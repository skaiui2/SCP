"""Independent packet model; it does not execute or replace the SCP core."""
import argparse
import csv
import hashlib
import math
from collections import deque
from dataclasses import dataclass
from pathlib import Path

MSS = 1430
RTT = 100
TICKS_PER_MS = 1000
RTT_TICKS = RTT * TICKS_PER_MS
GAINS = sorted([2 * math.log(2) + k * 0.05 for k in range(-6, 7)]
               + [1, 1.1, 1.75, 2, 2.5, 3, 4, 6, 8])
CA_GAINS = sorted([1.5 * math.log(3) + d for d in
                   [-.10, -.05, -.02, -.01, -.005, 0, .005, .01, .02, .05, .10]]
                  + [.9, 1, 1.2, 1.4, 1.5, 1.9, 2, 2.5, 3, 4, 8])
EVENT_ORDER = "DATA arrivals; ACK arrivals (output sends due); sender timer"


@dataclass
class Packet:
    number: int
    generation: int
    reserved: float
    gap: float
    window: float
    gain: float
    sent: int = -1


@dataclass
class Span:
    first: int = -1
    last: int = -1
    count: int = 0
    final: bool = False
    tail_slot: float = 0
    first_reserved: float = 0
    last_reserved: float = 0
    tail_gap: float = 0
    max_gap: int = 0
    bucket: int = -1
    bucket_count: int = 0
    max_1ms: int = 0

    def observe(self, packet, now, total):
        if self.count:
            self.max_gap = max(self.max_gap, now - self.last)
        else:
            self.first = now
            self.first_reserved = packet.reserved
        self.last = now
        self.count += 1
        self.final |= packet.number + 1 == total
        self.tail_slot = packet.reserved + packet.gap
        self.last_reserved = packet.reserved
        self.tail_gap = packet.gap
        bucket = now // TICKS_PER_MS
        self.bucket_count = self.bucket_count + 1 if bucket == self.bucket else 1
        self.bucket = bucket
        self.max_1ms = max(self.max_1ms, self.bucket_count)


class Sender:
    def __init__(self, mode, gain, total, network, spacing, initial_mss):
        self.mode, self.fixed_gain, self.total = mode, gain, total
        self.network, self.spacing = network, spacing
        self.cwnd = float(initial_mss)
        self.flight = self.scheduled = self.acked = self.zero_gaps = 0
        self.next_send_time = 0.0
        self.pending = deque()
        self.update_gap()

    def update_gap(self):
        if self.fixed_gain is not None:
            self.gain = self.fixed_gain
        elif self.mode == "ss":
            self.gain = 2 * math.log(2)
        else:
            self.gain = 1.5 * math.log(3)
        gap = RTT_TICKS / (self.gain * self.cwnd)
        self.gap = int(gap) if self.spacing == "gap-us" else gap

    def try_send(self, generation):
        self.next_send_time = max(self.next_send_time, self.network.tick)
        while self.scheduled < self.total and self.flight + 1 <= self.cwnd:
            packet = Packet(self.scheduled, generation, self.next_send_time,
                            self.gap, self.cwnd, self.gain)
            self.scheduled += 1
            self.flight += 1
            self.zero_gaps += self.gap == 0
            self.pending.append(packet)
            self.next_send_time += self.gap
            assert self.next_send_time == packet.reserved + packet.gap
            assert self.flight == self.scheduled - self.acked <= self.cwnd
            self.network.record("RESERVE", packet, packet.reserved)
        self.send_due()

    def send_due(self):
        while self.pending and int(self.pending[0].reserved) <= self.network.tick:
            packet = self.pending.popleft()
            assert packet.number == self.network.sent
            packet.sent = self.network.tick
            self.network.send_data(packet)

    def receive_ack(self, packet):
        assert packet.number == self.acked
        assert self.network.tick - packet.sent == RTT_TICKS
        self.flight -= 1
        self.acked += 1
        self.cwnd += 1 if self.mode == "ss" else 1 / self.cwnd
        self.update_gap()
        self.network.expected_groups[packet.generation + 1] = math.floor(self.cwnd)
        self.network.record("ACK_RECEIVED", packet, self.network.tick)
        self.try_send(packet.generation + 1)


