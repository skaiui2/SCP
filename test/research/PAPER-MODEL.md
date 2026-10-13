# Fixed-feedback pacing model validation

This experiment follows `papers/pacing/Pacing_and_Congestion_Window_Growth.tex`.
It is an independent simulation of the paper model, not a protocol-stack
transfer benchmark. The earlier virtual-link experiment and its archived
measurements remain historical data; this workflow does not run that experiment.

Every actual send schedules one ACK exactly 100 ms later. The sender starts
with one packet at time zero, remains in slow start, increases its window by
one packet for each ACK, and always has application data available. There is
no serialization delay, network queue, loss, ACK aggregation, receiver-window
restriction, finite transfer size, or congestion-window cap. A packet is 1430
bytes for conversion of packet windows to byte windows.

The event simulator explicitly maintains flight, reserved packets, the window,
and the next free queue-tail slot. Existing reservations retain their times.
ACKs precede sends at a shared timestamp. New reservations use the current
window; the final spacing is retained after each batch. The simulator checks
`flight + reserved == window` and `flight <= window` after every event.
The clock jumps directly between events with double-precision RTT-unit times;
it does not round transmissions or ACKs to whole milliseconds. The 1 ms grid
is an observation grid only and does not drive the event schedule.

The analytical comparison independently generates packet times from the
paper's harmonic block solution. It compares every observed send timestamp,
the sampled window from analytical ACK counts, and each complete index-group
span. At and above `2*ln(2)`, it also checks the exact integer-RTT boundary
formula `w(n) = 1 + 2**(n-1)`. Finite-group spans are compared to the exact
formula; their limiting values are references, not finite-time equalities.

`paper_experiment.json` contains 22 gains and three repeats, giving 66 cases.
The critical region is `2*ln(2) + k*0.05` for `k=-6,...,6`, with additional
gains 1, 1.1, 1.75, 2, 2.5, 3, 4, 6, and 8. Each case records the expression
and actual floating-point gain. The horizon is 20 RTTs (2000 ms), rather than
a 1 GiB stopping condition. Deterministic repetitions should agree; they do
not estimate random-network variability.

Each run preserves a compressed per-packet send/ACK trace, sampled cwnd and
formula errors, RTT-bin first/last spans, exact packet-index-group spans,
configuration, paper hash, and source commit. RTT bins are `[kR,(k+1)R)`;
the initial singleton bin has no valid first-to-last dispersion. Boundary
windows are sampled after ACKs and sends at that timestamp. Bytes use binary
units when displayed as MiB.

Run unit checks with `python test/research/test_paper_model.py`. Run one gain
with `python test/research/paper_model.py --gain-id critical --out results/critical`.
Run the complete local sweep by omitting `--gain-id`, then merge with
`python test/research/paper_model.py --merge results --out report`.
The GitHub workflow first verifies the normal protocol fix at `dcdb49a`, then
runs the model cases and publishes consolidated CSV tables and PNG/PDF/SVG
figures. The normal source fix only changes the no-SRTT pacing fallback to
1 ms; the source's empty-queue scheduling rule is not changed.
