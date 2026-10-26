# SCP tests

## Build

```sh
cmake -S test -B test/build -DCMAKE_BUILD_TYPE=Release
cmake --build test/build -j
ctest --test-dir test/build --output-on-failure
```

Python 3 is required for the paired UDP checks. Normal runs generate plots
automatically and require matplotlib (`python3 -m pip install matplotlib numpy pandas`).
CTest uses `--no-plot` so its correctness checks do not require matplotlib. On Windows append `.exe` to executable paths.

`scp_virtual` and `scp_udp` link the actual protocol core and share
`common/transfer.c`: A generates data in 64 KiB chunks; B validates every byte.
Both hash the ordered stream with FNV-1a-64. B returns its count and checksum
through SCP. A compares the reply, closes, and both require completed closure.
Generated-data mode creates no payload files. Transfers default to 1 GiB; use a different byte
count for 10 GiB or 100 GiB. Sender backlog is limited to about 1 MiB.

## Test design and environments

The test system asks three separate questions: whether the byte stream and connection lifecycle complete correctly, how the protocol behaves under configured network conditions, and what throughput or sharing is observed. A functional pass does not establish a throughput target or fairness guarantee.

### Architecture

```text
CTest / run.py / virtual/sweep.py
               |
        transfer workload
      common/transfer.c
               |
         actual SCP core
               |
    +----------+-----------+
    |                      |
virtual/vnet.c       common/udp_runner.c
seeded packet        sockets, real clock,
scheduler and        separate endpoint
simulated clock      processes
    |                      |
configured link      localhost / Linux netem / real path

core JSON logs -> original analyze_scp.py -> per-connection plots
case summaries -> sweep merge/report -> aggregate tables and overview
```

- `common/transfer.c` owns the shared generated-data workload, receive validation, checksum reply, and close progression. File transfers reuse the UDP/SCP path with input/output files. Fairness runs add a timed two-flow workload; short CTest competition runs check liveness.
- `virtual/vnet.c` owns packet delivery and link conditions, not retransmission or congestion control. Those remain in SCP. Each `vnet_step()` increments the clock by one tick (1 us by default), drains A-to-B arrivals, drains B-to-A arrivals, then processes core timers; the caller subsequently steps the applications. The optional rate model serializes packets before propagation and applies seeded random loss to DATA and control traffic in both directions; it has no finite queue, duplication, or deliberate reordering model. Setting the rate to zero disables serialization.
- `common/udp_runner.c` and `common/real_clock.c` provide real sockets and time. `run.py` starts the receiver before the sender, checks endpoint results, and organizes logs, file hashes, and plots. Localhost tests exercise OS networking but impose no WAN link constraints.
- Focused virtual cases exercise lifecycle, allocation failure, control-packet loss, and closure. Bulk transfers and sweeps reuse the shared workload rather than separate transfer implementations.
- `virtual/sweep.py` reads the single `network_sweep.json` grid, runs cases with explicit configurations and seeds, and merges results. GitHub distributes the grid across eight shards. Each case and connection retains its own core-state plots; the aggregate overview supplements them.

### Model and execution contract

The research workflow is idea → mathematical algorithm model → reviewed Python simulation → actual SCP execution. Both implementations retain the pacer's next available slot: start a batch at `max(next_slot, now)`, assign a packet's time, then advance the slot by that packet's gap. Empty queues retain the trailing interval. The reference is [Linux v6.12 `tcp_output.c`](https://github.com/torvalds/linux/blob/adc218676eef25575469234709c2d87185ca223a/net/ipv4/tcp_output.c#L1246-L1265), together with the [timestamp assignment](https://github.com/torvalds/linux/blob/adc218676eef25575469234709c2d87185ca223a/net/ipv4/tcp_output.c#L1302-L1304). SCP takes this next-slot principle; it does not reproduce Linux's initial-segment exemption, jitter compensation, socket, qdisc or offload path.

Both clocks advance one tick at a time, including idle ticks. Neither virtual network jumps to the earliest event. Network arrival is based on the actual send time plus the direction's delay; an overdue reservation is not treated as an earlier actual send. In the fixed-feedback campaigns, each direction is 50 ms with zero loss and no serialization, and every DATA-to-ACK feedback is checked as 100 ms.