class Receiver:
    def __init__(self, network):
        self.network, self.received = network, 0

    def receive_data(self, packet):
        assert packet.number == self.received
        assert self.network.tick - packet.sent == RTT_TICKS // 2
        self.received += 1
        self.network.record("DATA_RECEIVED", packet, self.network.tick)
        self.network.acks.append((self.network.tick + RTT_TICKS // 2, packet))
        self.network.record("ACK_SENT", packet, self.network.tick + RTT_TICKS // 2)


class Network:
    def __init__(self, trace=None):
        self.tick = self.sent = 0
        self.data, self.acks = deque(), deque()
        self.trace = trace
        self.windows, self.rtts, self.generations, self.expected_groups = {}, {}, {}, {}

    def state(self):
        a = self.sender
        assert a.flight == (self.sent - a.acked) + len(a.pending)
        return (a.cwnd * MSS, a.flight * MSS,
                (self.sent - a.acked) * MSS, len(a.pending) * MSS)

    def record(self, event, packet, due):
        if self.trace:
            self.trace.writerow((event, milliseconds(self.tick), packet.number * MSS,
                                 MSS, packet.generation, milliseconds(due),
                                 milliseconds(packet.sent) if packet.sent >= 0 else "",
                                 *self.state(), self.sender.gain, packet.gain,
                                 packet.window * MSS, packet.reserved, packet.gap,
                                 packet.reserved + packet.gap, self.sender.next_send_time))

    def send_data(self, packet):
        self.sent += 1
        self.record("SEND", packet, packet.reserved)
        self.data.append((self.tick + RTT_TICKS // 2, packet))
        for table, key in ((self.rtts, self.tick // RTT_TICKS),
                           (self.generations, packet.generation)):
            table.setdefault(key, Span()).observe(packet, self.tick, self.sender.total)

    def step(self):
        while self.data and self.data[0][0] <= self.tick:
            _, packet = self.data.popleft()
            self.receiver.receive_data(packet)
        while self.acks and self.acks[0][0] <= self.tick:
            _, packet = self.acks.popleft()
            self.sender.receive_ack(packet)
        self.sender.send_due()

    def run(self):
        while self.sender.acked < self.sender.total:
            self.step()
            if self.tick % RTT_TICKS == 0:
                self.windows[self.tick // RTT_TICKS] = (*self.state(), self.sender.acked)
            if self.sender.acked < self.sender.total:
                self.tick += 1
        assert self.sender.scheduled == self.sent == self.receiver.received == self.sender.acked
        assert self.sender.flight == 0 and not self.sender.pending and not self.data and not self.acks


def milliseconds(ticks):
    return f"{ticks / TICKS_PER_MS:.3f}"


def simulate(mode, gain, packets, trace=None, spacing="accumulated", initial_mss=1):
    network = Network(trace)
    network.sender = Sender(mode, gain, packets, network, spacing, initial_mss)
    network.receiver = Receiver(network)
    network.expected_groups[0] = initial_mss
    network.sender.try_send(0)
    network.run()
    return network


def write_tables(directory, network):
    with (directory / "rtt.csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("rtt", "cwnd_bytes", "flight_bytes", "sent_unacked_bytes", "queued_bytes", "acked_packets",
                         "first_send_ms", "last_send_ms", "span_ms", "packets", "last_data_bin",
                         "max_1ms_packets", "max_1ms_share", "max_internal_gap_ms"))
        for n in sorted(network.windows.keys() | network.rtts.keys()):
            span = network.rtts.get(n, Span())
            times = tuple(milliseconds(t) for t in (span.first, span.last, span.last - span.first)) if span.count else ("", "", "")
            writer.writerow((n, *network.windows[n], *times, span.count, span.final, span.max_1ms,
                             span.max_1ms / span.count if span.count else "", milliseconds(span.max_gap)))
    with (directory / "generations.csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("generation", "first_send_ms", "last_send_ms", "span_ms", "span_over_rtt",
                         "packets", "contains_last_packet", "complete", "expected_packets", "tail_slot_ms",
                         "occupancy_ms", "start_drift_ms", "next_start_extra_ms", "first_reserved_ms",
                         "last_reserved_ms", "reservation_span_ms", "tail_gap_ms", "tail_residual_ms"))
        for n, span in sorted(network.generations.items()):
            following = network.generations.get(n + 1)
            extra = milliseconds(following.first - span.first - RTT_TICKS) if following else ""
            writer.writerow((n, milliseconds(span.first), milliseconds(span.last),
                             milliseconds(span.last - span.first),
                             (span.last - span.first) / RTT_TICKS, span.count, span.final,
                             span.count == network.expected_groups[n], network.expected_groups[n],
                             span.tail_slot / TICKS_PER_MS, (span.tail_slot - span.first) / TICKS_PER_MS,
                             milliseconds(span.first - n * RTT_TICKS), extra,
                             span.first_reserved / TICKS_PER_MS, span.last_reserved / TICKS_PER_MS,
                             (span.last_reserved - span.first_reserved) / TICKS_PER_MS,
                             span.tail_gap / TICKS_PER_MS, (span.tail_slot - span.last) / TICKS_PER_MS))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("ss", "ca"), default="ca")
    gain_choice = parser.add_mutually_exclusive_group()
    gain_choice.add_argument("--gain", type=float, help="one fixed gain instead of the 22-gain grid")
    gain_choice.add_argument("--gain-grid", choices=("standard", "ca-critical"), default="standard")
    gain_choice.add_argument("--policy", action="store_true", help="unquantized phase policy: SS 2 ln(2), CA 1.5 ln(3)")
    parser.add_argument("--spacing", choices=("accumulated", "gap-us"), default="accumulated")
    parser.add_argument("--initial-mss", type=int, choices=range(1, 21), default=1)
    parser.add_argument("--bytes", type=int, default=1 << 30, help="requested size, rounded up to full MSS packets")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--trace", action="store_true")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.gain_grid == "ca-critical" and args.mode != "ca":
        parser.error("ca-critical gain grid requires --mode ca")
    grid = CA_GAINS if args.gain_grid == "ca-critical" else GAINS
    gains = [None] if args.policy else grid if args.gain is None else [args.gain]
    if args.bytes <= 0 or args.repeats <= 0 or any(g is not None and (not math.isfinite(g) or g <= 0) for g in gains):
        parser.error("bytes and repeats must be positive; fixed gains must be finite and positive")
    packets = (args.bytes + MSS - 1) // MSS
    args.out.mkdir(parents=True, exist_ok=False)
    source_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    with (args.out / "summary.csv").open("w", newline="") as stream:
        summary = csv.writer(stream)
        summary.writerow(("backend", "mode", "gain", "repeat", "requested_bytes", "packets", "transferred_bytes",
                          "mss", "rtt_ms", "tick_ms", "pacing_rounding", "last_ack_ms", "final_cwnd_bytes",
                          "reservation_rule", "script_sha256", "gain_policy", "initial_mss", "initial_train",
                          "event_order", "old_model_replay", "zero_gap_reservations"))
        for index, gain in enumerate(gains, 1):
            for repeat in range(1, args.repeats + 1):
                directory = args.out / f"g{index:02d}-r{repeat:02d}"
                directory.mkdir()
                events = (directory / "events.csv").open("w", newline="") if args.trace else None
                try:
                    trace = csv.writer(events) if events else None
                    if trace:
                        trace.writerow(("event", "now_ms", "seq", "bytes", "generation", "due_ms", "sent_ms",
                                        "cwnd_bytes", "flight_bytes", "sent_unacked_bytes", "queued_bytes", "current_gain",
                                        "reservation_gain", "reservation_cwnd_bytes", "reserved_us", "gap_us",
                                        "packet_tail_slot_us", "next_slot_us"))
                    result = simulate(args.mode, gain, packets, trace, args.spacing, args.initial_mss)
                    write_tables(directory, result)
                    summary.writerow(("independent_python_packet_model", args.mode, gain if gain is not None else "policy",
                                      repeat, args.bytes, packets, packets * MSS, MSS, RTT, 1 / TICKS_PER_MS,
                                      "retain_slot_fraction_floor_send_us" if args.spacing == "accumulated" else "floor_each_gap_us_and_send_us",
                                      milliseconds(result.tick), result.sender.cwnd * MSS,
                                      "max_now_next_slot_then_reserve_then_add_gap", source_hash,
                                      "fixed" if gain is not None else "phase_constant_reference", args.initial_mss,
                                      "paced_from_zero_using_known_RTT", EVENT_ORDER, False, result.sender.zero_gaps))
                    stream.flush()
                    print(f"{directory.name}: g={gain if gain is not None else result.sender.gain}, ACK={milliseconds(result.tick)} ms", flush=True)
                finally:
                    if events:
                        events.close()


if __name__ == "__main__":
    main()
