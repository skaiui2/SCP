import argparse
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
args = parser.parse_args()
args.out.mkdir(parents=True, exist_ok=True)
exe = str(args.executable.resolve())
fairness = args.mode in ("prob-prob", "prob-aimd")
amount = str(args.seconds if fairness else args.bytes)
command_b = [exe, "B", "127.0.0.1", amount, args.mode]
command_a = [exe, "A", "127.0.0.1", amount, args.mode]
with (args.out / "B.log").open("w") as log_b, (args.out / "A.log").open("w") as log_a:
    b = subprocess.Popen(command_b, stdout=log_b, stderr=subprocess.STDOUT)
    a = None
    try:
        time.sleep(1)
        if b.poll() is not None:
            raise RuntimeError("B exited while waiting for A")
        a = subprocess.Popen(command_a, stdout=log_a, stderr=subprocess.STDOUT)
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
    raise SystemExit(0)
hash_a = re.search(r"PASS bytes=(\d+) checksum=([0-9a-f]+)", text_a)
hash_b = re.search(r"PASS bytes=(\d+) checksum=([0-9a-f]+)", text_b)
if not hash_a or not hash_b or hash_a.groups() != hash_b.groups() or int(hash_a[1]) != args.bytes:
    raise RuntimeError("UDP endpoint checksums differ")
print(f"PASS real UDP: {args.bytes} bytes, A=B={hash_a[2]}; B waited before A started")
observations = [json.loads(line) for line in text_a.splitlines() if line.startswith('{"type":"pacing_observation"')]
if len(observations) != 1 or observations[0]["data_datagrams"] < 1:
    raise RuntimeError("Missing pacing observations")
