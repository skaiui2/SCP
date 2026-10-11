# Virtual generated-data transfer

This target links the actual SCP core. A generates deterministic data in 64 KiB chunks;
B receives through `scp_recv`. Both calculate FNV-1a-64 over the ordered application
data. B additionally checks every byte against the generator. Success requires all
bytes delivered, matching checksums, all data acknowledged, and connection closure.
No input or output payload file is used. Application backlog is capped at 1 MiB.

```sh
cmake -S test/virtual -B test/virtual/build -DCMAKE_BUILD_TYPE=Release
cmake --build test/virtual/build
ctest --test-dir test/virtual/build --output-on-failure
mkdir -p test/virtual/results/1g
cd test/virtual/results/1g
../../build/scp_virtual --bytes 1073741824
python3 ../../plot.py .
```

Default: 1 GiB, AIMD, 100 Mbps independently in each direction, 10 ms propagation
delay each way, no loss. Options: `--bytes`, `--mbps`, `--delay-ms`,
`--loss-ppm`, `--seed`, `--timeout-ms`. Every option takes an integer argument.
For 10 GiB use `--bytes 10737418240`; for 100 GiB use `--bytes 107374182400`.
For 0.1% packet loss use `--loss-ppm 1000`. Loss affects both DATA and control
packets, after consuming serialization time. Retransmissions use the actual core.

The network serializes packets FIFO per direction before adding propagation delay.
At each virtual millisecond it processes SCP timers, then A-to-B arrivals, then
B-to-A arrivals. The application produces/consumes data after those events.
There is currently no finite bottleneck queue, reordering or duplication model.
Counters use 64 bits so transfers can cross the protocol's 32-bit sequence wrap.

`samples.csv` records cwnd, actual flight, total reserved outstanding bytes,
delivery progress and network counters every 100 virtual milliseconds.
Queued unsent bytes are `reserved - flight`. `summary.json` includes configuration,
checksums and success. `plot.py` generates `curves.png` and requires matplotlib.
FNV is a test checksum, not a cryptographic authenticity check.

## Network sweep

From the repository root:

```sh
python3 test/virtual/sweep.py test/virtual/build/scp_virtual --out test/virtual/results/sweep --profile broad
python3 test/virtual/plot_sweep.py test/virtual/results/sweep
```

The sweep reads `network_sweep.json` next to the script by default.
Edit that file or pass `--config` to change transfer sizes and network grids.
`--profile` selects a grid; `--jobs` controls concurrent processes.
Each case has its own configuration, samples, summary and console log.
`sweep.csv` and `report.md` summarize all cases, including failures.
Timeout limits also come from the configuration file.

### Configured and GitHub runs

`network_sweep.json` defines editable parameter grids and timeout limits.
Configured profiles: `quick` (18 cases, 8 MiB), `broad` (300 cases, 8 MiB),
`large` (144 cases at 1 GiB plus 8 cases at 10 GiB), `stress` (8 cases at 100 GiB).
The configured deadline is 10 virtual hours and 600 wall seconds per case.

```sh
python3 test/virtual/sweep.py test/virtual/build/scp_virtual --config test/virtual/network_sweep.json --profile broad --out test/virtual/results/broad
python3 test/virtual/plot_sweep.py test/virtual/results/broad
```

The GitHub workflow runs `quick` on relevant pushes and pull requests. In Actions,
select **SCP virtual network**, **Run workflow**, then `broad`, `large` or `stress`.
Eight shards run independently and upload logs even on test failure. The report
job merges the shard CSVs and produces a plot and downloadable `network-report`.
Failed cases fail their shard; missing reports remain visible in the merged report.
Runs remain AIMD-only, with unlimited FIFO link queues and independent packet loss.
