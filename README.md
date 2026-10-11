# SCP (Simple Stream Control Protocol)

[中文说明](./docs/中文/README中文.md)

SCP is a compact, stream-oriented reliable transport protocol implemented over UDP or another unreliable packet transport. It is designed for protocol research, user-space networking, embedded systems, and environments where the transport behavior must remain visible and easy to modify.

SCP provides sequencing, ACK/SACK processing, retransmission, flow control, connection management, RTT/RTO estimation, and congestion control without depending on a kernel TCP stack. The project currently contains two interchangeable congestion controllers:

- **SCP-PROB**: an experimental nonlinear controller that converts loss and RTT evidence into a continuous congestion-likelihood signal.
- **AIMD**: a conventional additive-increase, multiplicative-decrease controller, also used as the baseline in comparative experiments.

The current implementation also includes a sender-side pacing layer. Packets admitted by the congestion window are assigned transmission times derived from `cwnd` and RTT, then released by the pacing timer.

> **Project status:** SCP is under active development; its API and wire format may change. The transport core and the selectable congestion controllers are evaluated separately. The PROB-specific experimental results are documented below.

The probability-driven controller is described in [SCP-PROB: Probability-Driven Nonlinear Congestion Control](./SCP-PROB_Probability-Driven_Nonlinear_Congestion_Control.pdf).

The pacing study, [Pacing and Congestion-Window Growth: An Exact Packet-Level Analysis](./Pacing_and_Congestion_Window_Growth.pdf), analyzes a specified per-ACK append scheduler. It is a mathematical model, not an exact description of the current SCP implementation. [Pacing_and_Congestion_Window_Growth.tex](./papers/pacing/Pacing_and_Congestion_Window_Growth.tex) is the sole manuscript source: upload it to Overleaf and compile with pdfLaTeX. Proofs, references, and the analytical figure are all included; no experiment package or external figures are required.

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
test/                         shared UDP and virtual-network tests
setup_scp_netns.sh             reproducible network-namespace topology
test/plot.py                  curves and state-log analysis
docs/                          user documentation
```

The protocol core sends packets through a user-provided transport callback:

```c
struct scp_transport_class {
    int (*send)(void *user, const void *buf, size_t len);
    int (*recv)(void *user, void *buf, size_t maxlen);
    int (*close)(void *user);
    void *user;
};
```

This makes the core independent of a particular UDP wrapper or network device.

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

The default transfer is 1 GiB. See [test instructions](test/README.md) for network sweeps and fairness measurements.

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

## Intended uses and limitations

SCP is useful for:

- congestion-control and reliable-transport experiments;
- user-space or embedded transports over a custom unreliable medium;
- controlled internal communication and file-transfer experiments;
- teaching and inspecting transport-protocol mechanisms.

Current limitations include:

- the tested PROB configuration has unresolved sharing behavior at zero random loss, including when competing with AIMD;
- no production security layer, authentication, or encryption in the protocol core;
- no claim of TCP compatibility;
- no stable wire-format or API guarantee;
- no large-scale interoperability, adversarial, or production deployment audit.

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