| Detail | Reviewed Python model | SCP plus shared virtual network |
|---|---|---|
| Clock | 1 µs ticks | 1 µs virtual ticks by default; configurable 1 ms; real UDP stays 1 ms |
| Queues | Sender holds pending reservations; network has a DATA queue and an ACK queue | Pending pacing is in the core; two FIFO queues contain packets actually sent in each direction |
| Same-tick order | Forward arrivals, reverse ACKs, then due sends; each ACK may itself send due packets | Drain forward arrivals, reverse arrivals and then core timers; ACK handling can itself send due packets |
| Pacing arithmetic | Retain fractional slots; dispatch at their integer µs part; unquantized gain | Q32.32 tick slots, Q16 gain; timer wakes at the head packet's integer due tick |
| CA window | Real-valued `w += 1/w` by default; optional byte-floor and byte-remainder ablations | Integer bytes: divide `MSS * acked + cwnd_cnt` by the old window and retain the remainder |
| Payload | Full MSS packets, rounding the request up | Exact requested bytes, possibly a short last packet |

These are explicit differences. The old Python script floored each gap, which could erase SS spacing; the rebuilt model fixes that arithmetic mismatch. AIMD now retains fractional CA growth in `cwnd_cnt`, clearing it on initialization, loss and timeout; SS still adds the actual acknowledged bytes. A CA update may add zero bytes while retaining credit for later ACKs. Formula evaluation remains a mathematical reference, not clock-driven Python or native execution. Complete descendant groups have separate actual span, occupied span including the retained slot, and start drift. Exclude terminal groups from summaries; do not equate fixed-bin span with absence of drift.

`SCP_TICK_SCALE` is the number of protocol ticks per physical millisecond. The core default is 1; virtual targets use 1000 through `SCP_TEST_TICK_SCALE`, while `scp_udp` keeps 1. Build the two virtual clock configurations with `cmake -S test -B build-us -DSCP_TEST_TICK_SCALE=1000` and `cmake -S test -B build-ms -DSCP_TEST_TICK_SCALE=1`. RTT, retransmission, persist and idle state remain in ticks. PROB calibration and its Q16 delay retain physical milliseconds. When plotting a high-resolution run, `test/plot.py` preserves the original `run-ticks.log` and converts the analysis copy to milliseconds; `--normalize-only` performs just this conversion. Summary metadata records `tick_scale`, and raw packet events remain available for timing audits.

The [packet model](models/pacing.py) and [independent checker](models/check.py) make the continuous model → discrete algorithm → sandwich argument → Python comparison reviewable. The engineering CA gain is the fixed constant `C = 1.5 * ln(3)`; SS uses `2 * ln(2)`. SCP supplies their Q16 approximations through the controller callbacks: AIMD uses SS/CA values `90852`/`107998`, while PROB always uses the CA value. A small trace can be inspected before the 22-gain, three-repeat scan:

```sh
python3 test/models/pacing.py --mode ca --gain 1.6479184330021646 --bytes 100100 --repeats 3 --trace --out results/small
python3 test/models/check.py --runs results/small --out results/small-check
python3 test/models/pacing.py --mode ss --out results/python-ss
python3 test/models/pacing.py --mode ca --out results/python-ca
python3 test/models/check.py --runs results/python-ss --out results/ss-check
python3 test/models/check.py --runs results/python-ca --out results/ca-check
```

To isolate CA byte rounding while keeping the same clock, gain and queues, run the model with `--ca-update real`, `--ca-update byte-floor` or `--ca-update byte-remainder`. The floor mode reproduces the old per-ACK truncation and minimum one-byte increase; the remainder mode reproduces the current core's quotient and retained credit. Add `--ack-windows` to save each observed ACK's window and remainder for the independent checker. For a native-gain comparison, pass `--gain 1.647918701171875` (`107998 / 65536`) to each run. Python keeps full MSS packets, so compare matching ACKs before the native short final packet. Retained credit closely follows the real-valued recurrence, but does not guarantee a window error strictly below one byte.

For the CA sweep centred on `C = 1.5 * ln(3)`:

```sh
python3 test/models/pacing.py --mode ca --gain-grid ca-critical --out results/python-ca-critical
python3 test/models/check.py --runs results/python-ca-critical --out results/ca-critical-check
python3 test/models/plot.py --python-root results/python-ca-critical --out results/ca-critical-figures
```

