import csv


def write_report(root, rows, expected=None):
    keys = list(dict.fromkeys(k for row in rows for k in row))
    with (root / "sweep.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)
    passed = sum(str(r["success"]).lower() == "true" for r in rows)
    lines = [f"# Network sweep: {passed}/{len(rows)} passed", "",
             "Goodput includes startup, ACK drain and closure. Base RTT excludes serialization and queueing.", ""]
    if expected is not None:
        lines += [f"Planned cases in recovered shards: {expected}. Completed reports: {len(rows)}.", ""]
    lines += ["| MiB | Mbps | Base RTT ms | Loss % | Seed | Result | Goodput Mbps | Virtual seconds | Dropped |",
              "|---:|---:|---:|---:|---:|:---:|---:|---:|---:|"]
    for r in rows:
        result = "PASS" if str(r["success"]).lower() == "true" else "FAIL"
        lines.append(f"| {int(r['bytes'])/2**20:g} | {r['mbps']} | {int(r['delay_ms'])*2} | {int(r['loss_ppm'])/10000:g} | {r['seed']} | {result} | {r['goodput_mbps']} | {int(r.get('simulated_ms', 0))/1000:g} | {r.get('dropped', '?')} |")
    (root / "report.md").write_text("\n".join(lines) + "\n")
    return passed
