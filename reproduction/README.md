# Reproduce the next-slot slow start study

This directory lets readers inspect and redraw the historical completed slow start gain sweep without rerunning transfers. It compares how gain changes the congestion window and packet spreading, using three separately labelled sources: mathematical references, the reviewed Python packet model, and the actual SCP C core in the shared virtual network. The committed material belongs only to this branch's phase.

The current shared baseline is `ddea25420afa0fc6dbd4bbeb5cff35f99a21a503`, with fixed Q16 phase gains: SS `90852`, CA and PROB `107998`. The [branch workflow](https://github.com/skaiui2/SCP/actions/workflows/pacing-research.yml?query=branch%3Aresearch%2Fslowstart-nextslot-q16-20261022) identifies the committed source of each run. The stored compact data retain baseline [`e41ac88`](https://github.com/skaiui2/SCP/commit/e41ac8821bc3e0864a552cea89c86d075744ae9e), experiment commit [`ef0076b`](https://github.com/skaiui2/SCP/commit/ef0076b39f8b3c6173f0d59273676e44f1c0e8f7), and successful [Actions run 39697564304](https://github.com/skaiui2/SCP/actions/runs/39697564304). The current manuscript is maintained on [main](https://github.com/skaiui2/SCP/blob/main/Pacing_and_Congestion_Window_Growth.pdf), rather than duplicated here. See the [execution-path review map](../test/research/README.md) and [experiment manifest](../test/research/scp_fixed_experiment.json) for the current code and gain list.

## Current source and historical data

Current shared sources match `ddea25420afa0fc6dbd4bbeb5cff35f99a21a503` and use scalable ticks, deadline wakeups and CA byte remainders. The normal campaign freeze verifies them against that commit. Earlier local validations retain their original commit plus source diff, compilation inputs and binary hashes; the stored Actions results below retain their original source identities.

Run the commands below to produce a fresh batch from the current baseline. Keep the new output directory, source manifest and gain list together. Existing `data/`, `comparison/` and figures retain their original revisions and gain meanings. This branch stores only SS material. Large DATA/ACK evidence remains in Actions artifacts or is regenerated locally.

## What this campaign measures

- 22 fixed gains, three transfers per gain: **66 actual SCP transfers**. A separate native AIMD gain-policy control adds three transfers; it is not relabelled as a fixed gain.
- Each actual transfer is exactly **1 GiB = 1,073,741,824 bytes**, starts at one MSS, and uses MSS = 1430 bytes. The final DATA payload is 584 bytes.
- DATA and ACK each traverse 50 ms. Every observed DATA-send to corresponding ACK-processing delay must be 100 ms. Loss, serialization and bandwidth queues are disabled.
- Gain requests are recorded together with `round(g * 65536) / 65536`. Current reservations use Q32.32 ticks; virtual targets default to 1 us (`SCP_TICK_SCALE=1000U`) and `--tick-scale 1` selects the 1 ms control. Pacing timers wake for the head deadline. Historical tables keep their original 1 ms grid.

SS starts at one MSS and keeps `ssthresh` above the attainable window. Its native-policy control uses Q16 `2*ln(2)` (`90852 / 65536`); this branch's fixed-gain list is unchanged. The shared baseline also supplies fixed Q16 `107998` for CA and PROB.

The stored historical 66-case summary, complete repeat check, payload audit and RTT tables are in [`data/`](data/). Both large RTT CSVs retain **all 22 gains and all three repeats** and are compressed losslessly. No per-packet raw events, per-millisecond window tables, payload files, binaries or Release attachments are included. `source_manifest.json` identifies the source run and original CSV hashes; `checksums.json` verifies the committed native data. These compact tables are derived observations, not a replacement for the raw execution audit.

## Current results and the Python difference

The tables in this section are preserved **2026-10-22 observations**. Their Python
source discarded each gap's fractional microseconds. The rebuilt model now in
`test/models/pacing.py` retains fractional slots; running it creates a new batch,
not a reproduction of those old Python curves. Historical comparison code and
source remain available at the [frozen SS revision](https://github.com/skaiui2/SCP/tree/631ab7b1aa68f234ff06c3bf3d02aa0ef1daa670/reproduction).

The completed sweep measures this next-slot implementation; the Python model is
an independent comparison with different precision. Completion is measured from
the first actual DATA send to the final ACK:

| Requested gain | Native SCP (ms) | Python (ms) | Continuous-time numerical reference (ms) |
|---|---:|---:|---:|
| 1 | 2682 | 2521.217 | 2682.611 |
| `2*ln(2)` | 2051 | 2051.628 | 2051.820 |

These values come from [`three_layer_summary.csv`](comparison/three_layer_summary.csv).
The reference column executes the labelled ACK-index model, not the SCP core.
The native critical gain is `90852/65536`, slightly below the requested real
`2*ln(2)`; the Python and numerical-reference columns use the requested gain.

The earlier Python source computed each gap as `floor(100000/(g*w))` microseconds,
with `w` in MSS.
It discards the fractional remainder on every reservation. Once `w > 100000/g`,
the computed gap becomes zero and packets can share a tick. The native scheduler
retains Q32.32-ms fractional reservations even though it releases packets on a
1 ms clock. This accumulated precision difference explains why the Python
`g=1` run finishes substantially earlier; it is not evidence that the native
100 ms feedback RTT changed.

For complete packet-index cohort 18 at `g=1`, native `L` is **138 ms**, Python
`L` is **99.999 ms**, and the finite ideal value is **138.628673 ms**; see
[`ss_packet_index_group_L.csv`](comparison/ss_packet_index_group_L.csv).
The native fixed-bin span of 99 ms is a different quantity: a half-open 100 ms
bin on the 1 ms send grid cannot show a span above 99 ms. It does not establish
that a packet cohort stays inside one RTT or that packets are uniformly spread.

## Redraw without running experiments

From this branch's repository root, install the existing plotting requirements, verify and expand the compact native data, then draw the figures:

```bash
python -m pip install -r test/research/requirements.txt
python reproduction/prepare_data.py --out results/reproduction-data
python reproduction/plot_paper_nextslot.py --phase ss --data results/reproduction-data --layers reproduction/comparison/three_layer_by_RTT.csv.xz --policy reproduction/comparison/ss_native_policy_by_RTT.csv.xz --ss-groups reproduction/comparison/ss_packet_index_group_L.csv --out results/paper-figures
```

Choose a new output directory when preparing data again. `prepare_data.py` verifies hashes and decompresses files using only the Python standard library. The plotting script reads stored results; it does not execute a transfer or rerun the Python model. Existing vector figures are in [`figures/`](figures/); the three-layer summary and native-policy summary are in [`comparison/`](comparison/). No full-scale Python campaign is needed just to redraw these plots.

## Obtain the Actions evidence

On [the successful run](https://github.com/skaiui2/SCP/actions/runs/39697564304), download **`scp-ss-results`** for the original analysis tables and manifest. The optional **`scp-ss-native`** contains the three native-policy runs. Optional `scp-ss-gain-*` artifacts contain the full native evidence for individual fixed gains: source/binary identity, configuration, DATA/ACK event logs and execution audits. Those raw evidence artifacts are much larger; download only the gains needed for a packet-path audit. They are not required for the redraw command above.

Actions artifacts are retained for 14 days. Once unavailable, rerun the frozen source revision for those historical conditions, or use the commands below for a new current-source campaign. Large raw data is intentionally not duplicated in Git or Releases.

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
python test/models/pacing.py --mode ss --out results/python-ss
python test/models/check.py --runs results/python-ss --out results/python-ss-check
python test/models/plot.py --python-root results/python-ss --out results/python-ss-figures
```

The default is 22 fixed gains, three repeats, and a 1 GiB request rounded up to
full MSS packets. Use a fresh output directory. Run the native-style policy
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
python test/models/plot.py --python-root results/python-ss --native-root results/fixed --native-groups results/native-groups/native_groups.csv --checks results/python-ss-check/rtt_comparison.csv --out results/current-comparison
```

The checker evaluates recurrences; it does not execute SCP or a second network.
The native group analyzer attributes new packets to actual returning ACKs and
labels reconstructed tail-slot fields. The plotter consumes those observations
without fitting or shifting time. Native `vnet_step()` advances one tick at a time
(default 1 us; 1 ms control with scale 1);
each direction's 50 ms delay starts only at an actual transport send.

## Compare like quantities

| Source | Executed object | Clock, window and payload |
|---|---|---|
| Mathematical reference | Independent ACK-index evaluation in `test/models/check.py`, including labelled SS finite-group formulas | Clock-matched and continuous references are separate; neither executes SCP |
| Independent Python model | This repository's `test/models/pacing.py` sender, receiver and network | 1 µs ticks; retain fractional slots and dispatch their integer µs part; one MSS added per ACK; unquantized gain; full MSS packets |
| Native SCP plus virtual network | This branch's actual SCP core and shared network | 1 us default tick; Q32.32 tick reservations with integer-floor release; acknowledged bytes added to cwnd; Q16 gain; exact 1 GiB with a short tail |

The full-MSS Python model sends 750,869 packets, or 1,073,742,670 bytes: **846 bytes more** than the native transfer. This deliberate algorithm-model convention must not be hidden when comparing completion times.

Time zero is the first actual DATA send, and boundary state is labelled before/after ACK processing. For the current group comparison, L is the last actual send minus the first within a complete descendant group. Read it together with occupied span including the trailing slot and accumulated start delay; exclude terminal groups. The stored fixed-bin `L` is instead the last minus first new-DATA send in `[nR, (n+1)R)`; empty bins have no span, and single-packet bins have zero span without establishing dispersion. Representative fixed-bin summaries exclude the terminal send bin. An SS packet-index cohort span cannot be replaced by fixed-bin `L`. Finite-cohort formulas and asymptotic limits are also reported separately. No post-hoc time shift, fitted amplitude or interpolated measurement is used to force agreement.
