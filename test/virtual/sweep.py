import argparse
import csv
import sys
import itertools
import json
import subprocess
import time
import hashlib
import shlex
import os
from threading import Timer
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from report import write_report

if len(sys.argv) == 3 and sys.argv[1] == "--merge":
    root = Path(sys.argv[2])
    rows, expected, total_planned = [], 0, 0
    for directory in sorted(root.glob("shard-*")):
        plan = directory / "plan.json"
        if plan.exists():
            metadata = json.loads(plan.read_text())
            expected += len(metadata["cases"])
            total_planned = max(total_planned, metadata.get("total_cases", 0))
        path = directory / "sweep.csv"
        if path.exists():
            with path.open() as f:
                rows.extend(csv.DictReader(f))
    if not rows:
        raise SystemExit("No completed shard reports found")
    expected = total_planned or expected
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
fields = ("mbps", "delay_ms", "loss_ppm", "bytes", "seed", "queue_packets")
for grid in config["profiles"][args.profile]:
    cases.extend(dict(zip(fields, values), scenario=grid.get("scenario", "network"),
                      require_zero_rto=grid.get("require_zero_rto", False))
                 for values in itertools.product(*(grid.get(k, [0]) for k in fields)))
cases = list({tuple(c.items()): c for c in cases}.values())
if args.shard_count < 1 or not 0 <= args.shard_index < args.shard_count:
    parser.error("Invalid shard index/count")
total_cases = len(cases)
cases = cases[args.shard_index::args.shard_count]
print(f"Profile {args.profile}: {total_cases} cases; shard {args.shard_index}/{args.shard_count}: {len(cases)} cases", flush=True)
if args.list:
    raise SystemExit(0)
source = Path(__file__).resolve().parents[2]
metadata = dict(settings=settings, cases=cases, total_cases=total_cases, profile=args.profile,
                executable=str(exe), binary_sha256=hashlib.sha256(exe.read_bytes()).hexdigest(),
                source_sha=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip(),
                source_dirty=bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=source, text=True).strip()))
(out / "plan.json").write_text(json.dumps(metadata, indent=2))

def run(case):
    rate, delay, loss, size, seed = (case[k] for k in ("mbps", "delay_ms", "loss_ppm", "bytes", "seed"))
    directory = out / (case.get("scenario", "network") + "_" + f"bytes{size}_rate{rate}_delay{delay}_loss{loss}_queue{case['queue_packets']}_seed{seed}")
    directory.mkdir(exist_ok=True)
    row = dict(case)
    command = [str(exe), "--bytes", str(size), "--mbps", str(rate),
               "--delay-ms", str(delay), "--loss-ppm", str(loss), "--seed", str(seed),
               "--timeout-ms", str(settings["simulated_timeout_ms"]),
               "--queue-packets", str(case["queue_packets"])]
    row["repro_command"] = ("& " + subprocess.list2cmdline(command)) if os.name == "nt" else shlex.join(command)
    (directory / "config.json").write_text(json.dumps(row, indent=2))
    summary = directory / "summary.json"
    if summary.exists():
        summary.unlink()
    start = time.perf_counter()
    tail = deque(maxlen=32)
    expired = []
    with subprocess.Popen(command, cwd=directory, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding="utf-8", errors="replace") as process:
        def stop():
            if process.poll() is None:
                expired.append(True)
                process.kill()
        timer = Timer(settings["wall_timeout_seconds"], stop)
        timer.start()
        try:
            for line in process.stdout:
                tail.append(line.rstrip())
            row["exit_code"] = process.wait()
        finally:
            timer.cancel()
    if expired: row["exit_code"] = "wall_timeout"
    row["wall_seconds"] = round(time.perf_counter() - start, 3)
    if summary.exists():
        try:
            row.update(json.loads(summary.read_text()))
        except (ValueError, TypeError) as error:
            tail.append(f"Invalid summary: {error}")
    failures = []
    for field in ("success", "checksum_ok", "sequence_ok", "cleanup_ok"):
        if row.get(field) is not True: failures.append(field)
    if row["exit_code"] != 0: failures.append(str(row["exit_code"]))
    if case["require_zero_rto"] and row.get("timeouts", 0): failures.append("nonzero RTO")
    row["transfer_success"] = row.get("success", False)
    row["success"] = not failures
    row["failure_reason"] = ", ".join(failures)
    if failures:
        (directory / "failure.log").write_text("\n".join(tail) + "\nRepro: " + row['repro_command'] + "\n")
        print("Repro: " + row['repro_command'], flush=True)
    else:
        (directory / "failure.log").unlink(missing_ok=True)
    ms = row.get("simulated_ms", 0)
    retrans = row.get("retrans_packets", 0)
    new_pkts = (size + 1429) // 1430
    row["time_s"] = round(ms / 1000, 3) if ms else 0
    row["goodput_mbps"] = round(row.get("received_bytes", 0) * 8 / (ms * 1000), 2) if ms else 0
    row["util_pct"] = round(row["goodput_mbps"] / rate * 100, 1) if rate and ms else 0
    row["retrans_pct"] = round(retrans / (new_pkts + retrans) * 100, 2) if (new_pkts + retrans) else 0
    print(f"{'PASS' if row['success'] else 'FAIL'} {directory.name} {row['time_s']}s {row['goodput_mbps']} Mbps ({row['util_pct']}%) retrans={row['retrans_pct']}% rto={row.get('timeouts', 0)}", flush=True)
    return row

with ThreadPoolExecutor(max_workers=args.jobs) as pool:
    rows = list(pool.map(run, cases))
passed = write_report(out, rows)
print(f"Completed: {passed}/{len(rows)} passed. Report: {out / 'report.md'}")
raise SystemExit(0 if passed == len(rows) else 1)
