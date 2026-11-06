#!/usr/bin/env python3
"""Run SSCP pacing research experiments and generate standard xplot traces."""

import argparse
import csv
import json
import math
import os
import subprocess
import sys
from pathlib import Path

CA_CRITICAL = 1.5 * math.log(3)
SS_CRITICAL = 2.0 * math.log(2)

CA_GAINS = [
    ("g90", 0.9),
    ("g100", 1.0),
    ("g120", 1.2),
    ("g140", 1.4),
    ("g150", 1.5),
    ("g154_8", CA_CRITICAL - 0.10),
    ("g159_8", CA_CRITICAL - 0.05),
    ("g162_8", CA_CRITICAL - 0.02),
    ("g163_8", CA_CRITICAL - 0.01),
    ("g164_3", CA_CRITICAL - 0.005),
    ("g164_8", CA_CRITICAL),
    ("g165_3", CA_CRITICAL + 0.005),
    ("g165_8", CA_CRITICAL + 0.01),
    ("g166_8", CA_CRITICAL + 0.02),
    ("g169_8", CA_CRITICAL + 0.05),
    ("g174_8", CA_CRITICAL + 0.10),
    ("g190", 1.9),
    ("g200", 2.0),
    ("g250", 2.5),
    ("g300", 3.0),
    ("g400", 4.0),
    ("g800", 8.0),
]

SS_GAINS = [
    ("g100", 1.0),
    ("g108_6", SS_CRITICAL - 0.30),
    ("g110", 1.1),
    ("g113_6", SS_CRITICAL - 0.25),
    ("g118_6", SS_CRITICAL - 0.20),
    ("g123_6", SS_CRITICAL - 0.15),
    ("g128_6", SS_CRITICAL - 0.10),
    ("g133_6", SS_CRITICAL - 0.05),
    ("g138_6", SS_CRITICAL),
    ("g143_6", SS_CRITICAL + 0.05),
    ("g148_6", SS_CRITICAL + 0.10),
    ("g153_6", SS_CRITICAL + 0.15),
    ("g158_6", SS_CRITICAL + 0.20),
    ("g163_6", SS_CRITICAL + 0.25),
    ("g168_6", SS_CRITICAL + 0.30),
    ("g175", 1.75),
    ("g200", 2.0),
    ("g250", 2.5),
    ("g300", 3.0),
    ("g400", 4.0),
    ("g600", 6.0),
    ("g800", 8.0),
]


def find_binary(explicit_path=None):
    if explicit_path:
        p = Path(explicit_path)
        if p.is_file():
            return str(p)
    candidates = [
        Path("test/build/sscp_research.exe"),
        Path("test/build/sscp_research"),
        Path("build/sscp_research.exe"),
        Path("build/sscp_research"),
        Path("research-build/sscp_research.exe"),
        Path("research-build/sscp_research"),
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    return "sscp_research"


def run_single(runner_bin, mode, gain_id, gain_val, gain_q16, total_bytes, delay_ms, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    events_csv = out_dir / "network_events.csv"
    summary_json = out_dir / "summary.json"
    xpl_file = out_dir / "xpl" / "a2b_tsg.xpl"

    cmd = [
        runner_bin,
        "--gain-q16", str(gain_q16),
        "--mode", mode,
        "--bytes", str(total_bytes),
        "--delay-ms", str(delay_ms),
        "--events", str(events_csv),
        "--summary", str(summary_json),
    ]

    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        print(f"[{gain_id}] FAILED with code {res.returncode}:\n{res.stderr}", file=sys.stderr)
        return None

    trace_script = Path(__file__).resolve().parent.parent / "sscptrace.py"
    if not trace_script.exists():
        trace_script = Path("test/sscptrace.py").resolve()

    trace_cmd = [sys.executable, str(trace_script), str(events_csv), "-o", str(xpl_file)]
    subprocess.run(trace_cmd, check=True, stdout=subprocess.DEVNULL)

    data = {}
    if summary_json.exists():
        data = json.loads(summary_json.read_text(encoding="utf-8"))

    row = {
        "gain_id": gain_id,
        "gain_value": f"{gain_val:.6f}" if gain_val > 0 else "native",
        "gain_q16": gain_q16,
        "bytes": data.get("bytes", total_bytes),
        "duration_ms": data.get("duration_ms", 0.0),
        "mbps": data.get("mbps", 0.0),
        "max_cwnd": data.get("max_cwnd", 0),
        "packets": data.get("packets", 0),
        "dropped": data.get("dropped", 0),
        "xpl": str(xpl_file),
    }

    print(f"[{gain_id}] bytes={row['bytes']} time={row['duration_ms']:.1f}ms rate={row['mbps']:.2f}Mbps -> {xpl_file}")
    return row


def main():
    parser = argparse.ArgumentParser(description="Run SSCP pacing experiments.")
    parser.add_argument("--mode", choices=["ca", "ss"], default="ca", help="Experiment mode (ca or ss)")
    parser.add_argument("--gain-id", type=str, help="Run specific gain ID (e.g. g164_8, native)")
    parser.add_argument("--all", action="store_true", help="Run all 22 gains in grid")
    parser.add_argument("--bytes", type=int, default=8388608, help="Bytes to transfer (default 8MB)")
    parser.add_argument("--delay-ms", type=int, default=50, help="One-way delay in ms (default 50)")
    parser.add_argument("--bin", type=str, help="Path to sscp_research binary")
    parser.add_argument("--out", type=Path, default=Path("results"), help="Output directory")
    args = parser.parse_args()

    gains_table = CA_GAINS if args.mode == "ca" else SS_GAINS
    gains_map = {gid: gval for gid, gval in gains_table}
    gains_map["native"] = 0.0

    selected = []
    if args.gain_id:
        if args.gain_id not in gains_map:
            parser.error(f"Unknown gain-id: {args.gain_id}. Available: {list(gains_map.keys())}")
        selected.append((args.gain_id, gains_map[args.gain_id]))
    elif args.all:
        selected = list(gains_table)
    else:
        # Default to critical gain
        default_id = "g164_8" if args.mode == "ca" else "g138_6"
        selected.append((default_id, gains_map[default_id]))

    runner_bin = find_binary(args.bin)
    rows = []

    for gid, gval in selected:
        q16 = int(round(gval * 65536)) if gval > 0 else 0
        run_out = args.out / gid if len(selected) > 1 else args.out
        r = run_single(runner_bin, args.mode, gid, gval, q16, args.bytes, args.delay_ms, run_out)
        if r:
            rows.append(r)

    if rows:
        args.out.mkdir(parents=True, exist_ok=True)
        summary_csv = args.out / "summary.csv"
        write_header = not summary_csv.exists()
        with summary_csv.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            if write_header:
                writer.writeheader()
            writer.writerows(rows)


if __name__ == "__main__":
    main()
