import argparse
import csv
from decimal import Decimal
import gzip
import json
from pathlib import Path

MASK = (1 << 32) - 1


def seconds(row, scale):
    if scale and row.get('t_tick') not in (None, ''):
        return Decimal(row['t_tick']) / Decimal(scale * 1000)
    return Decimal(row['t_ms']) / 1000


def export_case(run, out=None):
    run = Path(run)
    scale = None
    if run.is_dir():
        config = run / 'config.json'
        if config.exists():
            scale = json.loads(config.read_text(encoding='utf-8')).get('tick_scale')
        source = run / ('network_events.csv.gz' if (run / 'network_events.csv.gz').exists() else 'network_events.csv')
    else:
        source = run

    target = Path(out) if out is not None else (run / 'xpl/a2b_tsg.xpl' if run.is_dir() else source.parent / 'xpl/a2b_tsg.xpl')
    if target.is_dir():
        target = target / 'a2b_tsg.xpl'
    target.parent.mkdir(parents=True, exist_ok=True)

    opener = gzip.open if source.suffix == '.gz' else open

    first_t = None
    base_seq = None
    prev_ack = None
    prev_t = None
    data_count = 0
    ack_count = 0

    with opener(source, 'rt', newline='', encoding='utf-8') as f, \
         open(target, 'w', encoding='ascii', newline='\n') as out_file:

        out_file.write('double double\ntitle\nSSCP TSG\nxlabel\nTime (seconds)\nylabel\nSequence offset (bytes)\n')

        for row in csv.DictReader(f):
            direction = int(row['direction'])
            flags = int(row['flags'])
            size = int(row.get('payload_bytes', 0) or 0)

            if direction == 0 and (flags & 1) and size > 0:
                now = seconds(row, scale)
                if first_t is None:
                    first_t = now
                    base_seq = int(row['seq'])

                t = now - first_t
                start = (int(row['seq']) - base_seq) & MASK
                end = start + size
                out_file.write(f'white\ndarrow {t:.12f} {start}\nuarrow {t:.12f} {end}\nline {t:.12f} {start} {t:.12f} {end}\n')
                data_count += 1

            elif direction == 1 and (flags & 2) and first_t is not None:
                t = seconds(row, scale) - first_t
                ack = (int(row['ack']) - base_seq) & MASK

                out_file.write('green\n')
                if prev_ack is None:
                    out_file.write(f'dot {t:.12f} {ack}\n')
                else:
                    out_file.write(f'line {prev_t:.12f} {prev_ack} {t:.12f} {prev_ack}\n')
                    if ack != prev_ack:
                        out_file.write(f'line {t:.12f} {prev_ack} {t:.12f} {ack}\n')

                prev_ack, prev_t = ack, t
                ack_count += 1

        out_file.write('go\n')

    return dict(data_packets=data_count, ack_packets=ack_count, output=str(target))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('run', type=Path)
    parser.add_argument('-o', '--out', type=Path)
    args = parser.parse_args()
    print(json.dumps(export_case(args.run, args.out)))
