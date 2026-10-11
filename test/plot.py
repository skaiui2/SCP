import argparse
import csv
import json
from pathlib import Path
from statistics import mean
import matplotlib.pyplot as plt


def save(fig, path):
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def plot_sweep(directory):
    with (directory / 'sweep.csv').open() as f:
        rows = list(csv.DictReader(f))
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


def plot_samples(directory):
    with (directory / 'samples.csv').open() as f:
        rows = [{k: int(v) for k, v in row.items()} for row in csv.DictReader(f)]
    t = [r['t_ms'] / 1000 for r in rows]
    fig, axes = plt.subplots(3, 1, figsize=(10, 9), sharex=True)
    for key in ('cwnd', 'flight', 'reserved'):
        axes[0].plot(t, [r[key] / 1024 for r in rows], label=key)
    axes[0].set_ylabel('KiB'); axes[0].legend()
    axes[1].plot(t, [r['received_bytes'] / 2**30 for r in rows])
    axes[1].set_ylabel('Delivered GiB')
    pairs = [(a, b) for a, b in zip(rows, rows[1:]) if b['t_ms'] > a['t_ms']]
    axes[2].plot([b['t_ms'] / 1000 for a, b in pairs], [(b['received_bytes'] - a['received_bytes']) * 8 / ((b['t_ms'] - a['t_ms']) * 1000) for a, b in pairs])
    axes[2].set(ylabel='Goodput Mbps', xlabel='Virtual seconds')
    for ax in axes: ax.grid(alpha=0.3)
    save(fig, directory / 'curves.png')


def plot_log(path, out, stream_fd, max_points):
    streams = {}
    with path.open(encoding='utf-8') as f:
        for line in f:
            try: row = json.loads(line)
            except ValueError: continue
            if not isinstance(row, dict) or 't' not in row or 'cwnd' not in row: continue
            fd = row.get('stream_fd', 0)
            if stream_fd is None or fd == stream_fd: streams.setdefault(fd, []).append(row)
    if not streams: raise SystemExit('No SCP state records found; enable SCP_DUMP_SS for state logs')
    out.mkdir(parents=True, exist_ok=True)
    for fd, rows in streams.items():
        elapsed, prev = 0, rows[0]['t']
        times = []
        for row in rows:
            elapsed += (row['t'] - prev) % 2**32
            prev = row['t']; times.append(elapsed / 1000)
        step = max(1, (len(rows) + max_points - 1) // max_points)
        indices = set(range(0, len(rows), step)) | {len(rows) - 1}
        indices.update(i for i in range(1, len(rows)) if rows[i].get('cc_phase') != rows[i-1].get('cc_phase'))
        indices = sorted(indices)
        sampled = [rows[i] for i in indices]
        sample_times = [times[i] for i in indices]
        keys = sorted({k for row in rows for k, value in row.items() if isinstance(value, (int, float))} - {'t', 'stream_fd'})
        for key in keys:
            fig, ax = plt.subplots(figsize=(9, 3))
            ax.plot(sample_times, [row.get(key, float('nan')) for row in sampled])
            ax.set(xlabel='Seconds', ylabel=key, title=f'Stream {fd}')
            ax.grid(alpha=0.3)
            save(fig, out / f'flow{fd}_{key}.png')
        if all('cwnd' in row and 'd' in row for row in rows):
            fig, ax = plt.subplots(figsize=(6, 4))
            ax.scatter([row['cwnd'] / 1024 for row in sampled], [row['d'] / 65536 for row in sampled], s=2)
            ax.set(xlabel='cwnd KiB', ylabel='Residual delay (ms)')
            save(fig, out / f'flow{fd}_phase.png')
        transitions = [dict(t_seconds=times[i], cc_phase=rows[i]['cc_phase']) for i in range(1, len(rows)) if rows[i].get('cc_phase') != rows[i-1].get('cc_phase') and 'cc_phase' in rows[i]]
        with (out / f'flow{fd}_transitions.csv').open('w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=['t_seconds', 'cc_phase'])
            writer.writeheader(); writer.writerows(transitions)


parser = argparse.ArgumentParser()
parser.add_argument('input', type=Path)
parser.add_argument('--out', type=Path, default=Path('plots'))
parser.add_argument('--stream-fd', type=int)
parser.add_argument('--max-points', type=int, default=10000)
args = parser.parse_args()
if args.max_points < 1: parser.error('--max-points must be positive')
if args.input.is_file(): plot_log(args.input, args.out, args.stream_fd, args.max_points)
elif (args.input / 'sweep.csv').exists(): plot_sweep(args.input)
else: plot_samples(args.input)
