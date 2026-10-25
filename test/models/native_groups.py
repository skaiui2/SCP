"""Derive packet groups from audited actual-SCP transport events."""
import argparse
import csv
import gzip
import hashlib
import json
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

Q32 = 1 << 32
RTT = 100


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def groups(run):
    summary = json.loads((run / 'summary.json').read_text())
    config = json.loads((run / 'config.json').read_text())
    audit = json.loads((run / 'execution_audit.json').read_text())
    if (summary['status'] != 'complete' or not summary['checksum_ok'] or
            summary['retrans_packets'] or summary['dropped'] or
            not audit['next_slot_rule_verified'] or
            not audit['receiver_ack_generation_verified']):
        raise ValueError(f'Incomplete or unaudited native run: {run}')
    source = run / 'network_events.csv.gz'
    if not source.exists():
        source = run / 'network_events.csv'
    opener = gzip.open if source.suffix == '.gz' else open
    mss, total = config['mss_bytes'], summary['received_bytes']
    first = summary['first_data_tx_ms']
    reservations, by_end, limited, grouped = deque(), {}, set(), {}
    next_slot = assigned_bytes = acknowledged_bytes = 0
    assigned_packets = sent_packets = 0
    active_parent = None
    ack_count = 0
    with opener(source, 'rt', newline='') as stream:
        for row in csv.DictReader(stream):
            event, direction = row['event'], int(row['direction'])
            flags, now = int(row['flags']), int(row['t_ms'])
            is_ack = (direction == 1 and flags == 2 and
                      first <= now <= summary['data_acked_ms'] and
                      int(row['echo_send_ms']) >= first)
            if is_ack and event == 'arrival':
                if active_parent is not None:
                    raise ValueError('Nested sender ACK processing')
                active_parent = by_end.pop(int(row['ack']))
                acknowledged_bytes += active_parent['bytes']
                ack_count += 1
            if row['next_slot_q32_ms']:
                observed_slot = int(row['next_slot_q32_ms'])
                if observed_slot != next_slot:
                    cursor = max(next_slot, now << 32)
                    if observed_slot < cursor:
                        raise ValueError('Observed slot moved backwards')
                    cwnd, gain = int(row['cwnd_bytes']), int(row['gain_q16'])
                    while cursor < observed_slot:
                        size = min(mss, total - assigned_bytes)
                        if not size:
                            raise ValueError('Reservation exceeds payload')
                        gap = Q32 if now < first + RTT else (
                            size * RTT * ((65536 << 32) // gain) // cwnd)
                        following = cursor + gap
                        if not gap or following > observed_slot:
                            raise ValueError('Cannot reconstruct reservation end')
                        if not assigned_packets:
                            generation = 0
                        elif active_parent is not None and active_parent['generation'] >= 0:
                            generation = active_parent['generation'] + 1
                        else:
                            generation = -1
                        packet = dict(number=assigned_packets, bytes=size,
                                      parent=active_parent['number'] if active_parent is not None else -1,
                                      generation=generation, planned=cursor, following=following,
                                      gap=gap, reservation_cwnd=cwnd, gain_q16=gain,
                                      sent=None, seq=None)
                        assigned_packets += 1
                        reservations.append(packet)
                        assigned_bytes += size
                        cursor = following
                    next_slot = observed_slot
            if direction == 0 and event == 'send' and flags & 1:
                packet = reservations.popleft()
                plan, size = int(row['planned_q32_ms']), int(row['payload_bytes'])
                if (packet['planned'] != plan or packet['bytes'] != size or
                        plan >> 32 != now):
                    raise ValueError('Actual send differs from observed reservation')
                packet['sent'], packet['seq'] = now, int(row['seq'])
                by_end[(packet['seq'] + size) % 2**32] = packet
                sent_packets += 1
                group = grouped.setdefault(packet['generation'], dict(
                    head=packet, tail=packet, count=0, full=True))
                group['tail'] = packet
                group['count'] += 1
                group['full'] &= size == mss
            if is_ack and event == 'input_returned':
                if active_parent is None:
                    raise ValueError('ACK returned without an arrival')
                remaining = total - assigned_bytes
                available = int(row['cwnd_bytes']) - (assigned_bytes - acknowledged_bytes)
                if remaining and available >= min(mss, remaining):
                    limited.add(active_parent['generation'] + 1)
                active_parent = None
    if (active_parent is not None or reservations or by_end or
            assigned_bytes != total or acknowledged_bytes != total or
            ack_count != sent_packets or assigned_packets != sent_packets or
            sent_packets != summary['new_packets']):
        raise ValueError('Incomplete packet/ACK reconstruction')
    result, previous = [], None
    for generation, group in sorted(grouped.items()):
        head, tail = group['head'], group['tail']
        h, t = head['sent'], tail['sent']
        terminal = tail['number'] == sent_packets - 1
        attributable = generation >= 0 and generation not in limited
        complete = attributable and not terminal and group['full']
        row = dict(case_id=config['case_id'], phase=summary['mode'],
                   gain_policy=summary['gain_policy'], g=config['g'],
                   g_effective=config['g_effective'], repeat=config['repeat'],
                   generation=generation, packets=group['count'],
                   packet_first=head['number'], packet_last=tail['number'],
                   parent_first=head['parent'], parent_last=tail['parent'],
                   first_send_ms=h-first, last_send_ms=t-first,
                   actual_span_ms=t-h,
                   first_reserved_ms=head['planned']/Q32-first,
                   last_reserved_ms=tail['planned']/Q32-first,
                   reservation_span_ms=(tail['planned']-head['planned'])/Q32,
                   tail_gap_reconstructed_ms=tail['gap']/Q32,
                   tail_residual_after_actual_send_ms=tail['following']/Q32-t,
                   tail_slot_reconstructed_ms=tail['following']/Q32-first,
                   occupied_span_reconstructed_ms=tail['following']/Q32-h,
                   start_drift_ms=h-first-generation*RTT if attributable else '',
                   extra_start_delay_ms='',
                   expected_extra_start_delay_floor_ms='',
                   start_rule_error_ms='',
                   initial_reservation_cwnd_bytes=head['reservation_cwnd'],
                   tail_reservation_cwnd_bytes=tail['reservation_cwnd'],
                   first_gain_q16=head['gain_q16'], tail_gain_q16=tail['gain_q16'],
                   attributable=attributable, complete=complete,
                   terminal=terminal, short_tail=tail['bytes'] != mss,
                   application_limited=generation in limited)
        if (attributable and previous is not None and
                previous['generation'] + 1 == generation):
            old_first = previous['first_send_ms'] + first
            old_slot_floor = int(previous['tail_slot_reconstructed_q32']) >> 32
            expected = max(old_first + RTT, old_slot_floor)
            row['extra_start_delay_ms'] = h - old_first - RTT
            row['expected_extra_start_delay_floor_ms'] = expected - old_first - RTT
            row['start_rule_error_ms'] = h - expected
        row['tail_slot_reconstructed_q32'] = tail['following']
        result.append(row)
        previous = row if attributable else None
    identity = dict(run=str(run), engine='actual_scp_core_plus_virtual_network',
                    config_sha256=digest(run/'config.json'),
                    events_sha256=digest(source), source=str(source),
                    binary_sha256=config['binary_sha256'],
                    data_packets=sent_packets, acknowledged_bytes=acknowledged_bytes,
                    complete_groups=sum(row['complete'] for row in result),
                    unattributed_groups=sum(not row['attributable'] for row in result),
                    nonzero_start_rule_errors=sum(row['start_rule_error_ms'] not in ('', 0) for row in result),
                    reconstructed_fields='Per-packet tail gap/slot use observed next-slot changes and audited core arithmetic; actual send times are direct transport observations.')
    return result, identity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runs', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=1)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    sources = []
    runs = [p.parent for p in sorted(args.runs.rglob('runs/*/config.json'))]
    if not runs or args.workers < 1:
        raise ValueError('Need completed native runs and positive workers')
    with ProcessPoolExecutor(max_workers=args.workers) as pool, \
            (args.out/'native_groups.csv').open('w', newline='') as stream:
        writer = None
        for group_rows, identity in pool.map(groups, runs):
            if writer is None:
                writer = csv.DictWriter(stream, fieldnames=list(group_rows[0]))
                writer.writeheader()
            writer.writerows(group_rows)
            sources.append(identity)
            stream.flush()
            print(f"Audited native groups: {len(sources)}/{len(runs)}", flush=True)
        if writer is None:
            raise ValueError('No completed native runs found')
    metadata = dict(analysis_sha256=digest(Path(__file__)), rtt_ms=RTT,
                    virtual_tick_ms=1, sources=sources)
    (args.out/'provenance.json').write_text(json.dumps(metadata, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(dict(runs=len(sources),
        nonzero_start_rule_errors=sum(s['nonzero_start_rule_errors'] for s in sources),
        unattributed_groups=sum(s['unattributed_groups'] for s in sources))))


if __name__ == '__main__':
    main()
