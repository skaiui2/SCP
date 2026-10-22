# Reproduce the next-slot congestion avoidance study

This directory lets readers inspect and redraw the completed congestion avoidance gain sweep without rerunning transfers. It compares how gain changes the congestion window and packet spreading, using three separately labelled sources: mathematical references, the reviewed Python packet model, and the actual SCP C core in the shared virtual network. The committed material belongs only to this branch's phase; historical campaigns remain frozen.

The protocol and shared network/transfer code are identical to baseline [`e41ac88`](https://github.com/skaiui2/SCP/commit/e41ac8821bc3e0864a552cea89c86d075744ae9e). Experiment commit: [`9670889`](https://github.com/skaiui2/SCP/commit/9670889362677348b7077c6ff8c7ad885c6d0f3f); successful [Actions run 39697576064](https://github.com/skaiui2/SCP/actions/runs/39697576064). The current manuscript is maintained on [main](https://github.com/skaiui2/SCP/blob/main/Pacing_and_Congestion_Window_Growth.pdf), rather than duplicated here. See the [execution-path review map](../test/research/README.md) and [experiment manifest](../test/research/scp_fixed_experiment.json) for the underlying code and exact gain list.

## What this campaign measures

- 22 fixed gains, three transfers per gain: **66 actual SCP transfers**. A separate native AIMD gain-policy control adds three transfers; it is not relabelled as a fixed gain.
- Each actual transfer is exactly **1 GiB = 1,073,741,824 bytes**, starts at one MSS, and uses MSS = 1430 bytes. The final DATA payload is 584 bytes.
- DATA and ACK each traverse 50 ms. Every observed DATA-send to corresponding ACK-processing delay must be 100 ms. Loss, serialization and bandwidth queues are disabled.
- Gain requests are recorded together with `round(g * 65536) / 65536`. The shared core stores Q32.32 ms reservations and advances on the baseline 1 ms clock.

CA enters with `ssthresh = MSS` and starts at one MSS. Its native-policy control uses the candidate `1 + MSS/(2*cwnd)`, quantized to Q16; this is a first-order continuous reference, not a proven optimum for this scheduler.

The 66-case summary, complete repeat check, payload audit and RTT tables are in [`data/`](data/). Both large RTT CSVs retain **all 22 gains and all three repeats** and are compressed losslessly. No per-packet raw events, per-millisecond window tables, payload files, binaries or Release attachments are included. `source_manifest.json` identifies the source run and original CSV hashes; `checksums.json` verifies the committed native data. These compact tables are derived observations, not a replacement for the raw execution audit.

## Redraw without running experiments

From this branch's repository root, install the existing plotting requirements, verify and expand the compact native data, then draw the figures:

```bash
python -m pip install -r test/research/requirements.txt
python reproduction/prepare_data.py --out results/reproduction-data
python reproduction/plot_paper_nextslot.py --phase ca --data results/reproduction-data --layers reproduction/comparison/three_layer_by_RTT.csv.xz --policy reproduction/comparison/ca_native_policy_by_RTT.csv.xz --out results/paper-figures
```

Choose a new output directory when preparing data again. `prepare_data.py` verifies hashes and decompresses files using only the Python standard library. The plotting script reads stored results; it does not execute a transfer or rerun the Python model. Existing vector figures are in [`figures/`](figures/); the three-layer summary and native-policy summary are in [`comparison/`](comparison/). No full-scale Python campaign is needed just to redraw these plots.

## Obtain the Actions evidence

On [the successful run](https://github.com/skaiui2/SCP/actions/runs/39697576064), download **`scp-ca-results`** for the original analysis tables and manifest. The optional **`scp-ca-native`** contains the three native-policy runs. Optional `scp-ca-gain-*` artifacts contain the full native evidence for individual fixed gains: source/binary identity, configuration, DATA/ACK event logs and execution audits. Those raw evidence artifacts are much larger; download only the gains needed for a packet-path audit. They are not required for the redraw command above.

Actions artifacts are retained for 14 days. Once unavailable, rerun the source commands below or this branch's workflow. Large raw data is intentionally not duplicated in Git or Releases.

## Rerun the actual SCP campaign

Use this branch, a C compiler, CMake and Python. The runner builds `scp.c` together with the shared transport and transfer application; Python drives the binary and analyses its logs.

```bash
python -m pip install -r test/research/requirements.txt
python test/research/test_analysis.py
python test/research/run_scp_fixed_feedback.py --build-dir research-build --gain-id critical --repeats 1 --bytes 8388608 --out results/preflight
python test/research/run_scp_fixed_feedback.py --build-dir research-build --out results/fixed
python test/research/run_scp_fixed_feedback.py --build-dir research-build --gain-id native --out results/native
python test/research/run_scp_fixed_feedback.py --merge results/fixed --out results/merged
```

The final merge must find all 66 completed fixed-gain cases, exact byte counts and matching checksums, feedback min/max of 100 ms, zero retransmissions, complete packet/ACK audit evidence, unchanged shared sources and identical deterministic repeats. The native-policy three transfers are checked separately. Three identical deterministic repeats do not supply statistical error bars.

## Rerun the independent model and the comparison

This optional section is substantially slower than redrawing stored results: it executes another **22 gains × three Python-model runs** for this phase. `run_python.py` only invokes the reviewed [`test/models/pacing.py`](../test/models/pacing.py); it does not implement another sender, network or window update rule. It freezes that source and emits the batch layout expected by `compare_layers.py`.

```bash
python reproduction/run_python.py --out results/python --workers 4
python reproduction/compare_layers.py --native-ca results/reproduction-data --python-ca results/python/ca --metadata reproduction/actions_metadata.json --out results/three-layers
```

The comparison command above uses the full committed native RTT tables and complete summaries. To include the separately rerun native gain-policy control, additionally pass `--native-policy-ca results/native`. To regenerate packet-index cohort observations from raw native events, additionally pass `--raw-ca` pointing to the extracted per-gain evidence or the local fixed campaign. The raw option is needed only for that packet-level observation; stored cohort results, where applicable, remain in `comparison/`.

## Compare like quantities

| Source | Executed object | Clock, window and payload |
|---|---|---|
| Mathematical reference | ACK-index recurrence in `math_reference.py`, plus labelled analytical formulas | Continuous event times; no claim to be an SCP run or a new closed-form CA solution |
| Independent Python model | This repository's `test/models/pacing.py` sender, receiver and network | 1 µs clock, gaps floored to 1 µs; real-valued CA `w += 1/w`; unquantized gain; every packet is a full MSS |
| Native SCP plus virtual network | This branch's actual SCP core and shared network | 1 ms time progression, baseline integer-floor release rule and Q32.32 reservations; integer-byte CA increment `max(1, MSS*MSS/cwnd)`; Q16 gain; exact 1 GiB with a short tail |

The full-MSS Python model sends 750,869 packets, or 1,073,742,670 bytes: **846 bytes more** than the native transfer. This deliberate algorithm-model convention must not be hidden when comparing completion times.

Time zero is the first actual DATA send, and boundary state is labelled before/after ACK processing. Fixed-bin `L` is the last minus first new-DATA send in `[nR, (n+1)R)`; empty/single-packet bins do not define a dispersion span. Representative middle/late summaries exclude the terminal send bin. An SS packet-index cohort span is a separately labelled quantity and cannot be replaced by fixed-bin `L`. Finite-cohort formulas and asymptotic limits are also reported separately. No post-hoc time shift, fitted amplitude or interpolated measurement is used to force agreement.