This grid has 22 fixed gains and three repeats per gain: it adds `C ± 0.005`, `±0.01`, `±0.02`, `±0.05` and `±0.10`, with wider points from 0.9 to 8. Compare complete groups 300–600 and 900–1200 within each 1 GiB run. The compact [CA sweep table](https://github.com/skaiui2/SCP/blob/main/papers/pacing/data/ca-critical-sweep.csv) and [figure](https://github.com/skaiui2/SCP/blob/main/papers/pacing/figures/ca-critical-sweep.pdf) retain the plotted measurements without bundling raw logs.

The same batch also provides [cwnd and gain comparisons](https://github.com/skaiui2/SCP/blob/main/papers/pacing/figures/ca-critical-cwnd.pdf) and [within-run span and start-delay trajectories](https://github.com/skaiui2/SCP/blob/main/papers/pacing/figures/ca-critical-trajectories.pdf). The [1 ms native baseline table](https://github.com/skaiui2/SCP/blob/main/papers/pacing/data/ca-native-constant.csv) records the earlier actual SCP result: 88 ms late-group span under the same 100 ms feedback, versus the real-window Python model's 99.569 ms. It retains its original clock and source identity when compared with a fresh 1 us run.

Default input is a 1 GiB request rounded up to full MSS packets. `--policy` runs the current phase policy with unquantized model gains. The checker evaluates the ACK-index recurrence without importing the network loop; it checks every repetition and exports both clock-matched and continuous-time references. The earlier `1 + 1/(2w)` formula remains a continuous reference in the paper and discussion notes; earlier policy results identify that adaptive formula explicitly.

For dispersion, use `generations.csv`: each row follows the packets released by the preceding group's ACKs. Report `span_ms` (actual last send minus first send), `occupancy_ms` (including the retained tail slot), and `start_drift_ms` together. Select complete middle and late groups from the same transfer, exclude the terminal group, and state the selected range. Fixed 100 ms bins are useful for cwnd sampling but cannot test whether a descendant group spans or delays an RTT. Three repeats of this deterministic loss-free model check repeatability; they do not create random confidence intervals. Around the CA gain, sweeps show near-RTT spans below and at the chosen constant, and shorter spans above it. Record the measured values rather than forcing them to exactly 100 ms.

Actual SCP execution remains on the phase-specific [SS](https://github.com/skaiui2/SCP/tree/research/slowstart-nextslot-q16-20261022/reproduction) and [CA](https://github.com/skaiui2/SCP/tree/research/ca-nextslot-q16-20261022/reproduction) branches. [native_groups.py](models/native_groups.py) reads their audited transport events, attributes children to actual returning ACKs and labels reconstructed tail-slot fields. [plot.py](models/plot.py) consumes these tables and observed windows; it runs no simulation. Large raw results stay outside Git or in Actions artifacts. Completed earlier campaigns retain their source revision and policy label; fixed-gain scans remain comparable, while older CA controller-policy runs used the adaptive reference. The [discussion record](../papers/drafts/Pacing_Research_Discussion_Draft.md) records the failed connections, corrections and current limits.

### Environment and pass criteria

Build with CMake and a C99 compiler. Real UDP checks require Python 3; plotting requires matplotlib, numpy, and pandas. CTest disables plotting. Windows uses the compatibility headers and Winsock; Linux uses its native UDP sockets. The default protocol-log interval is 100 ms and is configurable at build time. Virtual time and real elapsed time must be labeled separately in reports.

Generated-data transfers validate the ordered bytes and compare FNV-1a-64 stream checksums returned through SCP. File runs additionally require equal lengths and SHA-256 hashes. Successful completion requires the applicable integrity checks and completed closure at both endpoints, without failure or timeout. Focused cases check their own state and resource conditions. Pacing statistics describe transport-send callback timing; fairness statistics should use receiver-delivered bytes over the shared measurement interval.

For reproducibility, retain the commit, OS/compiler, build configuration, controller, byte count or duration, rate, one-way delay, loss, seed where applicable, and timeouts. Keep logs and failure reports as well as successful results. The Linux CI workflow currently runs functional checks, separate 1 GiB virtual and localhost file transfers, and configured virtual sweeps; it does not run the namespace/netem setup.

### Earlier Linux netem experiments

`setup_scp_netns.sh` creates a routed `scp-c ↔ scp-r ↔ scp-s` topology using two veth pairs. The router forwards IP traffic and applies netem on both egress interfaces, so each direction has independently configured rate, one-way delay, random loss, and packet queue limit. The script attempts to disable interface offloads when `ethtool` is available. Setup requires Linux, root privileges, `ip` and `tc`; it recreates the named SCP namespaces.

The earlier validation snapshot in the main README used 5 Mbit/s per direction, 50 ms one-way delay, a 1000-packet queue, 100 MiB file transfers, and 300-second fairness measurements. The zero-loss and 10% random-loss observations are historical measurements, not results newly produced by the current CI. netem exercises real UDP/IP, qdisc queues and process scheduling under emulated impairments. It differs from the virtual model and from an actual intercontinental path.

```sh
sudo bash setup_scp_netns.sh setup 5mbit 50ms 0% 1000
sudo bash setup_scp_netns.sh ping
sudo bash setup_scp_netns.sh show
sudo python3 test/run.py test/build/scp_udp --netns --file testA.bin --timeout 3600 --out test/results/netem-file
sudo bash setup_scp_netns.sh clean
```

Prepare an existing nonempty input file first; its length determines the transfer size. Save `show` output with the experiment to record topology and qdisc statistics. Repeat setup with the desired loss (for example `10%`) for a separate condition and use a distinct output directory. Goodput, callback pacing, and receiver fairness are separate measurements; none alone proves the transport correct.

## File transfer on real Linux networking

The file test uses the same UDP/SCP receive and send path as the generated-data
test: A reads your input file; B writes `received.bin` after `scp_recv`. The runner
checks SCP checksums, file length and both files' SHA-256. It saves `A.log`, `B.log`,
`report.md` and protocol-state curves under `A/` and `B/` in the result directory.

```sh
python3 -m pip install matplotlib numpy pandas
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

This saves `run.log`, `summary.json`, `report.md` and
per-stream state curves under `run/`. `--mbps`, `--delay-ms`, `--loss-ppm` and
`--seed` configure this run. Executing the C binary directly writes summary JSON into
its working directory; automatic plotting is provided by `test/run.py`.

Options: `--bytes`, `--mbps`, `--delay-ms`, `--loss-ppm`, `--seed`, `--timeout-ms`.
Each takes an integer. Defaults: 1 GiB, AIMD, 100 Mbps per direction, 10 ms one-way
propagation, no loss. FIFO serialization precedes propagation. Random loss applies
to both control and DATA packets after serialization; retransmissions use the core.
There is no finite queue, duplication or deliberate reordering model.

Clock and queue ordering follow the [model and execution contract](#model-and-execution-contract).
State curves use the core JSON logs.
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
Every sweep automatically calls the original `analyze_scp.py` for each case and each stream.
Images and `summary.txt` are saved in `<case>/run/flow1/` and `flow2/`.
Each case has its own configuration, curves, summary and log. CSV/Markdown reports
include failures; goodput includes startup, ACK drain and close time.

GitHub Actions first reports functional/integrity PASS/FAIL, then runs two
1 GiB transfers: a virtual network with a 1000 Mbps link and a real Linux UDP
file transfer over localhost. `transfer-1g-curves` contains reports, summaries and
curves; `transfer-1g-logs` contains the original protocol logs for redrawing.
Generated input/output files are not uploaded.
The real UDP link has no imposed bandwidth/delay cap and is not a WAN measurement.
Eight virtual-network shards sweep configured parameters. The report job uses
the original analyzer in each sweep shard to draw every case's core-state curves;
the report job adds the sweep overview. `network-report` contains only the aggregate
`report.md`, `sweep.csv` and `sweep.png`; download `network-shard-*` for individual
case configurations, raw logs and per-connection plots. Artifacts are retained for
14 days; after expiration, rerun the workflow or the commands above. Local Linux real
network tests remain available (`ctest --test-dir test/build -L real`).
Relevant pushes run `scenarios`; manual runs can select any configured profile. Missing cases in recovered shards or failed
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
calls the original `test/analyze_scp.py` for individual field plots, unit conversion,
scatter plots and `summary.txt`, in a separate `flow<fd>/` directory per stream.
The original `test/fig.py` remains available for publication figures (PNG/PDF).
`fig.py` retains its original controller-specific thresholds; use it only when
those thresholds match the experiment.

CTest covers generated/file AIMD transfers, PROB transfers, pacing observations, two timed competition modes,
allocation failure and lifecycle cleanup, bidirectional close-before-drain, and
virtual transfers with and without loss. Large transfer and performance sweeps
remain explicit runs rather than mandatory local smoke tests.

## Focused loss scenarios

The default GitHub push sweep is `scenarios`: 150 runs of 64 MiB, using five
seeds and losses of 0, 0.01, 0.05, 0.1, 0.3, 0.5, 1, 2, 3 and 5 percent.
All scenarios use 100 Mbps: `wan` has 50 ms base RTT, `wifi-simplified` 10 ms,
and `intercontinental` 200 ms. These are controlled experimental settings,
not measured representative networks. Loss affects both directions, including
ACK and control packets. Wi-Fi contention and link-layer retries are not modeled.

Select `scenarios-1g` manually in Actions for 36 runs: 1 GiB, three seeds,
and 0, 0.1, 1 and 3 percent loss in each scenario. Local equivalents:

```sh
python3 test/virtual/sweep.py test/build/scp_virtual --profile scenarios --out test/results/scenarios
python3 test/virtual/sweep.py test/build/scp_virtual --profile scenarios-1g --out test/results/scenarios-1g
```

Each run uses the original state-log analyzer. `report.md` includes individual
failures and successful-run median/min/max goodput relative to the zero-loss
baseline; `sweep.png` plots loss versus median goodput with min/max shading.
