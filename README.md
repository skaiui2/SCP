# SCP (Simple Stream Control Protocol)

[中文介绍](./docs/中文/README中文.md)

SCP is a lightweight, controllable, and stable stream‑oriented protocol built on top of UDP or any unreliable transport. Its goal is to provide a simple and maintainable alternative in scenarios where TCP is too heavy and KCP is too aggressive or unpredictable.

SCP is lighter than a full TCP stack and far easier to customize or extend. Compared with aggressive protocols like KCP, SCP behaves more predictably and wastes significantly less bandwidth, making it well‑suited for controlled or high‑quality internal networks.

It implements sequence numbers, ACK, SACK, retransmission, flow control, congestion control, connection setup, and graceful shutdown, while keeping the codebase small and the state machine easy to understand. This makes SCP suitable for user‑space networking, embedded systems, and internal service communication.

## Features

- Reliable byte‑stream transport over an unreliable medium
- Clean and readable implementation, easy to port or modify
- SACK, timeout‑based retransmission, flow control, and congestion control
- Full connection semantics (CONNECT / FIN)
- Tunable behavior: retransmission, window strategy, and aggressiveness can be adjusted per scenario
- Red‑black tree–based reordering and timer management for better performance under packet disorder

## Performance

In a realistic weak‑network test environment with delay, jitter, light packet loss, reordering, and bandwidth limits (20 ms ± 5 ms latency, 0.5% loss, 5% reordering, 50 Mbps rate, 500‑packet queue), SCP achieves throughput comparable to aggressive protocols like KCP while keeping bandwidth waste around 10%, significantly better than KCP’s typical overhead under the same conditions.

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
