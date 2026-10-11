import argparse
import csv
import sys
import itertools
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from report import write_report

if len(sys.argv) == 3 and sys.argv[1] == "--merge":
    root = Path(sys.argv[2])
    rows, expected = [], 0
    for directory in sorted(root.glob("shard-*")):
        plan = directory / "plan.json"
        if plan.exists():
            expected += len(json.loads(plan.read_text())["cases"])
        path = directory / "sweep.csv"
        if path.exists():
            with path.open() as f:
                rows.extend(csv.DictReader(f))
    if not rows:
        raise SystemExit("No completed shard reports found")
    passed = write_report(root, rows, expected)
    raise SystemExit(0 if passed == expected else 1)

parser = argparse.ArgumentParser()
parser.add_argument("executable", type=Path)
parser.add_argument("--out", type=Path, default=Path("results/sweep"))
parser.add_argument("--jobs", type=int, default=2)
parser.add_argument("--config", type=Path, default=Path(__file__).with_name("network_sweep.json"))
parser.add_argument("--profile", default="broad")
parser.add_argument("--shard-index", type=int, default=0)
parser.add_argument("--shard-count", type=int, default=1)
parser.add_argument("--list", action="store_true")
args = parser.parse_args()
exe = args.executable.resolve()
out = args.out.resolve()
out.mkdir(parents=True, exist_ok=True)
config = json.loads(args.config.read_text())
settings = config["defaults"]
cases = []
fields = ("mbps", "delay_ms", "loss_ppm", "bytes", "seed")
for grid in config["profiles"][args.profile]:
    cases.extend(dict(zip(fields, values)) for values in itertools.product(*(grid[k] for k in fields)))
cases = list({tuple(c.items()): c for c in cases}.values())
if args.shard_count < 1 or not 0 <= args.shard_index < args.shard_count:
    parser.error("Invalid shard index/count")
total_cases = len(cases)
cases = cases[args.shard_index::args.shard_count]
print(f"Profile {args.profile}: {total_cases} cases; shard {args.shard_index}/{args.shard_count}: {len(cases)} cases", flush=True)
if args.list:
    raise SystemExit(0)
(out / "plan.json").write_text(json.dumps({"settings": settings, "cases": cases}, indent=2))

def run(case):
    rate, delay, loss, size, seed = (case[k] for k in ("mbps", "delay_ms", "loss_ppm", "bytes", "seed"))
    directory = out / f"bytes{size}_rate{rate}_delay{delay}_loss{loss}_seed{seed}"
    directory.mkdir(exist_ok=True)
    row = dict(case)
    command = [str(exe), "--bytes", str(size), "--mbps", str(rate),
               "--delay-ms", str(delay), "--loss-ppm", str(loss), "--seed", str(seed),
               "--timeout-ms", str(settings["simulated_timeout_ms"])]
    (directory / "config.json").write_text(json.dumps(row, indent=2))
    summary = directory / "summary.json"
    if summary.exists():
        summary.unlink()
    start = time.perf_counter()
    try:
        result = subprocess.run(command, cwd=directory, capture_output=True,
                                text=True, timeout=settings["wall_timeout_seconds"])
        row["exit_code"] = result.returncode
        (directory / "run.log").write_text(result.stdout + result.stderr)
    except subprocess.TimeoutExpired:
        row["exit_code"] = "wall_timeout"
        (directory / "run.log").write_text(f"Wall-clock timeout: {settings['wall_timeout_seconds']} seconds\n")
    row["wall_seconds"] = round(time.perf_counter() - start, 3)
    if summary.exists():
        row.update(json.loads(summary.read_text()))
    row["success"] = row.get("success", False) and row["exit_code"] == 0
    ms = row.get("simulated_ms", 0)
    row["goodput_mbps"] = round(row.get("received_bytes", 0) * 8 / (ms * 1000), 3) if ms else 0
    print(f"{'PASS' if row['success'] else 'FAIL'} {directory.name} {row['goodput_mbps']} Mbps", flush=True)
    return row

with ThreadPoolExecutor(max_workers=args.jobs) as pool:
    rows = list(pool.map(run, cases))
passed = write_report(out, rows)
print(f"Completed: {passed}/{len(rows)} passed. Report: {out / 'report.md'}")
raise SystemExit(0 if passed == len(rows) else 1)
