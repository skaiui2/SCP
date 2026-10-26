import argparse
import csv
import json
import shutil
import subprocess
import sys
from pathlib import Path
from statistics import mean, median
import matplotlib.pyplot as plt


def save(fig, path):
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def plot_sweep(directory):
    with (directory / 'sweep.csv').open() as f:
        rows = list(csv.DictReader(f))
    if any(row.get('scenario') for row in rows):
        groups = {}
        for row in rows:
            key = (row['scenario'], int(row['bytes']), int(row['mbps']), int(row['delay_ms']), int(row.get('queue_packets', 0)))
            groups.setdefault(key, []).append(row)
        for field, output, ylabel in [('goodput_mbps', 'sweep.png', 'Goodput (Mbps)'),
                                      ('bufferbloat_ratio', 'bufferbloat.png', 'Mean SRTT / propagation RTT')]:
            fig, ax = plt.subplots(figsize=(11, 6))
            for (scene, size, rate, delay, queue), group in sorted(groups.items()):
                losses = sorted({int(row['loss_ppm']) for row in group})
                points = []
                for loss in losses:
                    data = [float(row[field]) for row in group if int(row['loss_ppm']) == loss
                            and row['success'].lower() == 'true' and row.get(field) not in (None, '')
                            and float(row[field]) >= 0]
                    if data: points.append((loss / 10000, data))
                failed = [row for row in group if row['success'].lower() != 'true'
                          and row.get(field) not in (None, '') and float(row[field]) >= 0]
                if failed:
                    ax.scatter([int(row['loss_ppm'])/10000 for row in failed],
                               [float(row[field]) for row in failed], marker='x', color='crimson',
                               label=f'{scene}, {rate} Mbps: failed gate (observed value)', zorder=5)
                if not points: continue
                label = f'{scene}, {size/2**20:g} MiB, {rate} Mbps, RTT {2*delay} ms, Q {queue}'
                line, = ax.plot([x for x, v in points], [median(v) for x, v in points], 'o-', label=label)
                ax.fill_between([x for x, v in points], [min(v) for x, v in points], [max(v) for x, v in points], color=line.get_color(), alpha=0.12)
                for x, values in points:
                    ax.scatter([x]*len(values), values, color=line.get_color(), s=12, alpha=0.5)
            ax.set(xlabel='Independent loss (%)', ylabel=ylabel,
                   title=f'SCP virtual network: {sum(r["success"].lower() != "true" for r in rows)} failed / {len(rows)} runs\nSuccessful seeds: dots, median and min/max envelope; failures retained in report')
            ax.grid(alpha=0.3)
            if ax.get_legend_handles_labels()[0]: ax.legend(fontsize=8)
            save(fig, directory / output)
        return
    size = min(int(r['bytes']) for r in rows)
    grid = [r for r in rows if int(r['bytes']) == size]
    rates = sorted({int(r['mbps']) for r in grid})
    losses = sorted({int(r['loss_ppm']) for r in grid})
    fig, axes = plt.subplots(1, len(rates), figsize=(4 * len(rates), 4), sharey=True, squeeze=False)
    for ax, rate in zip(axes[0], rates):
        for loss in losses:
            selected = [r for r in grid if int(r['mbps']) == rate and int(r['loss_ppm']) == loss and r['success'].lower() == 'true']
            delays = sorted({int(r['delay_ms']) for r in selected})
            ax.plot([d * 2 for d in delays], [mean(float(r['goodput_mbps']) for r in selected if int(r['delay_ms']) == d) for d in delays], 'o-', label=f'{loss / 10000:g}% loss')
        ax.set(title=f'{rate} Mbps link', xlabel='Propagation RTT (ms)', yscale='log')
        ax.grid(alpha=0.3)
    axes[0][0].set_ylabel('Goodput Mbps')
    axes[0][-1].legend()
    fig.suptitle(f'{size / 2**20:g} MiB; successful cases averaged across seeds')
    save(fig, directory / 'sweep.png')


