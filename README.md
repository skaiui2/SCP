# SCP (Simple Stream Control Protocol)

[中文介绍](./docs/中文/README中文.md)

SCP is a lightweight and predictable stream‑oriented reliable transport protocol built on top of UDP or any unreliable medium. It focuses on clarity, controllability, and a compact state machine suitable for embedded systems, MCU‑class devices, and user‑space networking.

Its core innovation is a nonlinear congestion‑control algorithm (SB) that combines loss deviation, RTT deviation, and their interaction into a single logistic‑mapped probability signal. This allows SCP to distinguish random loss from congestion, achieving stable throughput in high‑loss or wireless environments while keeping the implementation small, readable, and easy to extend.

## Features

- Small and readable implementation, easy to port or extend
- Reliable byte‑stream transport with ACK/SACK
- Timeout‑based retransmission and congestion control
- Full connection lifecycle (CONNECT / FIN)
- Tunable behavior for different environments
- Efficient reordering and timer management using red‑black trees
- **Nonlinear AIMD‑style congestion control** — uses loss deviation, RTT deviation, and their interaction to produce a logistic‑mapped congestion signal that drives a smooth, probability‑based window increase/decrease

## Design

```mermaid
classDiagram
    direction LR

    class Sender {
        + sends packets
        + receives ACKs
    }

    class Network {
        <<layer>>
        + delay
        + loss
        + reordering
        + queueing
    }

    class Receiver {
        + receives packets
        + sends ACKs
    }

    Sender --> Network : packets →
    Network --> Receiver : packets →
    Receiver --> Sender : ← ACK

```

### state machine

```mermaid
graph LR

    CLOSED((CLOSED))

    SYN_SENT((SYN_SENT))
    SYN_RECV((SYN_RECV))

    ESTABLISHED((ESTABLISHED))

    FIN_WAIT((FIN_WAIT))
    LAST_ACK((LAST_ACK))

    %% Handshake
    CLOSED -->|send CONNECT| SYN_SENT
    CLOSED -->|RCV CONNECT / send CONNECT_ACK| SYN_RECV

    SYN_SENT -->|RCV CONNECT_ACK / send ACK| ESTABLISHED
    SYN_RECV -->|RCV ACK| ESTABLISHED

    %% Data transfer
    ESTABLISHED -->|RCV DATA / RCV ACK| ESTABLISHED

    %% Active close
    ESTABLISHED -->|send FIN+ACK| FIN_WAIT

    %% Passive close
    ESTABLISHED -->|RCV FIN+ACK / send ACK+FIN| LAST_ACK

    %% FIN_WAIT transitions
    FIN_WAIT -->|RCV FIN+ACK/ send ACK| CLOSED

    %% LAST_ACK transitions
    LAST_ACK -->|RCV ACK| CLOSED

    %% FIN retransmission loops
    FIN_WAIT -->|t_fin timeout / retransmit FIN| FIN_WAIT
    LAST_ACK -->|t_fin timeout / retransmit FIN| LAST_ACK
```

## Performance

**nodeB:**

![SCP B_seq](test/nodeB/output/seq.png)

**Transmitted bytes for nodeB**

![SCP B_packet_bytes](test/nodeB/output/packet_bytes.png)

**cwnd for nodeB**

![SCP B_cwnd](test/nodeB/output/cwnd.png)

**RTT for nodeB**

![SCP B_srtt](test/nodeB/output/srtt.png)

**cong_q for nodeB**

![SCP B_cong_q](test/nodeB/output/cong_q.png)



## Use Cases

SCP is suitable for scenarios requiring reliable, controllable, and lightweight transport over UDP or other unreliable channels, such as internal service communication, large file transfer in embedded/RTOS environments, and synchronization between game servers or real‑time systems.

## Getting Started

SCP requires only two core files plus a small data‑structure library:

```
scp.h
scp.c
```

Supporting data structures:

```
lib/
    rbtree.c / rbtree.h
    hashmap.c / hashmap.h
    queue.c / queue.h
```

SCP can run on top of any UDP transport. You only need to provide a simple send callback to integrate it into your system.

## Running the Test Program

The repository includes a bidirectional 100 MB file‑transfer test, which logs all protocol events in JSON format. A Python script is provided to visualize sequence evolution, congestion window behavior, retransmissions, and throughput.

### Prepare the test environment

Use `tc netem` to simulate a weak‑network environment:

```bash
sudo tc qdisc replace dev lo root netem \
    delay 20ms 5ms \
    loss 0.5% \
    reorder 5% 50% \
    rate 50mbit \
    limit 500
```

### Clone the repository

```bash
git clone https://github.com/skaiui2/SCP.git
cd SCP/test
```

### Build and run the test nodes

Two programs are provided: **nodeA** and **nodeB**, each sending a 100 MB file to the other.
 Open two terminals.

**Terminal 1 — start nodeB:**

```bash
cd nodeB
mkdir build
cd build
cmake ..
make
./nodeB > nodeB.log
```

**Terminal 2 — start nodeA:**

```bash
cd nodeA
mkdir build
cd build
cmake ..
make
./nodeA > nodeA.log
```

### Verify file integrity

After both transfers complete, place the four generated files in the same directory and verify their checksums.
 All files contain repeating bytes from 0–255, so their MD5 values should match:

```bash
md5sum testA.bin testB.bin outA.bin outB.bin
14d349e71547488a2a21c99115a3260d  testA.bin
14d349e71547488a2a21c99115a3260d  testB.bin
14d349e71547488a2a21c99115a3260d  outA.bin
14d349e71547488a2a21c99115a3260d  outB.bin
```

### Generate analysis plots

```
python3 analyze_scp.py nodeX.log
```

The script will generate several plots, including:

- Sequence number evolution
- Total transmitted bytes and bandwidth usage
- Congestion window dynamics

Example outputs (included in the repository under `test/nodeX/output/`):

**Sequence evolution**

![SCP A_seq](test/nodeA/output/seq.png)

**Transmitted bytes**

![SCP A_packet_bytes](test/nodeA/output/packet_bytes.png)

**Congestion window**

![SCP A_cwnd](test/nodeA/output/cwnd.png)

These figures illustrate SCP’s behavior under delay, jitter, packet loss, and reordering, including visible cwnd drops caused by timeout‑driven retransmissions.

