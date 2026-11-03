# SSCP User Guide

[使用手册 (中文)](../中文/使用手册.md) · [README](../../README.md) · [Testing Guide](../../test/README.md)

SSCP is a lightweight, stream-oriented reliable transport protocol implemented over UDP. It provides in-order byte stream delivery, flow control, acknowledgments, retransmissions, and congestion control.

## Clock Requirements

SSCP requires a monotonically increasing `uint32_t` clock in ticks. `SSCP_TICK_SCALE` defines ticks per millisecond:
- Real UDP / standard transports default to `1U` (1 ms per tick).
- Deterministic virtual network simulation defaults to `1000U` (1 μs per tick).

```c
extern uint32_t sscp_now_time(void);
```

Physical timeout constants scale with the clock:
- Initial retransmission timeout (RTO) is 1000 ms.
- Dynamic bounds clamp RTO between 200 ms and 2000 ms once RTT samples are observed.
- Before an SRTT sample exists, pacing falls back to a 1 ms interval.

## Pacing Policy

Packets admitted by `cwnd` and peer receive window are scheduled with per-packet intervals:
`gap = payload_bytes * RTT / (gain * cwnd)`

- Batches start at `max(next_slot, now)`.
- Each packet advances the persistent `next_slot` by its interval.
- AIMD uses fixed gains: Q16 `90852` (`2 ln 2`) in Slow Start, and Q16 `107998` (`3 ln 3 / 2`) in Congestion Avoidance.
- PROB uses the CA gain throughout.

## API Reference

### 1. Engine Initialization

```c
int sscp_init(size_t max_streams);
```
Initializes the global SSCP subsystem. Must be called once before allocating streams.

### 2. Stream Allocation and Binding

```c
struct sscp_stream *sscp_stream_alloc(struct sscp_transport_class *st_class,
                                      int src_fd,
                                      int dst_fd);
```
Allocates an SSCP stream and binds it to packet I/O callbacks (`send`, `recv`, `close`).
`src_fd` is a local protocol identifier, and `dst_fd` is the peer's identifier.

### 3. Controller Selection

```c
int sscp_set_cc(int fd, enum sscp_cc_id cc_id);
int sscp_prob_configure(struct sscp_stream *ss, uint32_t rtt_span_ms, uint32_t loss_q16);
```
Selects `SSCP_CC_AIMD` or `SSCP_CC_PROB` while the stream is in `SSCP_CLOSED`.

### 4. Connection Establishment

```c
int sscp_connect(int fd);
```
Initiates the 3-way handshake with the remote endpoint.

### 5. Input Packet Processing

```c
int sscp_input(void *ctx, void *buf, size_t len);
```
Passes an incoming UDP packet into the SSCP engine for parsing, sequencing, ACK generation, and state transitions.

### 6. Sending Data

```c
int sscp_send(int fd, void *buf, size_t len);
```
Queues user payload data into the transmission buffer.
- Returns `0` on success.
- Returns `SSCP_ERR_NOBUF` (-2) if send buffers are full; retry after progress.
- Returns `SSCP_ERR_GENERIC` (-1) on invalid stream or invalid state.

### 7. Receiving Data

```c
int sscp_recv(int fd, void *buf, size_t len);
```
Reads sequenced payload data from the receive buffer. Returns bytes copied, or `0` if no data is available.

### 8. Connection Teardown

```c
void sscp_close(int fd);
int sscp_is_closed(int fd);
```
`sscp_close` triggers graceful closure (FIN handshake) after all queued data is delivered.
`sscp_is_closed` checks whether the stream has reached the closed state.

### 9. Timer Processing

```c
void sscp_timer_process(void);
```
Processes active timers (pacing releases, retransmissions, keepalive). Must be called periodically by the event loop.

## Threading Model

SSCP is designed for single-threaded event loops. When accessing streams across multiple threads, serialize all API calls (including `sscp_timer_process`) using an external lock.
