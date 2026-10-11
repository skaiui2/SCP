import argparse
import csv
from pathlib import Path
import matplotlib.pyplot as plt

parser = argparse.ArgumentParser()
parser.add_argument("directory", type=Path)
args = parser.parse_args()
with (args.directory / "samples.csv").open() as f:
    rows = [{k: int(v) for k, v in r.items()} for r in csv.DictReader(f)]
t = [r["t_ms"] / 1000 for r in rows]
fig, axes = plt.subplots(3, 1, figsize=(10, 9), sharex=True)
for key in ("cwnd", "flight", "reserved"):
    axes[0].plot(t, [r[key] / 1024 for r in rows], label=key)
axes[0].set_ylabel("KiB")
axes[0].legend()
axes[1].plot(t, [r["received_bytes"] / 2**30 for r in rows])
axes[1].set_ylabel("Delivered GiB")
axes[2].plot(t[1:], [(b["received_bytes"] - a["received_bytes"]) * 8 / ((b["t_ms"] - a["t_ms"]) * 1000) for a, b in zip(rows, rows[1:])])
axes[2].set_ylabel("Goodput Mbps")
axes[2].set_xlabel("Virtual seconds")
for ax in axes:
    ax.grid(alpha=0.3)
fig.tight_layout()
fig.savefig(args.directory / "curves.png", dpi=160)
