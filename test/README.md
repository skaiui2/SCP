# SCP tests

## Build

```sh
cmake -S test -B test/build -DCMAKE_BUILD_TYPE=Release
cmake --build test/build -j
ctest --test-dir test/build --output-on-failure
```

Python 3 is required for the paired UDP checks. Normal runs generate plots
automatically and require matplotlib (`python3 -m pip install matplotlib`).
CTest uses `--no-plot` so its correctness checks do not require matplotlib. On Windows append `.exe` to executable paths.

`scp_virtual` and `scp_udp` link the actual protocol core and share
`common/transfer.c`: A generates data in 64 KiB chunks; B validates every byte.
Both hash the ordered stream with FNV-1a-64. B returns its count and checksum
through SCP. A compares the reply, closes, and both require completed closure.
Generated-data mode creates no payload files. Transfers default to 1 GiB; use a different byte
count for 10 GiB or 100 GiB. Sender backlog is limited to about 1 MiB.

## File transfer on real Linux networking

The file test uses the same UDP/SCP receive and send path as the generated-data
test: A reads your input file; B writes `received.bin` after `scp_recv`. The runner
checks SCP checksums, file length and both files' SHA-256. It saves `A.log`, `B.log`,
`report.md` and protocol-state curves under `A/` and `B/` in the result directory.

```sh
python3 -m pip install matplotlib
sudo bash setup_scp_netns.sh setup 100mbit 10ms 0% 1000
sudo python3 test/run.py test/build/scp_udp --netns --file testA.bin --timeout 3600 --out test/results/linux-file
```

Use an existing nonempty file. Its size determines the transfer size; no manual
byte count is needed. If you want the runner to create a 1 GiB input file:

```sh
sudo python3 test/run.py test/build/scp_udp --netns --file-test --bytes 1073741824 --timeout 3600 --out test/results/linux-file
```

Without `--netns`, this runs two user-space SCP processes over localhost UDP.
`--netns` uses the existing `scp-c`/`scp-s` namespaces and defaults to peer
`10.0.2.1`; `--peer` overrides it. The runner does not create or change the topology.

For manual endpoints in separate terminals, specify matching byte counts:

```sh
sudo ip netns exec scp-s test/build/scp_udp B 10.0.2.1 1073741824 aimd --file outB.bin | tee B.log
sudo ip netns exec scp-c test/build/scp_udp A 10.0.2.1 1073741824 aimd --file testA.bin | tee A.log
python3 test/plot.py A.log --out test/results/A
python3 test/plot.py B.log --out test/results/B
sha256sum testA.bin outB.bin
```

The manual example assumes a 1 GiB input file. Automatic `--file` determines its
actual size and avoids mismatching counts.

## Real UDP

Start B, then A, in separate terminals:

```sh
test/build/scp_udp B
test/build/scp_udp A 127.0.0.1
```

The argument order is `A|B [peer-ip] [bytes] [aimd|prob|pacing]`.
B ignores the peer argument and learns A's address from the incoming packet.
Use the same size and controller at both ends. `pacing` uses AIMD; the sender
reports DATA transmission gaps and maximum packets in one millisecond, including
retransmissions. These observations do not assert an ideal pacing distribution.
All transfer modes include these observations and checksum verification.

The automatic paired check starts B first, verifies it remains waiting, starts A,
checks both exit codes and compares both checksums:

```sh
python3 test/run.py test/build/scp_udp --out test/results/udp-1g
python3 test/run.py test/build/scp_udp --bytes 8388625 --mode prob --out test/results/prob
```

For the existing Linux namespace topology:

```sh
sudo bash setup_scp_netns.sh
sudo ip netns exec scp-s test/build/scp_udp B
sudo ip netns exec scp-c test/build/scp_udp A 10.0.2.1
```

Two-flow timed experiments use `prob-prob` or `prob-aimd`; the amount argument
becomes seconds. Ports are A=5000/5001 and B=6000/6001. The receiver measures both
flows over the same interval starting at first payload. It reports delivered bytes,
per-flow goodput and Jain's index. Sender-side rates and Jain's index describe
enqueued bytes, not delivered data; use B's measurements for fairness. After measurement B sends a stop signal through SCP; A stops producing,
drains queued data and closes both connections. Closure data is excluded from the
receiver's measurement. A short smoke test checks liveness, not fairness quality.

