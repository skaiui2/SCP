"""Export complete audited SSCP research logs or run logs to jPlot/XPlot files, offline."""
import argparse
from collections import deque
import csv
from decimal import Decimal
import gzip
import hashlib
import json
from pathlib import Path

Q32 = 1 << 32
MASK = (1 << 32) - 1
RTT = 100


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def groups(run):
    summary = json.loads((run / 'summary.json').read_text(encoding='utf-8'))
    config = json.loads((run / 'config.json').read_text(encoding='utf-8'))
    audit = json.loads((run / 'execution_audit.json').read_text(encoding='utf-8'))
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
    scale = int(summary.get('tick_scale', config.get('tick_scale', 1)))
    first = summary.get('first_data_tx_tick', round(summary['first_data_tx_ms'] * scale))
    end = summary.get('data_acked_tick', round(summary['data_acked_ms'] * scale))
    reservations, by_end, limited, grouped = deque(), {}, set(), {}
    next_slot = assigned_bytes = acknowledged_bytes = 0
    assigned_packets = sent_packets = 0
    active_parent = None
    ack_count = 0
    with opener(source, 'rt', newline='', encoding='utf-8') as stream:
        for row in csv.DictReader(stream):
            event, direction = row['event'], int(row['direction'])
            flags = int(row['flags'])
            now = int(row['t_tick']) if 't_tick' in row else round(float(row['t_ms']) * scale)
            echo = int(row['echo_send_tick']) if 'echo_send_tick' in row else round(float(row['echo_send_ms']) * scale)
            is_ack = (direction == 1 and flags == 2 and
                      first <= now <= end and echo >= first)
            if is_ack and event == 'arrival':
                if active_parent is not None:
                    raise ValueError('Nested sender ACK processing')
                active_parent = by_end.pop(int(row['ack']))
                acknowledged_bytes += active_parent['bytes']
                ack_count += 1
            if row.get('next_slot_q32_tick', row.get('next_slot_q32_ms')):
                observed_slot = int(row['next_slot_q32_tick']) if 'next_slot_q32_tick' in row else int(row['next_slot_q32_ms']) * scale
                if observed_slot != next_slot:
                    cursor = max(next_slot, now << 32)
                    if observed_slot < cursor:
                        raise ValueError('Observed slot moved backwards')
                    cwnd, gain = int(row['cwnd_bytes']), int(row['gain_q16'])
                    while cursor < observed_slot:
                        size = min(mss, total - assigned_bytes)
                        if not size:
                            raise ValueError('Reservation exceeds payload')
                        gap = Q32 * scale if now < first + RTT * scale else (
                            size * RTT * scale * ((65536 << 32) // gain) // cwnd)
                        following = cursor + gap
                        if not gap or following > observed_slot:
                            raise ValueError('Cannot reconstruct reservation end')
                        if not ack_count:
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
                plan = int(row['planned_q32_tick']) if 'planned_q32_tick' in row else int(row['planned_q32_ms']) * scale
                size = int(row['payload_bytes'])
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
                   gain_policy=summary['gain_policy'], g=config['g'], tick_scale=scale,
                   g_effective=config['g_effective'], repeat=config['repeat'],
                   generation=generation, packets=group['count'],
                   packet_first=head['number'], packet_last=tail['number'],
                   parent_first=head['parent'], parent_last=tail['parent'],
                   first_send_tick=h-first, last_send_tick=t-first,
                   first_send_ms=(h-first)/scale, last_send_ms=(t-first)/scale,
                   actual_span_ms=(t-h)/scale,
                   first_reserved_ms=(head['planned']/Q32-first)/scale,
                   last_reserved_ms=(tail['planned']/Q32-first)/scale,
                   reservation_span_ms=(tail['planned']-head['planned'])/Q32/scale,
                   tail_gap_reconstructed_ms=tail['gap']/Q32/scale,
                   tail_residual_after_actual_send_ms=(tail['following']/Q32-t)/scale,
                   tail_slot_reconstructed_ms=(tail['following']/Q32-first)/scale,
                   occupied_span_reconstructed_ms=(tail['following']/Q32-h)/scale,
                   start_drift_ms=(h-first-generation*RTT*scale)/scale if attributable else '',
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
            old_first = previous['first_send_tick'] + first
            old_slot_floor = int(previous['tail_slot_reconstructed_q32_tick']) >> 32
            expected = max(old_first + RTT * scale, old_slot_floor)
            row['extra_start_delay_ms'] = (h - old_first - RTT * scale) / scale
            row['expected_extra_start_delay_floor_ms'] = (expected - old_first - RTT * scale) / scale
            row['start_rule_error_ms'] = (h - expected) / scale
        row['tail_slot_reconstructed_q32_tick'] = tail['following']
        result.append(row)
        previous = row if attributable else None
    identity = dict(run=str(run), tick_scale=scale, engine='actual_scp_core_plus_virtual_network',
                    config_sha256=digest(run/'config.json'),
                    events_sha256=digest(source), source=str(source),
                    binary_sha256=config['binary_sha256'],
                    data_packets=sent_packets, acknowledged_bytes=acknowledged_bytes,
                    complete_groups=sum(row['complete'] for row in result),
                    unattributed_groups=sum(not row['attributable'] for row in result),
                    nonzero_start_rule_errors=sum(row['start_rule_error_ms'] not in ('', 0) for row in result),
                    reconstructed_fields='Per-packet tail gap/slot use observed next-slot changes and audited core arithmetic; actual send times are direct transport observations.')
    return result, identity


def open_text(path):
    return gzip.open(path, 'rt', encoding='utf-8') if path.suffix == '.gz' else path.open(encoding='utf-8')


def log_path(run, name):
    path = run / name
    return path if path.exists() else path.with_suffix('') if path.suffix == '.gz' else Path(str(path) + '.gz')


def tick(row, scale, raw='t_tick', physical='t_ms'):
    if row.get(raw) not in (None, ''):
        return int(row[raw])
    value = Decimal(str(row[physical])) * scale
    if value != int(value):
        raise ValueError('Timestamp is outside the declared tick grid')
    return int(value)


class XPlot:
    def __init__(self, stream, title, ylabel):
        self.stream, self.color = stream, None
        stream.write(f'double double\ntitle\n{title}\nxlabel\nSeconds since first DATA\nylabel\n{ylabel}\n')

    def emit(self, color, command):
        if color != self.color:
            self.stream.write(color + '\n')
            self.color = color
        self.stream.write(command + '\n')

    def line(self, color, x1, y1, x2, y2):
        self.emit(color, f'line {x1:.12f} {y1} {x2:.12f} {y2}')

    def dot(self, color, x, y):
        self.emit(color, f'dot {x:.12f} {y}')

    def label(self, color, x, y, text):
        self.emit(color, f'ltext {x:.12f} {y}\n{text}')

    def finish(self):
        self.stream.write('go\n')


def write_tsg(run, plot, summary, scale, origin, group_rows):
    heads = {row['packet_first']: row for row in group_rows}
    tails = {row['packet_last']: row for row in group_rows}
    data = acknowledgments = controls = payload = 0
    first_seq = previous_ack = previous_time = None
    pending_controls = []
    end = summary.get('data_acked_tick', round(summary['data_acked_ms'] * scale))
    with open_text(log_path(run, 'network_events.csv.gz')) as stream:
        for row in csv.DictReader(stream):
            direction, flags = int(row['direction']), int(row['flags'])
            if not ((direction == 0 and row['event'] == 'send') or
                    (direction == 1 and row['event'] == 'arrival')):
                continue
            now = tick(row, scale)
            seconds = (now - origin) / (scale * 1000)
            if direction == 0 and flags & 1:
                seq, size = int(row['seq']), int(row['payload_bytes'])
                if first_seq is None:
                    first_seq = seq
                relative = (seq - first_seq) & MASK
                plot.line('white', seconds, relative, seconds, relative + size)
                data += 1
                payload += size
            elif (direction == 1 and flags == 2 and origin <= now <= end and
                  tick(row, scale, 'echo_send_tick', 'echo_send_ms') >= origin):
                if first_seq is None:
                    raise ValueError('DATA ACK precedes the first DATA send')
                ack = (int(row['ack']) - first_seq) & MASK
                if previous_ack is None:
                    plot.dot('green', seconds, ack)
                else:
                    plot.line('green', previous_time, previous_ack, seconds, previous_ack)
                    plot.line('green', seconds, previous_ack, seconds, ack)
                previous_ack, previous_time = ack, seconds
                acknowledgments += 1
            else:
                pending_controls.append((seconds, direction, flags))
                controls += 1
    for seconds, direction, flags in pending_controls:
        plot.label('purple', seconds, 0, f"{'A>B' if direction == 0 else 'B>A'} flags=0x{flags:02x}")
    if (data != summary['new_packets'] or acknowledgments != data or
            payload != summary['received_bytes']):
        raise ValueError('Exported DATA/ACK counts differ from the audited transfer')
    return dict(data_packets=data, ack_packets=acknowledgments, control_packets=controls)


def write_window(run, plot, config, scale, origin):
    previous = None
    count = 0
    fields = (('cwnd', 'blue'), ('flight', 'green'), ('ssthresh', 'red'))
    with open_text(log_path(run, config.get('state_log', 'run.log.gz'))) as stream:
        for line in stream:
            if not line.startswith('{'):
                continue
            row = json.loads(line)
            if row.get('type') != 'ss' or row.get('stream_fd') != config.get('stream_fd', 1):
                continue
            if row.get('tick_scale', scale) != scale:
                raise ValueError('State clock scale differs from the experiment')
            now = tick(row, scale, physical='t')
            seconds = (now - origin) / (scale * 1000)
            if previous is not None and seconds < previous[0]:
                raise ValueError('State samples moved backwards')
            for name, color in fields:
                value = int(row[name])
                if previous is None:
                    plot.dot(color, seconds, value)
                else:
                    plot.line(color, previous[0], previous[1][name], seconds, value)
            previous = seconds, {name: int(row[name]) for name, _ in fields}
            count += 1
    if not count:
        raise ValueError('No sender state samples found')
    return count


def export_plain_log(run, out=None):
    candidates = [run / 'run.log', run / 'A.log', run / 'B.log'] if run.is_dir() else [run]
    source = next((p for p in candidates if p.exists()), None)
    if source is None:
        raise ValueError(f'No log file found in {run}')
    target = Path(out) if out is not None else (run / 'trace.xpl' if run.is_dir() else run.with_suffix('.xpl'))
    if target.suffix != '.xpl':
        target.mkdir(parents=True, exist_ok=True)
        target = target / 'trace.xpl'
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
    fields = (('cwnd', 'blue'), ('flight', 'green'), ('ssthresh', 'red'))
    previous = None
    origin = None
    count = 0
    tmp = target.with_suffix('.xpl.tmp')
    try:
        with tmp.open('w', encoding='ascii', newline='\n') as stream:
            plot = XPlot(stream, f'SSCP Window Trace ({source.name})', 'Bytes')
            with open_text(source) as log:
                for line in log:
                    if not line.startswith('{'):
                        continue
                    row = json.loads(line)
                    if row.get('type') != 'ss':
                        continue
                    t_ms = float(row.get('t', 0.0))
                    if origin is None:
                        origin = t_ms
                    seconds = (t_ms - origin) / 1000.0
                    for name, color in fields:
                        if name not in row:
                            continue
                        value = int(row[name])
                        if previous is None:
                            plot.dot(color, seconds, value)
                        else:
                            plot.line(color, previous[0], previous[1][name], seconds, value)
                    previous = seconds, {name: int(row.get(name, 0)) for name, _ in fields}
                    count += 1
            plot.finish()
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)
    return dict(state_samples=count, output=str(target))


def export_case(run, out=None):
    run = Path(run)
    if not (run.is_dir() and (run / 'config.json').exists()):
        return export_plain_log(run, out)
    config = json.loads((run / 'config.json').read_text(encoding='utf-8'))
    summary = json.loads((run / 'summary.json').read_text(encoding='utf-8'))
    scale = int(summary.get('tick_scale', config.get('tick_scale', 1)))
    if scale not in (1, 1000) or config.get('tick_scale', scale) != scale:
        raise ValueError('Clock scale differs from the experiment')
    group_rows, identity = groups(run)
    origin = summary.get('first_data_tx_tick', round(summary['first_data_tx_ms'] * scale))
    phase = 'SS' if summary['mode'] == 'always_slow_start' else 'CA'
    initial = summary.get('initial_cwnd_bytes', config.get('initial_cwnd_bytes', config['mss_bytes'])) / config['mss_bytes']
    exporter = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:7]
    gain = (f"Q16={config['gain_q16']}" if config.get('gain_q16') else
            f"g={config['g_effective']}" if config.get('g_effective') is not None else summary['gain_policy'])
    title = (f"SSCP {phase} {config['case_id']} IW{initial:g} {gain}"
             f" src={config.get('commit', 'unknown')[:7]} xpl={exporter}")
    out = Path(out) if out is not None else run / 'xpl'
    out.mkdir(parents=True, exist_ok=True)
    paths = [out / 'a2b_tsg.xpl', out / 'a2b_window.xpl']
    temporary = [path.with_suffix('.xpl.tmp') for path in paths]
    try:
        with temporary[0].open('w', encoding='ascii', newline='\n') as stream:
            plot = XPlot(stream, title, 'Sequence offset (bytes)')
            result = write_tsg(run, plot, summary, scale, origin, group_rows)
            plot.finish()
        with temporary[1].open('w', encoding='ascii', newline='\n') as stream:
            plot = XPlot(stream, title + ' (1 ms samples)', 'Bytes')
            result['state_samples'] = write_window(run, plot, config, scale, origin)
            plot.finish()
        if result['data_packets'] != identity['data_packets']:
            raise ValueError('Export and packet-group audit disagree')
        for source, target in zip(temporary, paths):
            source.replace(target)
    finally:
        for path in temporary:
            path.unlink(missing_ok=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    parser.add_argument('-o', '--out', type=Path)
    args = parser.parse_args()
    print(json.dumps(export_case(args.run, args.out)))


if __name__ == '__main__':
    main()
