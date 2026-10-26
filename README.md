# SCP congestion avoidance pacing study

This branch studies how pacing gain changes cwnd and DATA-packet spreading in
congestion avoidance. It executes the actual SCP C core with a fixed 100 ms feedback RTT
and zero loss. CA starts with ssthresh = MSS and remains in congestion avoidance. Experiment setup and read-only observations live in `test/research/`.
The shared core, network and transfer code match baseline `ddea25420afa0fc6dbd4bbeb5cff35f99a21a503`,
including scalable ticks, deadline-based pacing wakeups and CA byte remainders.
The runner verifies those shared sources against the pinned commit and records
the experiment source and binary identities.
The committed compact results retain their original baseline `e41ac88` and source labels.

[中文说明](docs/中文/README中文.md) · [SCP project](https://github.com/skaiui2/SCP/tree/main)

- [Results, plotting scripts and reproduction steps](reproduction/README.md)
- [Code and execution-path review map](test/research/README.md)
- [Shared clock and CA arithmetic controls](test/README.md)
- [Experiment settings and the 22 gains](test/research/scp_fixed_experiment.json)
- [Historical completed Actions run 39697576064](https://github.com/skaiui2/SCP/actions/runs/39697576064)
- [Current branch workflow and new run status](https://github.com/skaiui2/SCP/actions/workflows/pacing-research.yml?query=branch%3Aresearch%2Fca-nextslot-q16-20261022)
- [Current pacing paper](https://github.com/skaiui2/SCP/blob/main/Pacing_and_Congestion_Window_Growth.pdf) and [LaTeX source](https://github.com/skaiui2/SCP/tree/main/papers/pacing)

The preserved [2026-10-22 result interpretation](reproduction/README.md#current-results-and-the-python-difference)
traces the native/Python window difference and the sawtooth between gains to
integer-byte CA updates, with a directly checkable ACK example.

Each transfer carries exactly 1 GiB = 1,073,741,824 bytes, starts at one MSS
(1430 bytes), and uses 50 ms in each network direction without serialization or
bandwidth queues. There are 22 fixed gains × three repetitions, plus three
separate native-policy controls. The current sweep centres on `C = 1.5 * ln(3)`,
with offsets down to 0.005 around C and wider points from 0.9 to 8.
The native-policy control uses fixed Q16 C (`107998 / 65536`); earlier stored
native-policy tables used `1 + MSS/(2*cwnd)`.

The earlier 1 ms, byte-floor fixed-CA native control completed three exact 1 GiB transfers with
matching checksums, 100 ms feedback for every packet, and no retransmissions or
drops. Each completes in 1339.38 RTTs. Complete groups 300-600 and 900-1200 have
actual L = 88 ms and zero accumulated start delay; the corresponding real-window
Python result is 99.569 ms in the late range. The [compact source-labelled
observations](https://github.com/skaiui2/SCP/blob/main/papers/pacing/data/ca-native-constant.csv)
are maintained with the common paper. The current workflow status above identifies
the new full sweep separately from this completed local control.

The scheduler retains its next available slot: begin each batch at
`max(next_slot, now)`, assign the packet's timestamp, then add its own gap.
The virtual network defaults to 1 us ticks (`SCP_TICK_SCALE=1000U`) and delivers
packets to the actual peer SCP input path; scale 1 selects the 1 ms control.
Pacing wakes for the queue-head deadline rather than polling every tick. The rebuilt independent Python model advances by 1 us ticks, retains fractional
slots and dispatches their integer microsecond part. The stored 2026-10-22
comparison used the earlier per-gap truncation model. Current native CA retains
the numerator remainder per ACK (`byte-remainder`); Python supports real,
byte-floor and byte-remainder controls. Gain quantization and the full-MSS tail
convention remain labelled separately. Historical tables keep their original clocks.

This branch keeps only its own compact results. Full DATA/ACK logs can be
downloaded from Actions while available, or regenerated with the guide's commands.
The current paper follows continuous models → discrete packets → the author's
sandwich → Python → SCP. Formal papers and general project documentation are maintained on
[main](https://github.com/skaiui2/SCP/tree/main).
