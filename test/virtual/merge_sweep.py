import csv
import json
import sys
from pathlib import Path
from report import write_report

root = Path(sys.argv[1])
rows = []
expected = 0
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
write_report(root, rows, expected)
