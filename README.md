# SCP slow start pacing study

This branch studies how pacing gain changes cwnd and DATA-packet spreading in
slow start. It executes the actual SCP C core with a fixed 100 ms feedback RTT
and zero loss. SS remains below ssthresh throughout the transfer. Experiment setup and read-only observations live in `test/research/`.
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
- [Historical completed Actions run 39697564304](https://github.com/skaiui2/SCP/actions/runs/39697564304)
- [Current branch workflow and new run status](https://github.com/skaiui2/SCP/actions/workflows/pacing-research.yml?query=branch%3Aresearch%2Fslowstart-nextslot-q16-20261022)
- [Current pacing paper](https://github.com/skaiui2/SCP/blob/main/Pacing_and_Congestion_Window_Growth.pdf) and [LaTeX source](https://github.com/skaiui2/SCP/tree/main/papers/pacing)

The preserved [2026-10-22 result interpretation](reproduction/README.md#current-results-and-the-python-difference)
records the earlier Python per-gap truncation and distinguishes packet-cohort
spans from fixed-RTT-bin spans.

Each transfer carries exactly 1 GiB = 1,073,741,824 bytes, starts at one MSS
(1430 bytes), and uses 50 ms in each network direction without serialization or
bandwidth queues. There are 22 fixed gains × three repetitions, plus three
separate native-policy controls. The native-policy control uses Q16 2*ln(2).
The shared controller code now uses fixed Q16 `(3/2)*ln(3)` in CA and throughout
PROB. CA now carries its byte numerator remainder; neither change alters this all-SS experiment.

The scheduler retains its next available slot: begin each batch at
`max(next_slot, now)`, assign the packet's timestamp, then add its own gap.
The virtual network defaults to 1 us ticks (`SCP_TICK_SCALE=1000U`) and delivers
packets to the actual peer SCP input path; scale 1 selects the 1 ms control.
Pacing wakes for the queue-head deadline rather than polling every tick. The rebuilt independent Python model advances by 1 us ticks, retains fractional
slots and dispatches their integer microsecond part. The stored 2026-10-22
comparison used the earlier per-gap truncation model. Gain representation and
the full-MSS payload convention remain distinct from SCP;
the reproduction guide separates the historical tables and current model commands.

This branch keeps only its own compact results. Full DATA/ACK logs can be
downloaded from Actions while available, or regenerated with the guide's commands.
The current paper follows continuous models → discrete packets → the author's
sandwich → Python → SCP. Formal papers and general project documentation are maintained on
[main](https://github.com/skaiui2/SCP/tree/main).
