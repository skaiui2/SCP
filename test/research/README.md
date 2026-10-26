# Native SCP next-slot pacing study

This branch studies how gain changes cwnd and packet spreading after the next-slot
scheduler was introduced. It runs the actual SCP C core over the shared virtual
network. The independent Python packet model is a separate comparison source.
The historical campaign's [compact results and redraw guide](../../reproduction/README.md)
are available separately. The [current paper](https://github.com/skaiui2/SCP/blob/main/Pacing_and_Congestion_Window_Growth.pdf) is maintained on main.

## Conditions

`scp_fixed_experiment.json` is the campaign manifest. This branch starts at one
MSS. SS remains below ssthresh throughout the transfer. It uses baseline
`ddea25420afa0fc6dbd4bbeb5cff35f99a21a503` as the shared baseline, including scalable ticks,
deadline wakeups and CA byte remainders. Normal campaign freeze checks the shared
source files against that commit and records the experiment source and binary
identities. Historical committed
data used `e41ac8821bc3e0864a552cea89c86d075744ae9e`; their manifests are preserved.

- Each transfer carries exactly 1 GiB = 1,073,741,824 bytes. MSS is 1430 bytes;
  only the final packet is shorter.
- DATA travels for 50 ms; its ACK travels for 50 ms. There is no loss,
  serialization, bandwidth queue or random network variation. The audit requires
  actual DATA-send to ACK-processing feedback to equal 100 ms for every packet.
- The shared virtual loop defaults to 1 us (`SCP_TICK_SCALE=1000U`); scale 1
  provides the 1 ms control. Core reservations retain Q32.32 ticks and release
  at the integer tick. Timers arm for the head deadline. State/histogram sampling
  remains at physical 1 ms; event timestamps retain individual ticks.
- The 22 fixed gains each run three times. They use `round(g * 65536)` and record
  both the requested gain and the effective Q16 gain. The native policy runs
  three additional transfers, separately from the fixed-gain merge.
- Native AIMD uses fixed Q16 `90852` for `2*ln(2)` in SS and Q16 `107998` for
  `(3/2)*ln(3)` in CA. PROB uses `107998` throughout. Each controller registers
  its own gain callback. The constants are the engineering policy supported by
  the continuous/discrete/sandwich analysis and sweeps.

## Clock and packet delivery

Neither the native virtual network nor the reviewed Python model jumps to the next
event time. Native `vnet_step()` increments the clock by one tick (default 1 us), drains direction 0
then direction 1 into the peer's input path, and processes core timers. A packet's
50 ms propagation starts at its actual transport-send callback. The peer SCP core
constructs the ACK, which traverses the other direction; the network never updates
cwnd or flight itself.

Python increments its clock by 1 us. The sender owns pending reservations; the
network has separate DATA and ACK queues. Each tick processes forward arrivals,
reverse ACKs, then the sender timer; ACK handling can itself send due packets.
Actual sends begin the 50 ms forward propagation, and the receiver creates ACKs
for the separate 50 ms return delay. Fractional next-slots persist, and packets
are dispatched at their integer microsecond part. The stored 2026-10-22 Python
comparison used the earlier per-gap truncation and event order; it is not relabelled
as a run of this model. Python CA has `real`, `byte-floor` and `byte-remainder`
controls. Current native CA uses `byte-remainder`: divide `MSS*acked + cnt` by
the old window, add the quotient and retain the remainder. The audit checks both
window and counter per ACK; historical floor traces keep their original rule.

## Review the execution path

| File | Responsibility |
|---|---|
| `transfer_main.c` | Set up two actual SCP streams, select SS/CA and the gain callback; feed the common transfer application |
| `target.cmake` | Compile the baseline core, shared network and shared transfer code into `scp_research` |
| `../virtual/vnet.c` | Deliver copied packets after fixed delay and call the peer SCP input path; advance the core timers |
| `observe.c` | Read-only DATA/ACK, cwnd/counter, reservation, gain and next-slot observations |
| `audit.py` | Check baseline identity, packet/ACK paths and floor/remainder window updates |
| `run_scp_fixed_feedback.py` | Build, execute, validate, freeze provenance and merge the 66 fixed-gain cases |
| `comparison.py` | Produce observed cwnd/L tables and labelled formula comparisons |
| `../models/pacing.py` | Independent Python sender/receiver/network model; not the native protocol execution |
| `../models/check.py` | Independent ACK-recurrence checker for the observed model tables |
| `../models/native_groups.py` | Complete groups derived from audited actual-SCP transport events |
| `../models/plot.py` | Plots stored observations and source identities without running a simulation |

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

The [branch workflow](https://github.com/skaiui2/SCP/actions/workflows/pacing-research.yml?query=branch%3Aresearch%2Fslowstart-nextslot-q16-20261022)
runs functional checks and small native preflights before
the full campaign. Each gain job executes three transfers. The merge requires all
66 cases, intact evidence and consistent source identity; deterministic repeats
are checked rather than presented as independent random samples.

Download `scp-ss-results` from the chosen source-labelled Actions run for the compact figures
and tables. Per-gain artifacts contain the compressed native logs, packet events,
configuration, binary identity and audits. The native-policy artifact is separate.
Artifacts expire after 14 days; after expiry, rerun the workflow or these commands.
No raw campaign data is committed to Git or uploaded as a Release attachment.

For the current complete-group comparison, derive `generations.csv` or native
group tables and inspect middle and late groups within the same transfer. L is
the group's last actual send minus its first; also retain occupied span including
the trailing slot and accumulated start delay. Exclude the terminal group.

`L_fixed_RTT_all.csv` includes every observed bin. Historical fixed-bin dispersion plots
exclude the final sending bin and incomplete/empty bins. A span measured in fixed
100 ms bins is not the same quantity as a complete packet group's actual span,
occupied span including its retained slot, or cumulative start drift. The current
Python model retains slot fractions. Select its CA arithmetic explicitly when
matching native byte remainders; real-valued CA remains a reference. Gain
quantization and full-MSS rounding retain separate labels. Old tables keep the
clock and window arithmetic recorded by their original source manifests.
