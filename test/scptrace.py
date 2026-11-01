"""Export complete audited SCP research logs to jPlot/XPlot files, offline."""
import argparse
import csv
from decimal import Decimal
import gzip
import hashlib
import json
from pathlib import Path

from models.native_groups import groups

Q32 = 1 << 32
MASK = (1 << 32) - 1


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
                plot.line('blue', seconds, relative, seconds, relative + size)
                planned = row.get('planned_q32_tick', row.get('planned_q32_ms'))
                if planned not in (None, ''):
                    plan = int(planned) if 'planned_q32_tick' in row else int(planned) * scale
                    reserved = (plan - origin * Q32) / (Q32 * scale * 1000)
                    plot.dot('gray20', reserved, relative)
                if data in heads:
                    group = heads[data]
                    plot.dot('red', seconds, relative)
                    if group['generation'] % 100 == 0 or group['terminal']:
                        plot.label('red', seconds, relative,
                                   f"G{group['generation']} L={group['actual_span_ms']:.6f} ms"
                                   + (' complete' if group['complete'] else ' partial'))
                if data in tails:
                    plot.dot('red', seconds, relative + size)
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


def export_case(run, out=None):
    run = Path(run)
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
    title = (f"SCP {phase} {config['case_id']} IW{initial:g} {gain}"
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
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    print(json.dumps(export_case(args.run, args.out)))


if __name__ == '__main__':
    main()
