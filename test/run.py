import argparse
import hashlib
import sys
import json
import re
import subprocess
import time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("executable", type=Path)
parser.add_argument("--bytes", type=int, default=1073741824)
parser.add_argument("--out", type=Path, default=Path("udp-results"))
parser.add_argument("--timeout", type=int, default=600)
parser.add_argument("--mode", choices=["aimd", "prob", "pacing", "prob-prob", "prob-aimd"], default="aimd")
parser.add_argument("--seconds", type=int, default=3)
parser.add_argument("--backend", choices=["udp", "virtual"], default="udp")
parser.add_argument("--file", type=Path)
parser.add_argument("--file-test", action="store_true")
parser.add_argument("--netns", action="store_true")
parser.add_argument("--peer")
parser.add_argument("--no-plot", action="store_true")
parser.add_argument("--mbps", type=int, default=100)
parser.add_argument("--delay-ms", type=int, default=10)
parser.add_argument("--loss-ppm", type=int, default=0)
parser.add_argument("--seed", type=int, default=1)
args = parser.parse_args()
if args.bytes < 1 or args.seconds < 1: parser.error("Bytes and seconds must be positive")
if args.backend == "virtual" and args.mode != "aimd": parser.error("Virtual transfer currently uses AIMD")
args.out = args.out.resolve()
if args.file: args.file = args.file.resolve()
if (args.file or args.file_test) and (args.backend != "udp" or args.mode.startswith("prob-")):
    parser.error("File transfer requires single-flow UDP")
if args.netns and args.backend != "udp": parser.error("Namespaces require UDP")
if args.file_test and not args.file:
    args.out.mkdir(parents=True, exist_ok=True)
    args.file = args.out / "input.bin"
    chunk = bytes(range(256)) * 256
    with args.file.open("wb") as f:
        remaining = args.bytes
        while remaining:
            n = min(remaining, len(chunk)); f.write(chunk[:n]); remaining -= n
if args.file:
    args.bytes = args.file.stat().st_size
    if not args.bytes: parser.error("Input file is empty")
if not args.no_plot:
    try: import matplotlib, numpy, pandas
    except ImportError: parser.error("Plotting needs matplotlib, numpy and pandas: python3 -m pip install matplotlib numpy pandas; or use --no-plot")

def finish():
    if not args.no_plot:
        subprocess.run([sys.executable, str(Path(__file__).with_name("plot.py")), str(args.out)], check=True)
    (args.out / "report.md").write_text(
        f"# SCP {args.backend} run\n\nResult: PASS\n\n"
        f"Mode: {args.mode}\n\nBytes: {args.bytes if not args.mode.startswith('prob-') else 'timed two-flow experiment'}\n\n"
        "State curves come from the protocol's own JSON logs, using test/analyze_scp.py.\n", encoding="utf-8")
    print(f"Results: {args.out}", flush=True)
args.out.mkdir(parents=True, exist_ok=True)
exe = str(args.executable.resolve())
if args.backend == "virtual":
    command = [exe, "--bytes", str(args.bytes), "--mbps", str(args.mbps),
               "--delay-ms", str(args.delay_ms), "--loss-ppm", str(args.loss_ppm), "--seed", str(args.seed)]
    summary = args.out / "summary.json"
    if summary.exists(): summary.unlink()
    with (args.out / "run.log").open("w") as log:
        subprocess.run(command, cwd=args.out, stdout=log, stderr=subprocess.STDOUT, timeout=args.timeout, check=True)
    result = json.loads(summary.read_text())
    if not result["success"]: raise RuntimeError("Virtual transfer failed")
    print(f"PASS virtual: {args.bytes} bytes")
    finish()
    raise SystemExit(0)
fairness = args.mode in ("prob-prob", "prob-aimd")
amount = str(args.seconds if fairness else args.bytes)
peer = args.peer or ("10.0.2.1" if args.netns else "127.0.0.1")
command_b = [exe, "B", peer, amount, args.mode]
command_a = [exe, "A", peer, amount, args.mode]
received = args.out / "received.bin"
if args.file:
    if args.file == received: parser.error("Input must differ from output received.bin")
    command_a += ["--file", str(args.file)]
    command_b += ["--file", str(received)]
if args.netns:
    command_a = ["ip", "netns", "exec", "scp-c"] + command_a
    command_b = ["ip", "netns", "exec", "scp-s"] + command_b
with (args.out / "B.log").open("w") as log_b, (args.out / "A.log").open("w") as log_a:
    b = subprocess.Popen(command_b, stdout=log_b, stderr=subprocess.STDOUT, cwd=args.out)
    a = None
    try:
        time.sleep(1)
        if b.poll() is not None:
            raise RuntimeError("B exited while waiting for A")
        a = subprocess.Popen(command_a, stdout=log_a, stderr=subprocess.STDOUT, cwd=args.out)
        deadline = time.monotonic() + args.timeout
        while a.poll() is None or b.poll() is None:
            if time.monotonic() > deadline:
                raise RuntimeError("UDP transfer timed out")
            if (a.poll() is not None and a.returncode) or (b.poll() is not None and b.returncode):
                raise RuntimeError("UDP endpoint failed")
            time.sleep(0.05)
        if a.returncode or b.returncode:
            raise RuntimeError("UDP endpoint failed")
    finally:
        for process in (a, b):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
text_a = (args.out / "A.log").read_text()
text_b = (args.out / "B.log").read_text()
if fairness:
    for text in (text_a, text_b):
        summaries = [json.loads(line) for line in text.splitlines() if line.startswith('{"type":"fairness"')]
        if len(summaries) != 1 or summaries[0]["status"] != "success" or not all(summaries[0][key] > 0 for key in ("flow1_bytes", "flow2_bytes")):
            raise RuntimeError("Missing two-flow fairness measurement")
    print(f"PASS real UDP {args.mode}: both flows delivered data and closed")
    finish()
    raise SystemExit(0)
hash_a = re.search(r"PASS bytes=(\d+) checksum=([0-9a-f]+)", text_a)
hash_b = re.search(r"PASS bytes=(\d+) checksum=([0-9a-f]+)", text_b)
if not hash_a or not hash_b or hash_a.groups() != hash_b.groups() or int(hash_a[1]) != args.bytes:
    raise RuntimeError("UDP endpoint checksums differ")
print(f"PASS real UDP: {args.bytes} bytes, A=B={hash_a[2]}; B waited before A started")
observations = [json.loads(line) for line in text_a.splitlines() if line.startswith('{"type":"pacing_observation"')]
if len(observations) != 1 or observations[0]["data_datagrams"] < 1:
    raise RuntimeError("Missing pacing observations")
if args.file:
    def digest(path):
        h = hashlib.sha256()
        with path.open("rb") as f:
            for block in iter(lambda: f.read(1048576), b""): h.update(block)
        return h.hexdigest()
    if received.stat().st_size != args.bytes or digest(args.file) != digest(received):
        raise RuntimeError("Input/output files differ")
    print(f"PASS file SHA-256: {digest(received)}")
finish()
