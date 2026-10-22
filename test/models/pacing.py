import argparse
import csv
import hashlib
import heapq
import math
from collections import deque
from dataclasses import dataclass
from pathlib import Path

MSS = 1430
RTT = 100
TICKS_PER_MS = 1000
GAINS = sorted([2 * math.log(2) + k * 0.05 for k in range(-6, 7)]
               + [1, 1.1, 1.75, 2, 2.5, 3, 4, 6, 8])


@dataclass
class Packet:
    number: int
    generation: int
    reserved: int
    sent: int = -1


class Sender:
    def __init__(self, mode, gain, total, network):
        self.mode, self.gain, self.total, self.network = mode, gain, total, network
        self.cwnd = 1.0
        self.flight = self.scheduled = self.acked = 0
        self.next_send_time = self.gap = 0

    def try_send(self, generation):
        self.next_send_time = max(self.next_send_time, self.network.tick)
        while self.scheduled < self.total and self.flight + 1 <= self.cwnd:
            packet = Packet(self.scheduled, generation, self.next_send_time)
            self.scheduled += 1
            self.flight += 1
            self.network.reserve(packet)
            self.next_send_time += self.gap

    def receive_ack(self, packet):
        self.flight -= 1
        self.acked += 1
        if self.mode == "ss":
            self.cwnd += 1
        else:
            self.cwnd += 1 / self.cwnd
        self.gap = int(RTT * TICKS_PER_MS / (self.gain * self.cwnd))
        self.network.record("ACK_RECEIVED", packet, self.network.tick)
        self.try_send(packet.generation + 1)


class Receiver:
    def __init__(self, network):
        self.network, self.received = network, 0

    def receive_data(self, packet):
        self.received += 1
        self.network.record("DATA_RECEIVED", packet, self.network.tick)
        self.network.send_ack(packet)


