import argparse
import csv
from pathlib import Path
import matplotlib.pyplot as plt
from statistics import mean

parser = argparse.ArgumentParser()
parser.add_argument("directory", type=Path)
args = parser.parse_args()
with (args.directory / "sweep.csv").open() as f:
    rows = list(csv.DictReader(f))
grid = [r for r in rows if int(r["bytes"]) == min(int(x["bytes"]) for x in rows)]
rates = sorted({int(r["mbps"]) for r in grid})
losses = sorted({int(r["loss_ppm"]) for r in grid})
fig, axes = plt.subplots(1, len(rates), figsize=(4 * len(rates), 4), sharey=True, squeeze=False)
axes = axes[0]
for ax, rate in zip(axes, rates):
    for loss in losses:
        selected = [r for r in grid if int(r["mbps"]) == rate and int(r["loss_ppm"]) == loss and r["success"] == "True"]
        delays = sorted({int(r["delay_ms"]) for r in selected})
        ax.plot([d * 2 for d in delays],
                [mean(float(r["goodput_mbps"]) for r in selected if int(r["delay_ms"]) == d) for d in delays],
                "o-", label=f"{loss / 10000:g}% loss")
    ax.set_title(f"{rate} Mbps link")
    ax.set_xlabel("Propagation RTT (ms)")
    ax.set_yscale("log")
    ax.grid(alpha=0.3)
axes[0].set_ylabel("Goodput (Mbps, log scale)")
axes[-1].legend()
fig.suptitle(f"SCP AIMD: {int(grid[0]['bytes'])/2**20:g} MiB sweep (mean across seeds; successful cases)")
fig.tight_layout()
fig.savefig(args.directory / "sweep.png", dpi=160)
