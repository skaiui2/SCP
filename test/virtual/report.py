import csv
from statistics import median


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
    if any(r.get("scenario") for r in rows):
        lines += ["Scenarios use fixed bandwidth/delay and independent random loss on both directions, including ACK/control packets. Wi-Fi is simplified; no radio contention, link-layer retries or burst-loss model.", "",
                  "Median/min/max below cover successful runs only; failures remain explicit. Relative goodput compares with the successful zero-loss median of the same scenario and size.", "",
                  "| Scenario | MiB | Loss % | PASS / Runs | Median Mbps | Min / Max Mbps | Relative to zero loss |",
                  "|:---|---:|---:|:---:|---:|:---|---:|"]
        groups = {}
        for r in rows:
            groups.setdefault((r.get("scenario", "network"), int(r["bytes"]), int(r["loss_ppm"])), []).append(r)
        baselines = {}
        for (scene, size, loss), group in groups.items():
            values = [float(r["goodput_mbps"]) for r in group if str(r["success"]).lower() == "true"]
            if loss == 0 and values: baselines[scene, size] = median(values)
        for (scene, size, loss), group in sorted(groups.items()):
            values = [float(r["goodput_mbps"]) for r in group if str(r["success"]).lower() == "true"]
            mid = f"{median(values):.3f}" if values else "N/A"
            span = f"{min(values):.3f} / {max(values):.3f}" if values else "N/A"
            base = baselines.get((scene, size), 0)
            relative = f"{median(values)/base:.1%}" if values and base else "N/A"
            lines.append(f"| {scene} | {size/2**20:g} | {loss/10000:g} | {len(values)} / {len(group)} | {mid} | {span} | {relative} |")
        lines += ["", "Individual runs:", ""]
    lines += ["| Scenario | MiB | Mbps | Base RTT ms | Loss % | Seed | Result | Goodput Mbps | Virtual seconds | Dropped |",
              "|:---|---:|---:|---:|---:|---:|:---:|---:|---:|---:|"]
    for r in rows:
        result = "PASS" if str(r["success"]).lower() == "true" else "FAIL"
        lines.append(f"| {r.get('scenario', 'network')} | {int(r['bytes'])/2**20:g} | {r['mbps']} | {int(r['delay_ms'])*2} | {int(r['loss_ppm'])/10000:g} | {r['seed']} | {result} | {r['goodput_mbps']} | {float(r.get('simulated_ms', 0))/1000:g} | {r.get('dropped', '?')} |")
    (root / "report.md").write_text("\n".join(lines) + "\n")
    return passed
