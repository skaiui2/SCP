# Reproduce the SCP pacing paper experiments

This guide provides two separate checks:

1. **Reproduce the published analysis:** download the archived native logs, verify their hashes, independently audit them, and regenerate the numerical tables and the two paper figures.
2. **Repeat the experiment:** compile the original SCP C implementation and run it through the shared virtual network, first for an 8 MiB smoke test, then for the 1 GiB campaign.

The experiment executes `scp.c`, `scp.h`, and `lib/`. Python builds and launches the native executable and analyzes its output. It does **not** generate the observed packets, ACKs, or congestion windows from the paper's equations. This is an SCP-core experiment with a virtual clock/network, not a Linux UDP measurement or the old standalone Python model simulation.

## 1. Versions and evidence

| Item | Version / location |
|---|---|
| Original experiment | [`70f170ed505a55d83044eda2a2d5684fc80f382b`](https://github.com/skaiui2/SCP/commit/70f170ed505a55d83044eda2a2d5684fc80f382b) |
| Shared core and network baseline | `b591e9c3c0279b8d95e925066d3e87d10e7f0df6` |
| Completed Actions run | [38893710287](https://github.com/skaiui2/SCP/actions/runs/38893710287) |
| Raw data archive | [pacing-data-38893710287](https://github.com/skaiui2/SCP/releases/tag/pacing-data-38893710287) |
| Six-page paper | [PDF at paper commit 5d36b0f](https://github.com/skaiui2/SCP/blob/5d36b0fb3a0887ea930fd2e7d24d6fe11457e228/Pacing_and_Congestion_Window_Growth.pdf) |
| Archive and reference checksums | [manifest.json](manifest.json) |
| Expected numerical results | [reference/](reference/) |

The data release preserves the downloaded ZIPs without repacking them. It avoids depending on the retention period of Actions artifacts. Its tag identifies the **original experiment source**, while this guide and the analysis scripts live on `research/slowstart-g-100ms-20261012`.

The main results ZIP is approximately 1.34 GB. The preflight and workflow logs are separate small ZIPs. Allow at least 10 GB free for download, extraction, and analysis; a new full campaign needs additional space for build directories and temporary uncompressed logs. Keep the original logs even if a check fails.

## 2. Prepare the analysis environment

The commands below use a Linux shell, Python 3.12, and a full Git clone. Run them from the repository root unless indicated otherwise. A shallow clone can omit the baseline commit required by the runner's source checks.

```sh
git clone --branch research/slowstart-g-100ms-20261012 https://github.com/skaiui2/SCP.git
cd SCP
python3 -m venv reproduction/.venv
. reproduction/.venv/bin/activate
python -m pip install -r test/research/requirements.txt
```

The pinned analysis packages are NumPy 2.5.4, Matplotlib 3.11.2, and pandas 3.0.6. On Windows, activate the environment with `reproduction\.venv\Scripts\Activate.ps1`; the Python analysis commands are otherwise the same. Do not use `python -O`: the audit scripts intentionally retain their assertions and reject optimized execution.

## 3. Download and verify the original logs

```sh
python reproduction/download.py --out reproduction/data
```

The downloader verifies **both byte size and SHA-256 before extraction**. It extracts:

```text
reproduction/data/
  scp-fixed-results/       # all 66 native runs, original derived reports
  scp-fixed-preflight/     # original build and small-transfer evidence
  workflow-logs/          # original Actions job logs
```

Use a fresh extraction directory. The script refuses to overwrite an existing nonempty dataset. If you already downloaded the three ZIPs, use:

```sh
python reproduction/download.py --archive-dir /path/to/zip-files --out reproduction/data
```

No GitHub login is needed for the public release. If a network download is interrupted, rerun the command in a fresh extraction directory or use the verified ZIPs with `--archive-dir`.

## 4. Independently audit and redraw

```sh
python reproduction/review_all.py --data reproduction/data/scp-fixed-results --out reproduction/output
python reproduction/review_critical.py --data reproduction/data/scp-fixed-results --out reproduction/output
python reproduction/plot_paper_rtt.py --data reproduction/data/scp-fixed-results --out reproduction/output
python reproduction/verify_reference.py --out reproduction/output
```

What these commands check:

- `review_all.py`: all 66 cases; experiment commit; compressed artifact and derived-file hashes; gain encoding; received bytes and checksum; zero loss/retransmission/observer errors; fixed feedback delay; no cwnd-cap hit; and identical uncompressed log fingerprints across the three repetitions of each gain. Summary checks are distinguished from the packet-level check below.
- `review_critical.py`: independently reads all **750,869 DATA/ACK pairs** for `critical-r01`, checks sequence progression, actual send-to-ACK arrival and processing delay of **100 ms**, and the core's cwnd after every ACK. It also extracts RTT-boundary windows, transmission spans, first-packet timings, and the observed intra-RTT doubling check.
- `plot_paper_rtt.py`: reads actual core window samples from `run.log.gz` and transmission histograms from `packet_rtt.csv.gz`. It generates exactly **two figures**, each containing all 22 gains: cwnd versus elapsed RTTs, and within-RTT span versus RTT index. No model simulator is used.
- `verify_reference.py`: verifies the checked-in reference hashes, then requires exact CSV cell values (ignoring row/column order) and JSON values for six reference files. It fails on a discrepancy; it does not fit curves or update the references.

Main outputs:

| File in `reproduction/output/` | Contents |
|---|---|
| `paper_cwnd_rtt.pdf`, `.svg`, `.png` | Cwnd versus elapsed RTTs, all gains |
| `paper_span_rtt.pdf`, `.svg`, `.png` | Within-RTT span versus RTT index, all gains |
| `all_gain_cwnd_time.csv` | Every plotted 1 ms cwnd observation |
| `all_gain_L_time.csv` | Every plotted RTT-bin span and finite-transfer flag |
| `gain_summary_independent.csv` | Per-gain measured growth, concentration, and completion time; separately labelled model reference columns |
| `critical_boundaries_and_spans.csv` | Critical-gain RTT-boundary cwnd/flight and actual packet spans |
| `critical_first_packets.csv`, `critical_internal_error.csv` | Early packet timings and within-RTT comparisons |
| `campaign_audit.json`, `critical_independent_audit.json`, `critical_phase_audit.json` | Audit results and their observation scope |
| `figure_provenance.json`, `paper_rtt_captions.md` | Input paths, source commits, package versions, and figure definitions |

The archived result has critical-target `W(18R) = 132096 MSS`, `L(18) = 96 ms`, and final DATA ACK at `20.48 R`. For gain 8, `L(18) = 12 ms` and final DATA ACK at `20.06 R`. These are useful spot checks, not substitutes for the full audit.

PDF/SVG metadata, fonts, and renderer bytes can vary across systems. The numerical comparison is exact; a claim of identical figure-file bytes or identical binaries across platforms is not required.

## 5. Build the actual SCP core and run a smoke test

On Ubuntu, install a C compiler, CMake, Ninja, and Python development tooling as needed:

```sh
sudo apt-get update
sudo apt-get install build-essential cmake ninja-build python3-venv
export CMAKE_GENERATOR=Ninja
```

For an exact source-version rerun, create a separate checkout at the recorded experiment commit. Keep this research-branch checkout for the reproduction scripts:

```sh
git worktree add --detach ../SCP-pacing-original 70f170ed505a55d83044eda2a2d5684fc80f382b
cd ../SCP-pacing-original
```

Keep the previously activated Python environment. First run the existing functional and analysis tests:

```sh
cmake -S test -B build -DCMAKE_BUILD_TYPE=Release -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
cmake --build build --parallel 2
ctest --test-dir build --output-on-failure
python test/research/test_analysis.py
```

Then build the critical-gain native binary and send **8 MiB** through the virtual network:

```sh
python test/research/run_scp_fixed_feedback.py --build-dir research-build --gain-id critical --repeats 1 --bytes 8388608 --out results/preflight
```

This is a path/integrity smoke test, not the 1 GiB paper result. The runner compiles `scp_research`, checks that shared core/network files match the recorded baseline, and records compilation inputs, source hashes, the binary hash, command, effective gain, and native logs. An unsuccessful run exits nonzero and preserves evidence.

The executed path is:

```text
transfer_step -> scp_send -> scp_output -> scp_output_to_pacing
 -> scp_pacing_send_due -> scp_output_data -> vnet_send
 -> vnet_step -> transfer_input -> scp_input
```

The receiving SCP core creates ACKs and sends them back through the same virtual network to the sending core. The observer records events; it does not update cwnd, release flight credit, or synthesize ACKs from a formula. The original per-flow plotting path remains `test/plot.py -> test/analyze_scp.py`.

## 6. Repeat the paper-sized experiment

Choose either the critical gain alone (three 1 GiB transfers):

```sh
python test/research/run_scp_fixed_feedback.py --build-dir research-build --gain-id critical --out results/critical
```

or the **complete 22-gain, three-repeat campaign**:

```sh
python test/research/run_scp_fixed_feedback.py --build-dir research-build --out results/full
```

With no `--gain-id`, the runner uses all 22 gains in `test/research/scp_fixed_experiment.json`. There is no need to merge a campaign run by this single command: its native cases are already under `results/full/runs/`. Use new output paths for new experiments.

After the complete campaign, return to this guide's checkout and run the same audit/plot commands, replacing `--data reproduction/data/scp-fixed-results` with `--data ../SCP-pacing-original/results/full` and using a separate output directory. Run `verify_reference.py` against that directory. A difference is a finding to investigate, not permission to overwrite the archived reference tables.

For critical-only output, run only `review_critical.py`; the full-campaign audit and all-gain figures intentionally require all 66 cases. Its audit also assumes a 1 GiB run, so do not apply it to the 8 MiB smoke test.

To run on GitHub, use the existing **pacing research workflow** (`.github/workflows/pacing-research.yml`) on this research branch. Its preflight precedes the gain jobs. Download all per-gain artifacts and merge them with:

```sh
python test/research/run_scp_fixed_feedback.py --merge results/downloads --out results/merged
```

A new Actions run has its own run ID and commit. Record both. For a deliberate run of a newer research commit, pass that full SHA via `review_all.py --expected-commit SHA`; never relabel the new run as the archived campaign. The historical worktree route above avoids this ambiguity. Source/compiler/binary provenance belongs to each run; cross-platform numerical identity must be checked, not assumed.

## 7. Conditions and interpretation

- Generated payload: **1 GiB**, MSS **1430 bytes** except the final short packet; checksums at A and B must match. The archive's checksum is `7a4bb4de42097f8a`.
- Fixed full feedback RTT: **100 ms**, measured from actual DATA send to its ACK arriving and being processed by the sending core. Each network direction contributes 50 ms; there is no link serialization or packet loss.
- Initial cwnd: one MSS; ordinary per-ACK Reno slow start. Research compile-time limits allow the finite transfer to remain in slow start. Finite application backlog and transfer completion are recorded, not hidden.
- Core timing: Q32.32 millisecond reservations accumulated without per-packet fractional truncation; actual sends use the integer floor. The shared loop advances **1 ms**, processing arrivals, then core timers, then application work. An empty pacing queue starts at the current time; it retains no paper-model tail gap.
- Target gain is compiled as `round(2^32 / g)`; the critical target `2 ln 2 = 1.3862943611198906` uses integer `3098164009`, effective gain approximately `1.386294361280859`. This is not a substitution of 1.39 for the target, nor an exact representation of an irrational number.
- The 22 targets include `2 ln 2 + k*0.05` for `k=-6,...,6`, plus 1, 1.1, 1.75, 2, 2.5, 3, 4, 6, and 8. Each has three deterministic repetitions. They establish repeatability, not random-sample confidence intervals.
- Time zero is the **first actual DATA transmission**. It is not shifted to fit a prediction. Cwnd samples include both RTT boundaries and the 1 ms observations inside each RTT.
- `L(n)` is last minus first actual DATA send in **[nR,(n+1)R)**. A singleton has zero span; an empty interval has no span. The terminal DATA interval is flagged because the finite transfer can truncate it. These fixed-time bins are different from the paper's packet-index groups.
- The old retained-gap packet scheduler and the continuous-rate approximation are labelled analytical references. Their predictions are never substituted for measurements. This experiment does not establish `2 ln 2` as a universal implementation threshold or optimum.

## 8. What is committed and what is generated

This directory commits the guide, downloader, archive manifest, three analysis/plot scripts, reference verifier, and six compact reference tables/audits. Large raw logs are attached to the data release. Downloaded data, generated figures, build output, and the Python environment stay outside the committed material.

The scripts were adapted from the analysis used for the paper by adding portable input/output arguments. They retain the approved figure design and read the archived native observations. New simulation code, fitted replacement measurements, and a second implementation of SCP are not part of this package.

## 9. Checks performed for this publication

On 17 October 2026, the local reproduction check verified and extracted all three original archives, passed the 66-case audit and the independent 750,869-packet critical audit, regenerated both figures, and matched all six reference files exactly. The regenerated PNGs also matched the previously approved paper-figure PNGs byte-for-byte in that environment.

A fresh Windows/MinGW build of the native critical-gain target transferred 8,388,608 bytes through the shared virtual network: 5,867 DATA/ACK pairs, feedback minimum and maximum both 100 ms, zero dropped/retransmitted packets, and matching checksums `5ac1492526573cd9`. All five existing analysis tests passed. This publication check did not rerun the full 66-transfer campaign or the Linux UDP functional suite; their original execution evidence remains in the archived Actions run. The smoke test is additional execution evidence, not a replacement for that campaign.
