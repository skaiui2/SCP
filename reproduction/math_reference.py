"""ACK-index mathematical reference, not an execution of the SCP core."""
import math
from array import array


def next_slot_reference(mode, gain, packets, rtt_ms=100.0, quantum_ms=None):
    if mode not in ('ss', 'ca') or packets < 1 or not math.isfinite(gain) or gain <= 0:
        raise ValueError('Invalid phase, packet count or gain')
    sent, acked = array('d', [0.0])*packets, array('d', [0.0])*packets
    generations = array('I', [0])*packets
    windows = array('d', [1.0])
    scheduled, next_slot, window = 1, 0.0, 1.0
    for k in range(1, packets+1):
        ack_time = sent[k-1]+rtt_ms
        acked[k-1] = ack_time
        window += 1 if mode == 'ss' else 1/window
        windows.append(window)
        gap = rtt_ms/(gain*window)
        if quantum_ms is not None:
            gap = math.floor(gap/quantum_ms)*quantum_ms
        next_slot = max(next_slot, ack_time)
        limit = min(packets, k+math.floor(window))
        while scheduled < limit:
            sent[scheduled] = next_slot
            generations[scheduled] = generations[k-1]+1
            next_slot += gap
            scheduled += 1
    return dict(send_ms=sent, ack_ms=acked, cwnd_mss=windows, generation=generations)


def paper_ss_group_span(gain, parent_packets, rtt_ms=100.0):
    harmonic_difference = math.fsum(1/k for k in range(parent_packets+1, 2*parent_packets+1))
    return rtt_ms*(2*harmonic_difference-1/(2*parent_packets))/gain


def ca_ack_bounds(acks):
    correction = math.fsum(1/(1+2*k) for k in range(acks))
    return math.sqrt(1+2*acks), math.sqrt(1+2*acks+correction)
