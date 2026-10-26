import csv
from statistics import median


def truth(value):
    return str(value).lower() == "true"


def passed(row):
    return truth(row.get("success", False))


def write_report(root, rows, expected=None):
    keys = list(dict.fromkeys(k for row in rows for k in row))
    with (root / "sweep.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)
    count = sum(passed(row) for row in rows)
    lines = [f"# Network sweep: {count}/{expected or len(rows)} passed", "",
             "Actual SCP core, deterministic tick-by-tick virtual network. Goodput includes startup and close.",
             "Queue capacity is per direction, including serialization but excluding propagation. Zero means unlimited.",
             "SRTT mean/max are sampled after sender ACK processing; Bufferbloat = mean SRTT / propagation RTT.",
             "Rexmit Ratio = DATA retransmissions (both directions) / max(1, all network drops). This is not proof of spurious retransmission.",
             "Every run requires byte validation, checksum, sequence conservation and core allocation/timer cleanup. Zero RTO is additionally required only where the profile says so.",
             "Startup measures the first crossing of 80% of the observed peak cwnd, from first DATA send, in propagation RTTs. It is not 80% BDP.", "",
             "![Goodput and seed envelope](sweep.png)", "![Bufferbloat](bufferbloat.png)", "",
             "| Scenario | MiB | Link Mbps / RTT ms / Queue | Loss% | Seed | Result | Goodput Mbps | Bufferbloat | Zero-RTO? | Rexmit Ratio | Repro Command (if failed) |",
             "|:---|---:|:---|---:|---:|:---:|---:|---:|:---:|---:|:---|"]
    if expected is not None and len(rows) != expected:
        lines.insert(2, f"FAIL: expected {expected} reports, received {len(rows)}.")
    for row in rows:
        ok = passed(row)
        zero = "yes" if str(row.get("timeouts")) == "0" else "NO" if "timeouts" in row else "N/A"
        command = "" if ok else "`" + row.get("repro_command", "missing command") + "`"
        lines.append(f"| {row.get('scenario', 'network')} | {int(row['bytes']) / 2**20:g} | {row['mbps']} / {int(row['delay_ms'])*2} / {row.get('queue_packets', 0)} | {int(row['loss_ppm'])/10000:g} | {row['seed']} | {'PASS' if ok else 'FAIL'} | {row['goodput_mbps']} | {row.get('bufferbloat_ratio', 'N/A')} | {zero} | {row.get('spurious_rexmit_ratio', 'N/A')} | {command} |")
    lines += ["", "Seed envelopes (successful runs only; configurations are never mixed):", "",
              "| Scenario / Mbps / RTT / Queue / MiB | Loss% | Pass / Runs | Median [min, max] Mbps | Retention vs zero loss |",
              "|:---|---:|:---:|:---|---:|"]
    groups, baselines = {}, {}
    for row in rows:
        key = (row.get('scenario', 'network'), int(row['mbps']), int(row['delay_ms']), int(row.get('queue_packets', 0)), int(row['bytes']))
        groups.setdefault((key, int(row['loss_ppm'])), []).append(row)
    for (key, loss), group in groups.items():
        values = [float(r['goodput_mbps']) for r in group if passed(r)]
        if not loss and values: baselines[key] = median(values)
    for (key, loss), group in sorted(groups.items()):
        values = [float(r['goodput_mbps']) for r in group if passed(r)]
        label = f"{key[0]} / {key[1]} / {2*key[2]} / {key[3]} / {key[4]/2**20:g}"
        value = f"{median(values):.3f} [{min(values):.3f}, {max(values):.3f}]" if values else 'N/A'
        retention = f"{median(values)/baselines[key]:.1%}" if values and baselines.get(key) else 'N/A'
        lines.append(f"| {label} | {loss/10000:g} | {len(values)} / {len(group)} | {value} | {retention} |")
    for row in rows:
        if not passed(row):
            lines += ["", f"Failure: {row.get('failure_reason', 'transfer failed')}",
                      "", "```sh", "Repro: " + row.get('repro_command', 'unavailable'), "```"]
    (root / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return count