```sh
test/build/scp_udp B 127.0.0.1 300 prob-aimd
test/build/scp_udp A 127.0.0.1 300 prob-aimd
python3 test/run.py test/build/scp_udp --mode prob-aimd --seconds 10 --out test/results/fairness
```

Fairness requires a shared constrained network path; localhost is only a smoke
check. Application enqueue and input processing alternate between flows.

## Virtual network

```sh
python3 test/run.py test/build/scp_virtual --backend virtual --bytes 1073741824 --out test/results/virtual-1g
```

This saves `run.log`, `samples.csv`, `summary.json`, `report.md`, `curves.png` and
per-stream state curves under `run/`. `--mbps`, `--delay-ms`, `--loss-ppm` and
`--seed` configure this run. Executing the C binary directly writes CSV/JSON into
its working directory; automatic plotting is provided by `test/run.py`.

Options: `--bytes`, `--mbps`, `--delay-ms`, `--loss-ppm`, `--seed`, `--timeout-ms`.
Each takes an integer. Defaults: 1 GiB, AIMD, 100 Mbps per direction, 10 ms one-way
propagation, no loss. FIFO serialization precedes propagation. Random loss applies
to both control and DATA packets after serialization; retransmissions use the core.
There is no finite queue, duplication or deliberate reordering model.

Virtual time advances in milliseconds. Each step processes timers, A-to-B arrivals,
B-to-A arrivals, then applications. `samples.csv` records cwnd, flight, reserved
bytes and delivery progress. `reserved - flight` is scheduled but unsent data.
`summary.json` records configuration, checksums and success.

## Parameter sweeps and reports

```sh
python3 test/virtual/sweep.py test/build/scp_virtual --profile quick --out test/results/sweep
python3 test/plot.py test/results/sweep --recursive
python3 test/virtual/sweep.py --merge test/results
```

`virtual/network_sweep.json` is the only grid configuration. Profiles: quick
(18 cases), broad (300), large (144 at 1 GiB plus 8 at 10 GiB), stress (8 at
100 GiB). `--config`, `--jobs`, `--shard-index` and `--shard-count` control execution.
Each case has its own configuration, curves, summary and log. CSV/Markdown reports
include failures; goodput includes startup, ACK drain and close time.

GitHub Actions first reports functional/integrity PASS/FAIL, then runs two
1 GiB transfers: a virtual network with a 1000 Mbps link and a real Linux UDP
file transfer over localhost. Both save protocol logs, reports and curves in the
`transfer-1g-curves` artifact; generated input/output files are not uploaded.
The real UDP link has no imposed bandwidth/delay cap and is not a WAN measurement.
Eight virtual-network shards sweep configured parameters. The report job uses
`test/plot.py --recursive` to draw every completed case's core-state curves plus
the sweep overview; download `network-report` for the plots. Local Linux real
network tests remain available (`ctest --test-dir test/build -L real`).
Relevant pushes run quick; manual runs select larger profiles. Missing cases in recovered shards or failed
case reports make the merge step fail.

## State logs and regression checks

```sh
python3 test/plot.py A.log --stream-fd 1 --out test/results/state-plots
```

Test targets enable the existing `SCP_DUMP_SS` protocol JSON printer. It outputs
every connection every 100 ms, plus allocation/release snapshots. To use 500 ms:

```sh
cmake -S test -B test/build -DSCP_TEST_LOG_INTERVAL_MS=500
cmake --build test/build -j
```

Normal core builds still default to logging disabled. The plotter filters by stream,
unwraps the 32-bit clock and plots recorded numeric fields in their raw units.
It exports phase transitions; add `--fields` for individual field plots and residual-delay phase portraits.
Old hardcoded controller thresholds and derived switching-accuracy claims were
removed; current fields are analyzed directly.

CTest covers generated/file AIMD transfers, PROB transfers, pacing observations, two timed competition modes,
allocation failure and lifecycle cleanup, bidirectional close-before-drain, and
virtual transfers with and without loss. Large transfer and performance sweeps
remain explicit runs rather than mandatory local smoke tests.
