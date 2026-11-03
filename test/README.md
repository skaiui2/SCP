# SSCP Testing and Reproduction Guide

[测试说明 (中文)](../docs/中文/测试说明.md) · [User Guide](../docs/English/UserGuide.md) · [README](../README.md)

This guide explains how to run SSCP's automated tests, reproduce network experiment scenarios under emulated impairments, and visualize protocol traces with jPlot.

## Test Environments

| Environment | Purpose | Harness & Backend |
|---|---|---|
| **Deterministic Virtual Network** | Verifies transport correctness, retransmissions, state transitions, and memory cleanup without physical elapsed time | `sscp_virtual` + `vnet.c` (1 μs simulated tick) |
| **Real Localhost UDP** | Tests OS socket I/O, process scheduling, and bulk file delivery | `sscp_udp` + `run.py` (real-time clock) |
| **Linux Network Namespaces** | Tests against the real Linux kernel network stack and qdisc netem impairments (loss, delay, rate limiting) | `setup_sscp_netns.sh` + `ip netns` |

## Building and Running Tests

### 1. Build Test Targets

```sh
cmake -S test -B test/build -DCMAKE_BUILD_TYPE=Release
cmake --build test/build -j
```

### 2. Run Virtual Network Tests (CTest)

Execute the 11 registered virtual tests:

```sh
ctest --test-dir test/build -L virtual --output-on-failure
```

CTest suites cover:
- Handshake ACK loss and recovery
- Connection lifecycle and immediate closure
- Out-of-order receive buffer sequence wrap
- Pacing startup timing
- High random loss recovery and FNV-1a checksum verification
- Behavior of AIMD and PROB controllers under fixed feedback

### 3. Run Real UDP Paired Transfers

Use `test/run.py` to execute coordinated paired transfers and verify checksums:

```sh
# 8 MiB transfer
python3 test/run.py test/build/sscp_udp --bytes 8388608 --out test/results/udp-test
```

### 4. Linux Netem Impairment Experiments

Set up client (`sscp-c`), router (`sscp-r`), and server (`sscp-s`) network namespaces:

```sh
# Configure: 5 Mbps rate, 50 ms one-way delay (100 ms RTT), 10% loss, 1000-packet queue limit
sudo bash setup_sscp_netns.sh setup 5mbit 50ms 10% 1000

# Terminal 1: Receiver (B)
sudo ip netns exec sscp-s test/build/sscp_udp B 10.0.2.1 104857600 aimd

# Terminal 2: Sender (A)
sudo ip netns exec sscp-c test/build/sscp_udp A 10.0.2.1 104857600 aimd

# Clean up topology
sudo bash setup_sscp_netns.sh teardown
```

## Trace Visualization with jPlot

SSCP transfers produce runtime event logs. Convert them into `.xpl` / `.tsg` files for interactive analysis:

```sh
python3 test/sscptrace.py test/results/udp-test -o test/results/udp-test/trace.xpl
java -jar jPlot.jar test/results/udp-test/trace.xpl
```

jPlot provides:
- Seamless timeline zooming and panning
- Visual separation of original sends and retransmissions
- Real-time tracking of `cwnd` growth and SRTT evolution
