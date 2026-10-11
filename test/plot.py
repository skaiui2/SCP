import argparse
import csv
import json
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
        fig, ax = plt.subplots(figsize=(9, 5))
        groups = {}
        for row in rows:
            groups.setdefault((row['scenario'], int(row['bytes'])), []).append(row)
        for (scene, size), group in sorted(groups.items()):
            losses = sorted({int(row['loss_ppm']) for row in group})
            values = [[float(row['goodput_mbps']) for row in group if int(row['loss_ppm']) == loss and row['success'].lower() == 'true'] for loss in losses]
            points = [(loss, data) for loss, data in zip(losses, values) if data]
            ax.plot([loss / 10000 for loss, data in points], [median(data) for loss, data in points], 'o-', label=f'{scene}, {size / 2**20:g} MiB')
            ax.fill_between([loss / 10000 for loss, data in points], [min(data) for loss, data in points], [max(data) for loss, data in points], alpha=0.15)
        ax.set(xlabel='Independent packet loss (%)', ylabel='Goodput Mbps', title='Successful runs: median and min/max; failures listed in report')
        ax.grid(alpha=0.3)
        ax.legend()
        save(fig, directory / 'sweep.png')
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


def plot_log(path, out, stream_fd, max_points):
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
args = parser.parse_args()
if args.max_points < 1: parser.error('--max-points must be positive')
def plot_directory(directory):
    for path in (directory / 'A.log', directory / 'B.log', directory / 'run.log'):
        if path.exists():
            plot_log(path, directory / path.stem, args.stream_fd, args.max_points)

if args.input.is_file():
    plot_log(args.input, args.out, args.stream_fd, args.max_points)
else:
    if (args.input / 'sweep.csv').exists(): plot_sweep(args.input)
    elif not args.recursive: plot_directory(args.input)
    if args.recursive:
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
print(f'Plots written under {args.out if args.input.is_file() else args.input}')
