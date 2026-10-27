# SCP User Guide

[中文手册](../中文/使用手册.md) · [Project overview](../../README.md) · [Tests and reproduction](../../test/README.md)

SCP is a lightweight reliable byte‑stream transport layer that provides sequencing, acknowledgments, retransmission, flow control, out‑of‑order reassembly, and congestion control. It is independent of the underlying link and independent of the upper‑layer protocol; its only responsibility is reliable delivery.

## Clock Requirements

Provide a monotonically advancing `uint32_t` clock in ticks. `SCP_TICK_SCALE` is ticks per millisecond: the core and real UDP default to `1U` (1 ms); virtual tests default to `1000U` (1 μs). CMake's `SCP_TEST_TICK_SCALE` selects the virtual scale only. All compilation units must agree. Unsigned wrap is handled by sequence comparisons; do not reset the clock during a connection.

```c
extern uint32_t scp_now_time(void);
```

Physical timeout constants scale with the clock. Initial RTO is 1000 ms; after an RTT sample, the existing estimator uses the 200–2000 ms bounds. This is separate from pacing's 1 ms fallback before SRTT exists. `scp_prob_configure()` still takes physical milliseconds.

## Pacing policy

Packets admitted by the congestion and receiver windows are paced with `gap = payload_bytes * RTT / (gain * cwnd)`. A batch starts at `max(next_slot, now)`; each packet receives that slot before the slot advances by its gap. The trailing slot survives an empty queue, so later packets respect the previous packet's pacing interval.

AIMD uses fixed phase gains: Q16 `90852` (`2 ln 2`) in SS and Q16 `107998` (`3 ln 3 / 2`) in CA. PROB uses the CA gain throughout. The effective CA value is `107998 / 65536 = 1.647918701171875`. These gains are supplied by each controller's callback. Before an RTT sample exists, pacing uses a 1 ms fallback; regular timer service is still required.

The [pacing paper](../../Pacing_and_Congestion_Window_Growth.pdf) explains the continuous models, discrete packet timing and sandwich argument behind this policy. The [test guide](../../test/README.md#model-and-execution-contract) distinguishes the Python model, actual SCP virtual-network execution and real UDP measurements.

## API Overview

### Initialization

```c
int scp_init(size_t max_streams);
```

Initializes the global SCP subsystem. This must be called once.

### Stream Allocation

```c
struct scp_stream *scp_stream_alloc(struct scp_transport_class *st_class,
                                    int src_fd,
                                    int dst_fd);
```

Creates a new SCP stream and binds it to a transport class.

**Note:** 
 `src_fd` is a unique local protocol identifier, and `dst_fd` identifies the corresponding remote stream; neither is an OS socket handle. Keep the transport object and its user context alive while the stream can access them. Allocate the passive stream before starting the active connection.

### Controller selection

```c
int scp_set_cc(int fd, enum scp_cc_id cc_id);
int scp_prob_configure(struct scp_stream *ss,
                       uint32_t rtt_span_ms, uint32_t loss_q16);
```

Choose `SCP_CC_AIMD` or `SCP_CC_PROB` before connecting, while the stream is `SCP_CLOSED`. Allocation currently defaults to PROB; test runners select their controller explicitly. Switching after establishment is rejected. PROB calibration takes a positive RTT span in milliseconds and a Q16 loss fraction from 0 to 65536; it does not configure network packet loss.

### Stream Free

```c
int scp_stream_free(struct scp_stream *ss);
```

Immediately releases a stream and its timers and buffers; this is not graceful closure. Normal protocol closure or timeout can free the stream automatically. Do not dereference stale pointers or free them twice.

### Connection Establishment

```c
int scp_connect(int fd);
```

Initiates the SCP handshake with the remote endpoint.

### Input Processing

```c
int scp_input(void *ctx, void *buf, size_t len);
```

Feeds a complete SCP packet into the protocol engine. SCP handles sequencing, ACK processing, retransmission, and state transitions internally.

### Sending Data

```c
int scp_send(int fd, void *buf, size_t len);
```

Queues a copy after establishment. Return `0` means the entire input was accepted, not a byte count and not peer delivery. `SCP_ERR_NOBUF` (-2) means retain the application data and retry after progress. `SCP_ERR_NOMEM` (-3) leaves no partial input enqueued. `SCP_ERR_GENERIC` (-1) indicates invalid input/state. The shared test workload bounds application backlog to about 1 MiB independently of cwnd.

### Receiving Data

```c
int scp_recv(int fd, void *buf, size_t len);
```

Returns the number of bytes copied, zero if nothing was copied, or a negative result for a missing/invalid stream. Zero alone is not EOF. Consume data regularly to reopen the advertised receive window.

### Closing a Stream

```c
void scp_close(int fd);
```

Requests closure after queued data drains and rejects further sends. Continue servicing input and timers; closure is asynchronous.

```c
int scp_is_closed(int fd);
```

This is also true for a missing stream and a newly allocated, not-yet-connected stream. Track whether the connection has started before using it to recognize completion.

### Timer Processing

```c
void scp_timer_process(void);
```

Processes all SCP timers, including pacing, retransmission, keepalive, and persist timers.
Call it regularly in the SCP thread even when no packets arrive; input-only service can stall sends. Pacing now arms the timer at the head packet's due integer tick instead of polling every tick. The caller still supplies service; a fine clock does not itself guarantee fine wakeups. Late service can release several packets together. The virtual test clock advances one tick at a time, without event-time jumps.

### Configuration Macros

Review [scp.h](../../scp.h) for timeout, window and memory limits. Only macros guarded by `#ifndef` support direct compiler overrides. Compile the core and its integration consistently. AIMD CA retains the remainder of `(MSS * acked + cwnd_cnt) / cwnd`, resetting credit on initialization, loss and timeout; the old byte-floor update is historical, not current behavior.

## Multithreading Notes

SCP is designed for single‑threaded use.
Use one execution thread or serialize all calls, including timer processing, with the same external lock. Locking alone does not solve callback reentrancy. Runnable integrations are in `test/common/udp_runner.c` and `test/virtual/vnet.c`; the [test guide](../../test/README.md) covers build, diagnostics, failure reproduction and artifacts. Record source revision, clock scale, controller and network configuration with every report.
