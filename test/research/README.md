# SCP core pacing experiment

This directory runs the real SCP core through the baseline virtual transport. Python builds
and launches `scp_research`, checks native observations and plots them; it does not generate
packets, ACKs or congestion windows from a mathematical model.

## Shared implementation

Baseline: `b591e9c3c0279b8d95e925066d3e87d10e7f0df6`.
`scp.c`, `scp.h`, `lib/`, `test/common/`, `test/virtual/vnet.*`, the functional transfer entry
and the original plotting scripts must match that baseline. The runner checks these source
contents before every case. Research changes are this directory, one CMake include and the
research workflow. Functional targets keep their baseline defaults.

Actual call path:

`transfer_step -> scp_send -> scp_output -> scp_output_to_pacing -> scp_pacing_send_due
-> scp_output_data -> vnet_send -> vnet_step -> transfer_input -> scp_input`.

The receiving core emits its ACK through `vnet_send`. The same `vnet_step` delivers it to
the sender's `scp_input`; normal Reno slow start updates cwnd. The observer records sends,
arrivals and returns from input. It never modifies a window, flight, queue, clock or ACK.

## Configuration and timing

- 1 GiB generated payload with the baseline content/checksum implementation; 1430-byte
  payloads except the final tail, verified from actual DATA headers.
- Zero loss, seed 1, 50 ms per direction, no link serialization. Every DATA-to-ACK
  arrival/processing interval must be exactly 100 ms, checked against packet identity.
- Initial cwnd one MSS. The baseline Reno algorithm is unchanged. Compile-time ssthresh
  and cwnd cap are 2^31-1 bytes; advertised-window limits and application backlog are
  1 GiB. These permit uninterrupted slow start for this finite transfer. Measured receive
  window/backlog and end-of-transfer limits remain in the output, not silently ignored.
- The core uses Q32.32 ms planned timestamps and sends at their integer floor. The shared
  virtual loop advances 1 ms: arrivals first, core timers second, application steps last.
  Empty pacing queues start at the current time and retain no terminal gap.
- Each gain gets its own native binary, compiling the same core with
  `SCP_PACING_RECIPROCAL_Q32 = round(2^32/g_target)`. Both that integer and
  `g_effective = 2^32 / reciprocal` are reported. At the critical target this is
  3098164009 and approximately 1.386294361280859; it is not exactly irrational 2 ln 2.
- The configuration has 22 target gains, including 2 ln 2 and offsets of 0.05, with three
  deterministic repeats. Logs must reproduce byte-for-byte. No random error bars.

## Run and inspect

```sh
python test/research/run_scp_fixed_feedback.py --build-dir research-build \
  --gain-id critical --repeats 1 --bytes 8388608 --out results/preflight
python test/research/run_scp_fixed_feedback.py --build-dir research-build \
  --gain-id critical --out results/critical
python test/research/run_scp_fixed_feedback.py --merge results/downloads --out results/merged
```

The workflow first builds and runs the normal functional tests, then runs an 8 MiB core
preflight before the 22 gain jobs. Every job builds its gain binary and runs three 1 GiB
transfers. Failures preserve logs and stop that case; they are never replaced with model data.

Each case preserves native protocol logs, packet events including Q32.32 reservations,
RTT-bin histograms, packet-index-group spans, transfer/checksum summary, execution audit,
compiled gain, source and binary hashes. Build commands and compile inputs accompany them.
Original per-flow plots use `test/plot.py -> test/analyze_scp.py`.

The merged output includes all-gain cwnd curves, a near-critical intra-RTT zoom, L versus
RTT and L versus gain, millisecond cwnd/flight/reservation tables, boundary tables, and all
empty/partial RTT bins. Fixed bins are `[nR,(n+1)R)` relative to first actual DATA send.
Packet-index groups are separate: zero-based packet indices `[2q-1,4q-2]`, q a power of two.
The old paper's retained-gap finite formula is a labelled reference for those groups only.
The updated core is not asserted to implement that old mathematical scheduler.

Unit-test fixtures in `test_analysis.py` are synthetic analysis checks, not experiment data.