class Network:
    def __init__(self, trace=None):
        self.tick = self.sent = 0
        self.send_queue, self.ack_queue = [], deque()
        self.trace = trace
        self.windows, self.rtts, self.generations = {}, {}, {}

    def reserve(self, packet):
        heapq.heappush(self.send_queue, (packet.reserved, 0, packet.number, packet))
        self.record("RESERVE", packet, packet.reserved)

    def send_ack(self, packet):
        self.ack_queue.append((self.tick + RTT * TICKS_PER_MS // 2, packet))

    def state(self):
        a = self.sender
        return (a.cwnd * MSS, a.flight * MSS,
                (self.sent - a.acked) * MSS, (a.scheduled - self.sent) * MSS)

    def record(self, event, packet, due):
        if self.trace:
            self.trace.writerow((event, milliseconds(self.tick), packet.number * MSS,
                                 MSS, packet.generation, milliseconds(due),
                                 milliseconds(packet.sent) if packet.sent >= 0 else "",
                                 *self.state()))

    def step(self):
        while True:
            if self.send_queue and self.send_queue[0][0] <= self.tick:
                due, kind, _, packet = heapq.heappop(self.send_queue)
                if kind == 0:
                    packet.sent = self.tick
                    self.sent += 1
                    self.record("SEND", packet, due)
                    arrival = self.tick + RTT * TICKS_PER_MS // 2
                    heapq.heappush(self.send_queue, (arrival, 1, packet.number, packet))
                    for table, key in ((self.rtts, self.tick // (RTT * TICKS_PER_MS)),
                                       (self.generations, packet.generation)):
                        span = table.setdefault(key, [self.tick, self.tick, 0, False])
                        span[1], span[2] = self.tick, span[2] + 1
                        span[3] |= packet.number + 1 == self.sender.total
                else:
                    self.receiver.receive_data(packet)
            elif self.ack_queue and self.ack_queue[0][0] <= self.tick:
                _, packet = self.ack_queue.popleft()
                self.sender.receive_ack(packet)
            else:
                break

    def run(self):
        while self.sender.acked < self.sender.total:
            self.step()
            if self.tick % (RTT * TICKS_PER_MS) == 0:
                self.windows[self.tick // (RTT * TICKS_PER_MS)] = self.state()
            if self.sender.acked < self.sender.total:
                self.tick += 1
        assert self.sender.scheduled == self.sent == self.receiver.received == self.sender.acked
        assert self.sender.flight == 0 and not self.send_queue and not self.ack_queue


def milliseconds(ticks):
    return f"{ticks / TICKS_PER_MS:.3f}"


def simulate(mode, gain, packets, trace=None):
    network = Network(trace)
    network.sender = Sender(mode, gain, packets, network)
    network.receiver = Receiver(network)
    network.sender.try_send(0)
    network.run()
    return network


def write_tables(directory, windows, rtts, generations):
    with (directory / "rtt.csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("rtt", "cwnd_bytes", "flight_bytes", "sent_unacked_bytes", "queued_bytes",
                         "first_send_ms", "last_send_ms", "span_ms",
                         "packets", "last_data_bin"))
        for n in sorted(windows.keys() | rtts.keys()):
            first, last, count, final = rtts.get(n, (None, None, 0, False))
            times = tuple(milliseconds(t) for t in (first, last, last - first)) if count else ("", "", "")
            writer.writerow((n, *windows[n], *times, count, final))
    with (directory / "generations.csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("generation", "first_send_ms", "last_send_ms",
                         "span_ms", "span_over_rtt", "packets",
                         "contains_last_packet"))
        for n, (first, last, count, final) in sorted(generations.items()):
            writer.writerow((n, milliseconds(first), milliseconds(last), milliseconds(last - first),
                             f"{(last - first) / (RTT * TICKS_PER_MS):.5f}", count, final))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("ss", "ca"), default="ca")
    parser.add_argument("--gain", type=float, help="one gain instead of the 22-gain grid")
    parser.add_argument("--bytes", type=int, default=1 << 30, help="requested size, rounded up to full MSS packets")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--trace", action="store_true", help="write per-packet events.csv")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    gains = GAINS if args.gain is None else [args.gain]
    if args.bytes <= 0 or args.repeats <= 0:
        parser.error("bytes and repeats must be positive")
    if any(not math.isfinite(g) or g <= 0 for g in gains):
        parser.error("gain must be positive and finite")
    packets = (args.bytes + MSS - 1) // MSS
    args.out.mkdir(parents=True, exist_ok=False)
    source_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

    with (args.out / "summary.csv").open("w", newline="") as stream:
        summary = csv.writer(stream)
        summary.writerow(("backend", "mode", "gain", "repeat", "requested_bytes", "packets",
                          "transferred_bytes", "mss",
                          "rtt_ms", "tick_ms", "pacing_rounding", "last_ack_ms",
                          "final_cwnd_bytes", "reservation_rule", "script_sha256"))
        for index, gain in enumerate(gains, 1):
            for repeat in range(1, args.repeats + 1):
                directory = args.out / f"g{index:02d}-r{repeat:02d}"
                directory.mkdir()
                events = (directory / "events.csv").open("w", newline="") if args.trace else None
                try:
                    trace = csv.writer(events) if events else None
                    if trace:
                        trace.writerow(("event", "now_ms", "seq", "bytes", "generation",
                                        "due_ms", "sent_ms", "cwnd_bytes",
                                        "flight_bytes", "sent_unacked_bytes", "queued_bytes"))
                    result = simulate(args.mode, gain, packets, trace)
                    write_tables(directory, result.windows, result.rtts, result.generations)
                    summary.writerow(("python_packet_model", args.mode, gain, repeat,
                                      args.bytes, packets, packets * MSS, MSS, RTT, 1 / TICKS_PER_MS,
                                      "floor_to_0.001ms", milliseconds(result.tick),
                                      result.sender.cwnd * MSS, "max_now_next_slot_then_reserve_then_add_gap", source_hash))
                    stream.flush()
                    print(f"{directory.name}: g={gain:.17g}, ACK={milliseconds(result.tick)} ms", flush=True)
                finally:
                    if events:
                        events.close()


if __name__ == "__main__":
    main()
