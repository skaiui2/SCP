import csv
from statistics import median


def truth(value):
    return str(value).lower() == "true"


def passed(row):
    return truth(row.get("success", False))


def fval(row, key, default=0.0):
    val = row.get(key)
    return float(val) if val not in (None, "") else default


def ival(row, key, default=0):
    val = row.get(key)
    return int(float(val)) if val not in (None, "") else default


def write_report(root, rows, expected=None):
    keys = list(dict.fromkeys(k for row in rows for k in row))
    with (root / "sweep.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)

    count = sum(passed(row) for row in rows)
    total = expected or len(rows)
    lines = [f"# SSCP Network Sweep Report: {count}/{total} passed", ""]
    if expected is not None and len(rows) != expected:
        lines += [f"**FAIL**: expected {expected} reports, received {len(rows)}.", ""]

    failed = [row for row in rows if not passed(row)]
    if failed:
        lines += [
            f"## Failed Cases ({len(failed)})",
            "",
            "| Scenario | MiB | Link / RTT / Queue | Loss % | Seed | Failure Reason | Time (s) | RTOs | Repro Command |",
            "|:---|---:|:---|---:|---:|:---|---:|---:|:---|",
        ]
        for r in failed:
            mib = f"{ival(r, 'bytes') / 2**20:g}"
            env = f"{ival(r, 'mbps')}M / {ival(r, 'delay_ms') * 2}ms / q{ival(r, 'queue_packets')}"
            loss = f"{ival(r, 'loss_ppm') / 10000:g}%"
            reason = r.get("failure_reason") or "transfer failed"
            repro = "`" + r.get("repro_command", "unavailable") + "`"
            lines.append(
                f"| {r.get('scenario', 'network')} | {mib} | {env} | {loss} | {ival(r, 'seed')} "
                f"| {reason} | {fval(r, 'time_s'):.2f} | {ival(r, 'timeouts')} | {repro} |"
            )
        lines.append("")

    lines += [
        "## Environment Summary",
        "",
        "| Scenario (Link / RTT / Queue) | Loss % | Pass | Time (s) [med / max] | Goodput (Util%) | Retrans % | RTOs [med / max] | SRTT ms [avg / max] |",
        "|:---|---:|:---:|---:|---:|---:|---:|---:|",
    ]
    groups = {}
    for r in rows:
        key = (
            r.get("scenario", "network"),
            ival(r, "mbps"),
            ival(r, "delay_ms"),
            ival(r, "queue_packets"),
            ival(r, "bytes"),
            ival(r, "loss_ppm"),
        )
        groups.setdefault(key, []).append(r)

    for key, group in sorted(groups.items()):
        scen, mbps, delay, q, size, loss_ppm = key
        ok_group = [r for r in group if passed(r)]
        active = ok_group or group
        label = f"{scen} ({mbps}M / {2 * delay}ms / q{q})"
        loss_str = f"{loss_ppm / 10000:g}%"
        pass_str = f"{len(ok_group)}/{len(group)}"
        times = [fval(r, "time_s") for r in active]
        gputs = [fval(r, "goodput_mbps") for r in active]
        utils = [fval(r, "util_pct") for r in active]
        retrans = [fval(r, "retrans_pct") for r in active]
        rtos = [ival(r, "timeouts") for r in active]
        srtt_avg = [fval(r, "srtt_avg_ms") for r in active]
        srtt_max = [fval(r, "srtt_max_ms") for r in active]
        gput_str = f"{median(gputs):.2f} Mbps ({median(utils):.1f}%)" if mbps else f"{median(gputs):.2f} Mbps"
        lines.append(
            f"| {label} | {loss_str} | {pass_str} "
            f"| {median(times):.2f} / {max(times):.2f} "
            f"| {gput_str} "
            f"| {median(retrans):.2f}% "
            f"| {int(median(rtos))} / {max(rtos)} "
            f"| {median(srtt_avg):.1f} / {max(srtt_max):.1f} |"
        )

    lines += [
        "",
        f"<details><summary>All individual runs ({len(rows)} cases)</summary>",
        "",
        "| Scenario | MiB | Link / RTT / Queue | Loss % | Seed | Result | Time (s) | Goodput (Util%) | Retrans % | RTOs | SRTT ms [avg / max] |",
        "|:---|---:|:---|---:|---:|:---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        ok = passed(r)
        mib = f"{ival(r, 'bytes') / 2**20:g}"
        mbps = ival(r, "mbps")
        env = f"{mbps}M / {ival(r, 'delay_ms') * 2}ms / q{ival(r, 'queue_packets')}"
        loss_str = f"{ival(r, 'loss_ppm') / 10000:g}%"
        gput_str = f"{fval(r, 'goodput_mbps'):.2f} ({fval(r, 'util_pct'):.1f}%)" if mbps else f"{fval(r, 'goodput_mbps'):.2f}"
        lines.append(
            f"| {r.get('scenario', 'network')} | {mib} | {env} | {loss_str} | {ival(r, 'seed')} "
            f"| {'PASS' if ok else 'FAIL'} | {fval(r, 'time_s'):.2f} | {gput_str} "
            f"| {fval(r, 'retrans_pct'):.2f}% | {ival(r, 'timeouts')} "
            f"| {fval(r, 'srtt_avg_ms'):.1f} / {fval(r, 'srtt_max_ms'):.1f} |"
        )
    lines += ["", "</details>"]
    (root / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return count
