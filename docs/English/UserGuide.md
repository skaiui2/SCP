## SCP User Guide

SCP is a lightweight reliable byte‑stream transport layer that provides sequencing, acknowledgments, retransmission, flow control, out‑of‑order reassembly, and congestion control. It is independent of the underlying link and independent of the upper‑layer protocol; its only responsibility is reliable delivery.

## Clock Requirements

SCP requires a millisecond‑resolution clock. You only need to implement a function that returns a monotonically increasing `uint32_t` timestamp. A common approach is to maintain a global counter that increments every millisecond.

```c
extern uint32_t scp_now_time(void);
```

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
 `fd` is a user‑assigned identifier. It must be unique per node at any given time.

### Stream Free

```c
int scp_stream_free(struct scp_stream *ss);
```

Releases a stream and its associated resources.

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

Sends application data through the specified stream.

### Receiving Data

```c
int scp_recv(int fd, void *buf, size_t len);
```

Reads data from the receive queue of the specified stream.

### Closing a Stream

```c
void scp_close(int fd);
```

Initiates the FIN handshake and closes the stream.

### Timer Processing

```c
void scp_timer_process();
```

Processes all SCP timers, including pacing, retransmission, keepalive, and persist timers.
Call it periodically in the SCP thread even when no packets arrive; processing it only after input can stall scheduled sends. A 1 ms service interval matches the pacing timer's granularity.

### Configuration Macros

All configuration macros in the header file may be tuned to fit your environment (timeouts, window sizes, limits, etc.).

## Multithreading Notes

SCP is designed for single‑threaded use.
If you need to use SCP from multiple threads, protect all SCP API calls with a global lock to ensure thread safety.
