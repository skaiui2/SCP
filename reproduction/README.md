# Reproduce the next-slot congestion avoidance study

This directory lets readers inspect and redraw the historical completed congestion avoidance gain sweep without rerunning transfers, then run the current sweep around fixed C = (3/2) ln 3. It compares how gain changes the congestion window and packet spreading, using three separately labelled sources: mathematical references, the reviewed Python packet model, and the actual SCP C core in the shared virtual network. The committed material belongs only to this branch's phase.

Current source uses shared baseline `da52bfe3690752a393f17d26f0ec60e7966db0b8`, including fixed Q16 phase gains: SS `90852`, CA and PROB `107998`. The [branch workflow](https://github.com/skaiui2/SCP/actions/workflows/pacing-research.yml?query=branch%3Aresearch%2Fca-nextslot-q16-20261022) reports reruns of this source. The stored compact data retain baseline [`e41ac88`](https://github.com/skaiui2/SCP/commit/e41ac8821bc3e0864a552cea89c86d075744ae9e), experiment commit [`9670889`](https://github.com/skaiui2/SCP/commit/9670889362677348b7077c6ff8c7ad885c6d0f3f), and successful [Actions run 39697576064](https://github.com/skaiui2/SCP/actions/runs/39697576064). The current manuscript is maintained on [main](https://github.com/skaiui2/SCP/blob/main/Pacing_and_Congestion_Window_Growth.pdf), rather than duplicated here. See the [execution-path review map](../test/research/README.md) and [experiment manifest](../test/research/scp_fixed_experiment.json) for the current code and gain list.

## Current source and historical data

Run the commands below to produce a fresh batch from the current baseline. Keep the new output directory, source manifest and gain list together. Existing `data/`, `comparison/` and figures retain their original revisions and gain meanings; they are not relabelled as new fixed-CA-policy results. This branch stores only CA material. Large DATA/ACK evidence remains in Actions artifacts or is regenerated locally.

## What this campaign measures

- 22 fixed gains, three transfers per gain: **66 actual SCP transfers**. A separate native AIMD gain-policy control adds three transfers; it is not relabelled as a fixed gain.
- Each actual transfer is exactly **1 GiB = 1,073,741,824 bytes**, starts at one MSS, and uses MSS = 1430 bytes. The final DATA payload is 584 bytes.
- DATA and ACK each traverse 50 ms. Every observed DATA-send to corresponding ACK-processing delay must be 100 ms. Loss, serialization and bandwidth queues are disabled.
- Gain requests are recorded together with `round(g * 65536) / 65536`. The shared core stores Q32.32 ms reservations and advances on the baseline 1 ms clock.

CA enters with `ssthresh = MSS` and starts at one MSS. The current native-policy control uses fixed `C = 1.5 * ln(3)`, quantized to Q16 `107998` (effective gain `1.647918701171875`). The current 22-gain grid has C and C ± 0.005, ±0.01, ±0.02, ±0.05 and ±0.10, plus 0.9, 1, 1.2, 1.4, 1.5, 1.9, 2, 2.5, 3, 4 and 8. `critical` now identifies C. Historical committed data used the earlier grid centred on `2 ln 2`; their former `critical` and adaptive native-policy labels remain unchanged in those data.

The stored historical 66-case summary, complete repeat check, payload audit and RTT tables are in [`data/`](data/). Both large RTT CSVs retain **all 22 gains and all three repeats** and are compressed losslessly. No per-packet raw events, per-millisecond window tables, payload files, binaries or Release attachments are included. `source_manifest.json` identifies the source run and original CSV hashes; `checksums.json` verifies the committed native data. These compact tables are derived observations, not a replacement for the raw execution audit.

## Current fixed-CA control

Three new native controls use the current fixed Q16 `107998` policy. Every exact 1 GiB transfer passes checksums and the packet-path audit: feedback is 100 ms for every packet, with no retransmissions or drops. Completion is 1339.38 RTTs in each repeat. Complete groups 300-600 and 900-1200 have actual L = 88 ms; late occupied span has median 88.732585 ms and accumulated start delay is zero. The corresponding real-window Python C result has late L = 99.569 ms and zero start delay. The difference is retained as an implementation observation, rather than calling both spans one RTT. Compact [group measurements](https://github.com/skaiui2/SCP/blob/main/papers/pacing/data/ca-native-constant.csv) and [transfer audits](https://github.com/skaiui2/SCP/blob/main/papers/pacing/data/ca-native-transfers.csv) are maintained with the paper on main. The branch workflow reports the new full sweep separately from these completed local controls.

## Current results and the Python difference

The tables in this section are preserved **2026-10-22 observations**. Their Python
source discarded each gap's fractional microseconds. The rebuilt model now in
`test/models/pacing.py` retains fractional slots; running it creates a new batch,
not a reproduction of those old Python curves. Historical comparison code and
source remain available at the [frozen CA revision](https://github.com/skaiui2/SCP/tree/d53c6d5fb6612220d16289558a560da7d2c2b615/reproduction).

The native CA window is stored in integer bytes. For a full-MSS ACK its update is
`C += max(1, floor(MSS*MSS/C))`; the Python model instead keeps the real-valued
update `w += 1/w`, with `w` in MSS. Native rounding discards the fractional
increase on every ACK without carrying it forward. After the same **750,868
full-MSS ACKs**, the native window is **1,476,947 bytes**, versus
**1,752,403.2934 bytes** for the real-valued recurrence: **−15.719%**, relative
to the real-valued value. The final short ACK adds another byte natively.
All 750,869 native ACK updates in the audited `g=1` run match the integer rule
exactly. Download `scp-ca-gain-g1` from the linked Actions run to inspect
`runs/g1-r01/ack_updates.csv.gz`.

Integer thresholds also explain the sawtooth in *differences between gains*.
At `C = MSS*MSS/2 = 1,022,450` bytes the full-ACK increment changes from
2 to 1 byte when the window crosses the threshold. Boundary windows include
ACKs processed at that boundary; their difference counts ACKs in `(814R, 815R]`:

| Gain | cwnd at `814R` (bytes) | ACKs | Increase per ACK (bytes) | cwnd at `815R` (bytes) |
|---|---:|---:|---:|---:|
| 1 | 1,020,627 | 713 | 2 | 1,022,053 |
| 8 | 1,023,218 | 715 | 1 | 1,023,933 |

The larger window crosses the threshold first, so the gap between the windows
shrinks by `713*2 - 715*1 = 711` bytes, or **0.497203 MSS**. Both windows still
grow. Their boundary values are stored in
[`three_layer_by_RTT.csv.xz`](comparison/three_layer_by_RTT.csv.xz); the ACK
counts and increments can be checked in the corresponding raw gain artifacts.
This effect has no counterpart in the real-valued Python update.

At the same elapsed RTT boundary, the native/Python cwnd difference reaches
about **21.9%** relative to Python across the sweep. That comparison also
includes different numbers of processed ACKs; it is not the same measurement
as the equal-ACK 15.719% deficit. See
[`three_layer_summary.csv`](comparison/three_layer_summary.csv).
The separate native `1 + MSS/(2*cwnd)` policy completes in **134.139 s**; its
middle/late fixed-bin span of 99 ms does not prove an optimal discrete gain or
`L = RTT`. That stored comparison had no matching dynamic-policy Python arm.
The current `--policy` model command runs the new fixed phase policy, so it does
not reproduce that old adaptive arm.

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

## Run and check the rebuilt independent model

The current model is shared with the baseline. It has an A sender, a B receiver,
a pending pacing queue, and separate forward DATA and returning ACK queues. Its
clock advances by 1 us without event jumps. New batches retain fractional slots
and dispatch at the integer microsecond part; this replaces per-gap truncation.
Existing comparison tables above keep their original model label.

Run this branch's phase only, then check every repetition against the independent
ACK-index recurrence and draw its observed windows, complete group spans,
occupied spans and start drift:

```bash
python test/models/pacing.py --mode ca --gain-grid ca-critical --out results/python-ca
python test/models/check.py --runs results/python-ca --out results/python-ca-check
python test/models/plot.py --python-root results/python-ca --out results/python-ca-figures
```

The `ca-critical` grid is 22 fixed gains, three repeats, and a 1 GiB request rounded up to
full MSS packets. The model's unchanged default grid remains available for historical
comparisons. Use a fresh output directory. Run the native-style policy
separately with `--policy`; it uses the unquantized fixed phase constant.
Native Q16 gain and integer window arithmetic remain distinct. A small
`--trace` run records reservation, send and ACK evidence. `--spacing gap-us`
is an explicit arithmetic control with the new event order, not a replay of the
entire earlier model. `--initial-mss 1..20` paces its initial train using known
RTT; native experiments here still start from one MSS.

After rerunning the actual core into `results/fixed`, derive native groups
from its audited transport events and compare the two labelled sources:

```bash
python test/models/native_groups.py --runs results/fixed --out results/native-groups
python test/models/plot.py --python-root results/python-ca --native-root results/fixed --native-groups results/native-groups/native_groups.csv --out results/current-comparison
```

The checker evaluates recurrences; it does not execute SCP or a second network.
The native group analyzer attributes new packets to actual returning ACKs and
labels reconstructed tail-slot fields. The plotter consumes those observations
without fitting or shifting time. Native `vnet_step()` still advances by 1 ms;
each direction's 50 ms delay starts only at an actual transport send.

## Compare like quantities

| Source | Executed object | Clock, window and payload |
|---|---|---|
| Mathematical reference | Independent ACK-index evaluation in `test/models/check.py` | Clock-matched and continuous references are separate; neither executes SCP or proves an infinite-time CA threshold |
| Independent Python model | This repository's `test/models/pacing.py` sender, receiver and network | 1 µs ticks; retain fractional slots and dispatch their integer µs part; real-valued CA `w += 1/w`; unquantized gain; full MSS packets |
| Native SCP plus virtual network | This branch's actual SCP core and shared network | 1 ms time progression, baseline integer-floor release rule and Q32.32 reservations; integer-byte CA increment `max(1, MSS*MSS/cwnd)`; Q16 gain; exact 1 GiB with a short tail |

The full-MSS Python model sends 750,869 packets, or 1,073,742,670 bytes: **846 bytes more** than the native transfer. This deliberate algorithm-model convention must not be hidden when comparing completion times.

Time zero is the first actual DATA send, and boundary state is labelled before/after ACK processing. For the current engineering comparison, use `generations.csv` or reconstructed native groups: L is the first-to-last actual send of a complete descendant group, Omega also includes its retained tail slot, and start drift records accumulated delay. Compare middle groups 300-600 and late groups 900-1200 within the same transfer, excluding the terminal group. The stored fixed-bin `L` is instead the last minus first new-DATA send in `[nR, (n+1)R)`; empty bins have no span, and single-packet bins have zero span without establishing dispersion. Representative fixed-bin summaries exclude the terminal send bin. On the native 1 ms send grid, a half-open 100 ms bin cannot show a span above 99 ms. No post-hoc time shift, fitted amplitude or interpolated measurement is used to force agreement.