def plot_curve(path):
    summary = path.with_name('summary.json')
    metadata = json.loads(summary.read_text()) if summary.exists() else {}
    scale = metadata.get('tick_scale', 1000)
    with path.open() as source:
        rows = list(csv.DictReader(source))
    fig, axes = plt.subplots(2, 1, figsize=(9, 6), sharex=True)
    for fd in sorted({r['stream_fd'] for r in rows}):
        flow = [r for r in rows if r['stream_fd'] == fd]
        ticks, epoch, previous = [], 0, 0
        for row in flow:
            tick = int(row['tick'])
            if previous - tick > 2**31: epoch += 2**32
            ticks.append((tick + epoch) / scale / 1000)
            previous = tick
        axes[0].plot(ticks, [int(r['cwnd'])/1024 for r in flow], label=f'flow {fd} cwnd')
        axes[0].plot(ticks, [int(r['flight'])/1024 for r in flow], '--', label=f'flow {fd} flight')
        axes[1].plot(ticks, [int(r['srtt_ticks'])/scale for r in flow], label=f'flow {fd}')
    axes[0].set(ylabel='KiB', title='Bounded core-state samples (up to 256 per flow); integrity checked throughout')
    axes[1].set(xlabel='Virtual time (s)', ylabel='SRTT (ms)')
    for ax in axes:
        ax.grid(alpha=0.3)
        if rows: ax.legend()
    save(fig, path.with_suffix('.png'))


def normalize_log(path):
    summary = path.parent / 'summary.json'
    scale = json.loads(summary.read_text()).get('tick_scale', 1) if summary.exists() else 1
    if scale == 1:
        return
    with path.open(encoding='utf-8') as source:
        for line in source:
            try: row = json.loads(line)
            except ValueError: continue
            if not isinstance(row, dict) or 't' not in row: continue
            if 't_tick' in row:
                if row.get('tick_scale') != scale: raise ValueError('Log tick scale differs from summary')
                return
            break
    raw = path.with_name(path.stem + '-ticks' + path.suffix)
    shutil.copyfile(path, raw)
    temporary = path.with_suffix(path.suffix + '.tmp')
    previous, epoch = None, 0
    with raw.open(encoding='utf-8') as source, temporary.open('w', encoding='utf-8') as output:
        for line in source:
            try: row = json.loads(line)
            except ValueError:
                output.write(line)
                continue
            if isinstance(row, dict) and 't' in row:
                tick = row['t']
                if previous is not None and previous - tick > 2**31: epoch += 2**32
                previous = tick
                row['t_tick'], row['tick_scale'] = tick, scale
                row['t'] = (tick + epoch) / scale
                for field in ('srtt', 'rtt_base', 'rto'):
                    if field in row: row[field] /= scale
                line = json.dumps(row, separators=(',', ':')) + '\n'
            output.write(line)
    temporary.replace(path)


def plot_log(path, out, stream_fd, max_points):
    normalize_log(path)
    streams = set()
    with path.open(encoding='utf-8') as f:
        for line in f:
            try: row = json.loads(line)
            except ValueError: continue
            if not isinstance(row, dict) or row.get('type') != 'ss': continue
            fd = row.get('stream_fd', 0)
            if stream_fd is None or fd == stream_fd: streams.add(fd)
    if not streams: raise SystemExit('No SCP state records found; enable SCP_DUMP_SS for state logs')
    for fd in sorted(streams):
        subprocess.run([sys.executable, str(Path(__file__).with_name('analyze_scp.py')),
                        str(path), '--stream-fd', str(fd), '--out', str(out / f'flow{fd}'),
                        '--max-points', str(max_points)], check=True)


parser = argparse.ArgumentParser()
parser.add_argument('input', type=Path)
parser.add_argument('--out', type=Path, default=Path('plots'))
parser.add_argument('--stream-fd', type=int)
parser.add_argument('--max-points', type=int, default=10000)
parser.add_argument('--recursive', action='store_true', help='Plot each completed sweep case')
parser.add_argument('--normalize-only', action='store_true', help='Preserve virtual raw ticks and convert log times to ms')
args = parser.parse_args()
if args.max_points < 1: parser.error('--max-points must be positive')
def plot_directory(directory):
    for path in (directory / 'A.log', directory / 'B.log', directory / 'run.log'):
        if path.exists():
            plot_log(path, directory / path.stem, args.stream_fd, args.max_points)

if args.normalize_only:
    normalize_log(args.input)
elif args.input.is_file():
    plot_log(args.input, args.out, args.stream_fd, args.max_points)
else:
    if (args.input / 'sweep.csv').exists(): plot_sweep(args.input)
    elif not args.recursive: plot_directory(args.input)
    if args.recursive:
        for curve in sorted(args.input.rglob('curve.csv')):
            plot_curve(curve)
        failures = []
        for log in sorted(args.input.rglob('run.log')):
            directory = log.parent
            try:
                plot_directory(directory)
                print(f'Case curves: {directory}')
            except (subprocess.CalledProcessError, SystemExit) as error:
                failures.append(directory)
                print(f'Plot failed: {directory}: {error}', file=sys.stderr)
        if failures: raise SystemExit(f'Plotting failed for {len(failures)} cases')
print(f'Time units normalized: {args.input}' if args.normalize_only else
      f'Plots written under {args.out if args.input.is_file() else args.input}')
