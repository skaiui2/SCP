# Native SCP next-slot pacing study

This branch studies how gain changes cwnd and packet spreading after the next-slot
scheduler was introduced. It runs the actual SCP C core over the shared virtual
network. The independent Python packet model is a separate comparison source.
New results are pending; the papers inherited from main describe earlier campaigns.

## Conditions

`scp_fixed_experiment.json` is the campaign manifest. SS starts at one MSS
and remains below its configured ssthresh; CA starts at one MSS with ssthresh
set to one MSS. Both branches use baseline
`e41ac8821bc3e0864a552cea89c86d075744ae9e` without a core patch.

- Each transfer carries exactly 1 GiB = 1,073,741,824 bytes. MSS is 1430 bytes;
  only the final packet is shorter.
- DATA travels for 50 ms; its ACK travels for 50 ms. There is no loss,
  serialization, bandwidth queue or random network variation. The audit requires
  actual DATA-send to ACK-processing feedback to equal 100 ms for every packet.
- The shared virtual loop advances by 1 ms. Core reservations retain Q32.32 ms;
  the core's integer-floor send rule is preserved and audited.
- The 22 fixed gains each run three times. They use `round(g * 65536)` and record
  both the requested gain and the effective Q16 gain. The native policy runs
  three additional transfers, separately from the fixed-gain merge.
- Native AIMD uses Q16 `2*ln(2)` in SS and the candidate
  `1 + MSS/(2*cwnd)` in CA. The CA expression is a continuous first-order reference,
  not an established optimum for every packet scheduler.

## Review the execution path

| File | Responsibility |
|---|---|
| `transfer_main.c` | Set up two actual SCP streams, select SS/CA and the gain callback; feed the common transfer application |
| `target.cmake` | Compile the baseline core, shared network and shared transfer code into `scp_research` |
| `../virtual/vnet.c` | Deliver copied packets after fixed delay and call the peer SCP input path; advance the core timers |
| `observe.c` | Read-only DATA/ACK, reservation, gain and next-slot observations |
| `audit.py` | Check baseline source identity, complete packet/ACK paths and window updates |
| `run_scp_fixed_feedback.py` | Build, execute, validate, freeze provenance and merge the 66 fixed-gain cases |
| `comparison.py` | Produce observed cwnd/L tables and labelled formula comparisons |
| `../models/pacing.py` | Independent Python sender/receiver/network model; not the native protocol execution |

## Reproduce

From the repository root, with CMake, a C compiler and Python available:

```bash
python -m pip install -r test/research/requirements.txt
python test/research/test_analysis.py
python test/research/run_scp_fixed_feedback.py --build-dir research-build --gain-id critical --repeats 1 --bytes 8388608 --out results/preflight
python test/research/run_scp_fixed_feedback.py --build-dir research-build --out results/fixed
python test/research/run_scp_fixed_feedback.py --build-dir research-build --gain-id native --out results/native
python test/research/run_scp_fixed_feedback.py --merge results/fixed --out results/merged
```

The branch workflow runs functional checks and small native preflights before
the full campaign. Each gain job executes three transfers. The merge requires all
66 cases, intact evidence and consistent source identity; deterministic repeats
are checked rather than presented as independent random samples.

Download `scp-ss-results` from the successful Actions run for the compact figures
and tables. Per-gain artifacts contain the compressed native logs, packet events,
configuration, binary identity and audits. The native-policy artifact is separate.
Artifacts expire after 14 days; after expiry, rerun the workflow or these commands.
No raw campaign data is committed to Git or uploaded as a Release attachment.

`L_fixed_RTT_all.csv` includes every observed bin. Representative dispersion plots
exclude the final sending bin and incomplete/empty bins. A span measured in fixed
100 ms bins is not the same quantity as a packet-generation tail limit. Python
model output also differs in its 1 us clock, real-valued CA window, unquantized gain
and full-MSS rounding of the requested byte count; comparisons must retain these
labels rather than demand artificial sample-by-sample agreement.
