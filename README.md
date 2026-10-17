# SCP pacing research: gain, slow-start growth, and transmission spread

This research branch investigates a question encountered while implementing SCP: **why can adding pacing slow down slow start, and how does increasing the pacing gain change window growth and the concentration of packet transmissions?** The focus is the neighborhood of `g = 2 ln 2`, a reference value from the continuous model.

The experiments execute the actual SCP C core with the baseline virtual network and transfer code. They measure cwnd at RTT boundaries and inside each RTT, and the first-to-last packet transmission span `L` within each RTT. The archived campaign contains 22 gains with three deterministic 1 GiB transfers each, a fixed actual send-to-ACK RTT of 100 ms, and zero loss. The protocol's 1 ms execution and integer pacing rules remain part of the experiment.

**What can you reproduce here?** You can regenerate the paper's cwnd-versus-RTT and L-versus-RTT figures from the original native logs, verify the accompanying numerical tables, or compile SCP and repeat the transfers. This tests the implemented scheduler against the analytical references; it does not assume that the implementation and an idealized model are identical.

| Start here | Purpose |
|---|---|
| [Step-by-step reproduction guide](reproduction/README.md) | Download and verify the archived evidence, redraw both figures, then build and rerun the native experiments |
| [Pacing Gain and Slow-Start Growth: A Packet Model and SCP Experiments](https://github.com/skaiui2/SCP/blob/5d36b0fb3a0887ea930fd2e7d24d6fe11457e228/Pacing_and_Congestion_Window_Growth.pdf) | Read the six-page paper associated with these results |
| [Original logs and build evidence](https://github.com/skaiui2/SCP/releases/tag/pacing-data-38893710287) | Inspect the archived campaign, with checksums recorded in [the manifest](reproduction/manifest.json) |
| [Research implementation and settings](test/research/README.md) | Inspect the execution path, gain encoding, timing rules, and experiment runner |

Branch: `research/slowstart-g-100ms-20261012`. The archived experiment used commit `70f170e`, sharing its core and virtual-network implementation with baseline `b591e9c`; research additions provide configuration, an experiment entry point, and observation/analysis. The [main branch](https://github.com/skaiui2/SCP/tree/main) remains the general protocol-development branch. The protocol overview below describes the wider SCP project.

## SCP (Simple Stream Control Protocol)

[中文说明](./docs/中文/README中文.md)

SCP is a compact, stream-oriented reliable transport protocol implemented over UDP or another unreliable packet transport. The project focuses on network transport research: exploring reliable delivery, congestion control, pacing, and connection behavior with an inspectable C implementation.

SCP provides sequencing, ACK/SACK processing, retransmission, flow control, connection management, RTT/RTO estimation, and congestion control without depending on a kernel TCP stack. The project currently contains two interchangeable congestion controllers:

- **SCP-PROB**: an experimental nonlinear controller that converts loss and RTT evidence into a continuous congestion-likelihood signal.
- **AIMD**: a conventional additive-increase, multiplicative-decrease controller, also used as the baseline in comparative experiments.

The current implementation also includes a sender-side pacing layer. Packets admitted by the congestion window are assigned transmission times derived from `cwnd` and RTT, then released by the pacing timer.

> **Project status:** SCP is under active development; its API and wire format may change. The transport core and the selectable congestion controllers are evaluated separately. The PROB-specific experimental results are documented below.

## Research papers

The probability-driven controller is described in [SCP-PROB: Probability-Driven Nonlinear Congestion Control](./SCP-PROB_Probability-Driven_Nonlinear_Congestion_Control.pdf).

The current pacing paper is [Pacing Gain and Slow-Start Growth: A Packet Model and SCP Experiments](https://github.com/skaiui2/SCP/blob/5d36b0fb3a0887ea930fd2e7d24d6fe11457e228/Pacing_and_Congestion_Window_Growth.pdf). It compares analytical models with native SCP virtual-network observations. Its [manuscript and figure files](https://github.com/skaiui2/SCP/tree/5d36b0fb3a0887ea930fd2e7d24d6fe11457e228/papers/pacing) are pinned to the paper revision; use that complete directory for compilation. The manuscript files inherited by this research branch are an earlier model-only revision. For the current paper's experimental evidence and reproduction instructions, use [reproduction/](reproduction/README.md).

## Features

- Reliable byte stream over an unreliable packet transport
- Sequence numbers, cumulative ACKs, and SACK information
- Out-of-order buffering and retransmission
- Receiver-advertised flow-control window
- RTT, RTT variance, and retransmission-timeout estimation
- CONNECT/FIN connection lifecycle
- Pluggable congestion-control callbacks
- Selectable PROB and AIMD congestion controllers
- Sender-side pacing based on `cwnd / RTT`
- Red-black trees for timers and out-of-order receive buffers
- Small C implementation intended to remain inspectable and portable

## Repository layout

```text
scp.c / scp.h                  protocol core
lib/                           intrusive containers and data structures
test/common/                   shared transfer logic and real UDP runner
test/virtual/                  virtual network, functional tests and sweeps
test/run.py                    paired transfers, validation and saved results
test/plot.py                   plotting entry point and sweep overview
test/analyze_scp.py / fig.py    original state-log analyzer and publication figures
.github/workflows/             Linux transfers and virtual-network CI
setup_scp_netns.sh             Linux network-namespace topology
papers/pacing/                 pacing manuscript source
test/research/                 native SCP pacing experiment configuration and runner
reproduction/                  paper-result reproduction guide, scripts and reference tables
docs/                          user documentation
```

## Connection state machine

```mermaid
stateDiagram-v2
    [*] --> CLOSED

    CLOSED --> SYN_SENT: send CONNECT
    CLOSED --> SYN_RECV: receive CONNECT / send CONNECT_ACK

    SYN_SENT --> ESTABLISHED: receive CONNECT_ACK / send ACK
    SYN_RECV --> ESTABLISHED: receive ACK

    ESTABLISHED --> ESTABLISHED: DATA / ACK / SACK
    ESTABLISHED --> FIN_WAIT: active close / send FIN+ACK
    ESTABLISHED --> LAST_ACK: receive FIN+ACK / send ACK+FIN

    FIN_WAIT --> CLOSED: receive FIN+ACK / send ACK
    LAST_ACK --> CLOSED: receive ACK

    FIN_WAIT --> FIN_WAIT: FIN timeout / retransmit
    LAST_ACK --> LAST_ACK: FIN timeout / retransmit
```

## Congestion control and pacing

### SCP-PROB

SCP-PROB combines a smoothed loss estimate with RTT measurements to form a residual congestion signal. A calibrated sigmoid and hysteresis determine whether its nonlinear control law increases or decreases the window.

The design target is transmission in lossy or variable environments. In the zero-random-loss configuration reported below, two PROB flows received unequal shares, and PROB received a very small share when competing with AIMD. These observations concern the tested PROB controller and configuration; they do not establish a failure of the AIMD controller or SCP's reliable-transport mechanisms.

### AIMD

AIMD is an independently selectable congestion controller using conventional additive increase and multiplicative decrease. It uses the same congestion-control interface as SCP-PROB, while applying its own window-update rules. It also serves as the baseline in the experiments below.

### Pacing

The pacing layer does not replace the congestion window:

1. `cwnd` and the peer window determine how much data may enter the network.
2. The sender derives a per-packet interval from the current RTT and congestion window.
3. Packets receive increasing scheduled timestamps.
4. A 1 ms pacing timer sends every packet whose scheduled time has arrived.

Multiple packets may be released in one timer tick when their calculated interval is below 1 ms or when the process wakes up late. The pacing integration test measures calls to the real transport `send` callback; it does not claim to measure physical wire-departure time after qdisc.

### Pacing discussion notes

The analysis asks how pacing changes ACK timing and window growth, and how that behavior depends on the pacing gain. Relevant ICCRG threads record the questions and implementation details considered during the work:

- [Slow-start timing and comparison models](https://mailarchive.ietf.org/arch/msg/iccrg/K0LZfeCUDnp3Kj91dMx-0iPGef8/), including the distinction between [fixed-batch and per-ACK scheduling](https://mailarchive.ietf.org/arch/msg/iccrg/GSzlNebpiu6Gp1UTRZJISBRiIi8/) and [window limits versus pacing-rate limits](https://mailarchive.ietf.org/arch/msg/iccrg/6K-oKt8LfaaH1XQKzp62wfElujc/).
- [Why Linux uses a slow-start pacing multiplier of 2](https://mailarchive.ietf.org/arch/msg/iccrg/ms7Nww2LvUBuDaUmOD71osmp8bI/). [Neal Cardwell's reply](https://mailarchive.ietf.org/arch/msg/iccrg/ERsHqvYQLs7cNCxOPw13U5Jl9sg/) provides paced/unpaced CUBIC timing diagrams and the BBR Startup gain derivation. The [follow-up question](https://mailarchive.ietf.org/arch/msg/iccrg/a59n7DRXZr4q6it_djAZh23wuas/) examines the assumptions behind a continuous-model calculation of `2 ln 2` and its relation to Linux's multiplier of 2.

## Build and tests

One CMake configuration builds all tests. Real UDP and virtual-network tests share the generated-data transfer, checksum exchange and close verification. No payload files are needed.

```sh
cmake -S test -B test/build -DCMAKE_BUILD_TYPE=Release
cmake --build test/build -j
ctest --test-dir test/build --output-on-failure
python3 test/run.py test/build/scp_udp --out test/results/udp-1g
```

The default standalone transfer is 1 GiB. See [test instructions](test/README.md) for dependencies, file transfers, network sweeps, and fairness measurements.

### Current test system

- **Functional and integrity checks:** CTest registers 12 checks covering connection lifecycle, control-packet loss, allocation failure, closure, generated-data and file transfers, PROB/AIMD operation, pacing observations, and two-flow smoke checks.
- **Shared transfer logic:** real UDP and virtual-network runs use the same transfer implementation. Generated data is checked byte by byte and by stream checksum; file runs additionally compare file lengths and SHA-256 hashes. Both endpoints must complete closure.
- **Linux and virtual transfers:** GitHub Actions runs CTest, a 1 GiB virtual transfer, and a 1 GiB file transfer over Linux localhost UDP. The localhost run has no imposed WAN bandwidth or delay; Linux namespaces provide a separate controlled-network setup.
- **Network scenarios:** the default CI sweep runs 150 virtual cases: three 100 Mbps scenarios with base RTTs of 10, 50, and 200 ms, ten independent packet-loss rates from 0% to 5%, five seeds, and 64 MiB per run. Manual `scenarios-1g` runs 36 cases at 1 GiB. These are configured simulations; the simplified Wi-Fi case does not model contention or link-layer retries.
- **Inspectable results:** each parameter combination and connection retains its own protocol-state logs and plots using the original analyzer. Reports include failures and successful-run goodput statistics. CI artifacts are `transfer-1g-curves` and `network-report`.

These tests provide evidence for the configurations exercised; they do not establish correctness for every network or turn simulation results into real-path measurements.

### Test architecture and environments

The harness separates the transfer workload from packet delivery. Both backends exercise the actual SCP core and reuse `test/common/transfer.c` for data generation, receive validation, checksum exchange, and closure. The virtual backend uses a seeded packet scheduler and simulated clock; the UDP backend uses sockets and real elapsed time. Python runners coordinate execution, retain diagnostics, and invoke the original plotting scripts on the core's JSON logs. The [test design and environment notes](test/README.md#test-design-and-environments) describe the layers and pass criteria.

| Environment | What it exercises |
|---|---|
| Virtual network | Repeatable rate, propagation delay, random loss, and protocol events without waiting for real elapsed time |
| Localhost UDP | Real sockets, Linux/Windows process scheduling, paired endpoints, and file I/O |
| Linux namespaces + netem | Real Linux UDP/IP and qdisc behavior on a controlled client–router–server path |
| Actual China-to-US path | Deployment experience on a real intercontinental route; separate from configured simulation scenarios |

Earlier netem experiments used `scp-c ↔ scp-r ↔ scp-s`, with a netem qdisc on each router egress. Each direction had its own rate, one-way delay, random loss, and finite packet queue. The historical results below used 5 Mbit/s per direction, 50 ms one-way delay (about 100 ms unloaded RTT), a 1000-packet queue, 100 MiB transfers, and 300-second fairness measurements. This exercises actual Linux networking under emulated impairments; it does not reproduce every feature of an Internet path. The current CI localhost transfer does not apply this netem topology, and the virtual model has no finite queue. Results from these environments should be reported separately.

For Linux namespaces, create the topology, then start B and A in separate terminals:

```sh
sudo bash setup_scp_netns.sh
sudo ip netns exec scp-s test/build/scp_udp B
sudo ip netns exec scp-c test/build/scp_udp A 10.0.2.1
```

## Earlier validation snapshot

The following results are a transparent snapshot from **2026-08-19**, not a multi-run benchmark or a general performance claim.

Environment:

```text
rate:             5 Mbit/s per direction
one-way delay:    50 ms
expected RTT:     approximately 100 ms
queue limit:      1000 packets
file size:        100 MiB
fairness period:  300 seconds
```

### Zero random loss

| Case | Observed result |
|---|---|
| PROB, single flow | 4.686 Mbit/s receiver goodput |
| AIMD, single flow | 4.746 Mbit/s receiver goodput |
| Pacing | 8 MiB verified; 2.200 ms average sender-callback gap; at most 4 DATA packets observed in one millisecond |
| PROB vs PROB | 4.735 Mbit/s total; Jain index 0.641; flow ratio 6.95:1 |
| PROB vs AIMD | 3.421 Mbit/s total; Jain index 0.504; PROB received 0.36% of delivered bytes |

The transferred 100 MiB file passed end-to-end hash verification.

### Ten percent random loss

| Case | Observed result |
|---|---|
| PROB vs PROB | 2.044 Mbit/s total; Jain index 0.999887; flow ratio 1.022:1 |

In this snapshot, the two PROB flows had near-equal shares at 10% random loss and unequal shares at zero random loss; aggregate goodput was lower in the loss case. PROB's allocation behavior and coexistence with AIMD need further investigation. The tables report controller-specific throughput and sharing separately from the transport's file-integrity checks.

In test output, `"status":"success"` records successful case execution and the applicable protocol/integrity checks. Throughput and fairness are reported as separate measurements.

## Analysis

Test builds enable the protocol's own JSON state printer, sampled every 100 ms.
`test/run.py` saves logs and plots automatically (requires matplotlib). Real Linux
file transfer and virtual-network runs are separate:

```sh
sudo python3 test/run.py test/build/scp_udp --netns --file testA.bin --timeout 3600 --out test/results/linux-file
python3 test/run.py test/build/scp_virtual --backend virtual --out test/results/virtual-1g
```

Create the namespace topology before the file run. The file test writes
`received.bin`, checks both files' SHA-256 and saves endpoint logs and curves.
See [test instructions](test/README.md) for setup and output locations.

## Research use and project experience

The author reports using SCP in Linux protocol-stack and network experiments, transferring 100 MB files on an actual China-to-US intercontinental path, and running a version on a microcontroller with 20 KB of memory. The repository also contains extensive automated transport tests. These experiences concern particular versions and environments; the microcontroller experience is not a memory requirement guaranteed by the current version, and the real intercontinental transfer is separate from the virtual scenarios above.

If you enjoy experimenting with networking, try SCP, inspect its behavior, and join the discussion. The project is a place to investigate transport mechanisms and compare designs. For products with strict reliability, security, maintenance, or interoperability requirements, consider established options such as lwIP, QUIC implementations, or KCP and evaluate them against your requirements.

SCP remains experimental: its API and wire format may change, it is not TCP-compatible, and the core provides no authentication or encryption. The historical PROB results above leave sharing and coexistence questions open. Automated tests, code review, and mathematical analysis each contribute different evidence.

## Contributing and discussion

[Issues](https://github.com/skaiui2/SCP/issues) are welcome for questions, experiment reports, bugs, and design discussions. For a reproducible report, include the commit, OS and build commands, controller, transfer size, network settings or virtual seed, expected and observed behavior, and relevant logs. Distinguish real-network measurements from simulations.

Before proposing a core algorithm or API change, discuss the intended behavior and a concrete approach in an issue. Read [AGENTS.md](AGENTS.md) and the [project contribution rules](FUCKAI/SKILL.md), particularly for AI-assisted work. Keep pull requests focused, explain what changes and why, and report the checks actually run with their results. Run the relevant CTest checks; network or performance claims should include their configuration, logs, and per-connection curves. Submit through [pull requests](https://github.com/skaiui2/SCP/pulls) for author review.

## AI assistance disclosure

The author, **Skai Uijing**, describes the division of work as follows:

- **Protocol code:** `scp.c` and `scp.h` were written primarily by the author. AI assisted with review and error checking, and with work on the pacing mechanism.
- **Tests and plotting:** most test code, including large-file transfer tests, was generated by AI and checked by the author for validity and suitability. The plotting scripts were also generated by AI.
- **SCP-PROB paper and control design:** the probability-driven design, residual algorithm, and the project's AIMD control-loop design are the author's work. This does not claim invention of AIMD itself. AI assisted with translation and writing, and with checking parts of the mathematical arguments, including Bayesian reasoning and fairness. Such assistance does not establish those claims as proved.
- **Pacing research:** the author proposed the sandwich comparison and the idea that very large pacing gain approaches burst sending, whereas too little gain slows window growth. The author also proposed investigating a gain that preserves window growth without increasingly concentrating packet transmissions. This is the motivation for the analysis, not a general guarantee for every pacer.
- **Further modeling and derivations:** GPT 6 ASTRA proposed the differential-equation analysis, rate-based modeling, and other extended analyses beyond the author's ideas described above. Because of the volume of generated material, the author has not yet independently verified all of these analyses and their derivations. Readers should critically assess their assumptions, proofs, and applicability, including those in the pacing manuscript. AI checking is not a substitute for the author's verification or independent review.

This README was edited with AI assistance. Reported experimental measurements come from runtime logs and file-integrity checks, not from AI-generated assertions. Code review, experimental evidence, and verification of mathematical claims are separate activities; none should be inferred from another.

## License

SCP is released under the [MIT License](./LICENSE).
