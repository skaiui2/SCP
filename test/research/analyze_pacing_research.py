#!/usr/bin/env python3
"""Merge SCP cwnd curves and per-RTT DATA release histograms.

Dependencies: numpy, matplotlib. Reads native SCP state and transport logs.
This script does not modify the protocol or run transfers.
"""
import argparse
import csv
import gzip
import json
import math
from collections import defaultdict
from pathlib import Path
from statistics import median

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


def open_text(path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open(encoding="utf-8")


def write_csv(path, rows, fields=None):
    if fields is None:
        fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="", encoding="utf-8-sig") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def representative_gains(gains):
    targets = (1, 2*math.log(2)-.05, 2*math.log(2), 2*math.log(2)+.05, 2, 4, 8)
    return sorted({min(gains, key=lambda gain: abs(gain-target)) for target in targets})


def packet_metrics(slots, first_ms, last_ms, rtt_ms, exposure_ms):
    """Exact endpoint span/mean and 1 ms histogram CV; empty/singleton bins are flagged."""
    if len(slots) != rtt_ms or any(type(n) is not int or n < 0 for n in slots):
        raise ValueError("slot_counts must contain rtt_ms nonnegative integers")
    n = sum(slots)
    occupied = [i for i, count in enumerate(slots) if count]
    result = dict(packet_count=n, occupied_ms=len(occupied),
                  exposure_ms=exposure_ms, full_rtt=exposure_ms == rtt_ms,
                  dispersion_valid=n >= 2)
    if not n:
        result.update(span_ms=None, span_rtt=None, occupied_fraction=None,
                      burst_fraction=None, hhi=None, effective_ms=None,
                      gap_mean_ms=None, gap_cv=None, zero_gap_fraction=None)
        return result
    if first_ms is None or last_ms is None:
        raise ValueError("Nonempty bins need first_tx_ms and last_tx_ms")
    span = last_ms - first_ms
    if abs(span - (occupied[-1] - occupied[0])) >= 1:
        raise ValueError("first/last timestamps disagree with the 1 ms histogram")
    hhi = sum(count * count for count in slots) / (n * n)
    mean_gap = span / (n - 1) if n >= 2 else None
    histogram_mean = (occupied[-1] - occupied[0]) / (n - 1) if n >= 2 else None
    cv = None
    if n >= 2 and histogram_mean > 0:
        positive_gaps = np.diff(occupied)
        mean_square = float(np.square(positive_gaps).sum()) / (n - 1)
        cv = math.sqrt(max(0.0, mean_square - histogram_mean ** 2)) / histogram_mean
    result.update(span_ms=span, span_rtt=span / rtt_ms,
                  occupied_fraction=len(occupied) / rtt_ms,
                  burst_fraction=max(slots) / n, hhi=hhi, effective_ms=1 / hhi,
                  gap_mean_ms=mean_gap, gap_cv=cv,
                  zero_gap_fraction=(n - len(occupied)) / (n - 1) if n >= 2 else None)
    return result


def load_states(path, stream_fd):
    times, windows = [], []
    with open_text(path) as source:
        for line_number, line in enumerate(source, 1):
            if not line.lstrip().startswith("{"):
                continue
            try:
                row = json.loads(line)
            except ValueError as error:
                raise ValueError(f"{path}:{line_number}: invalid JSON") from error
            if row.get("type") != "ss" or int(row.get("stream_fd", -1)) != stream_fd:
                continue
            t, window = float(row["t"]), int(row["cwnd"])
            if times and t < times[-1]:
                raise ValueError(f"{path}: decreasing timestamps; this experiment expects <24h")
            if window < 0:
                raise ValueError(f"{path}: negative cwnd")
            if times and t == times[-1]:
                windows[-1] = window
            else:
                times.append(t)
                windows.append(window)
    if not times:
        raise ValueError(f"{path}: no state records for stream_fd={stream_fd}")
    return np.asarray(times, dtype=float), np.asarray(windows, dtype=np.int64)


def past_samples(times, windows, query):
    indices = np.searchsorted(times, query, side="right") - 1
    values = np.full(len(query), np.nan)
    ages = np.full(len(query), np.nan)
    valid = (indices >= 0) & (query <= times[-1])
    values[valid] = windows[indices[valid]]
    ages[valid] = query[valid] - times[indices[valid]]
    return values, ages


def load_packets(path, case, summary):
    rows, histograms, payloads = [], {}, {}
    rtt = int(case["rtt_ms"])
    anchor = float(summary["first_data_tx_ms"])
    end = float(summary.get("data_acked_ms") or summary["last_observed_ms"])
    final_bin = math.floor((float(summary["last_data_tx_ms"])-anchor)/rtt)
    scale = int(summary.get("tick_scale", case.get("tick_scale", 1)))
    with open_text(path) as source:
        for raw in csv.DictReader(source):
            k = int(raw["rtt_index"])
            scope = raw["scope"]
            if k < 0 or scope not in ("all", "new", "retrans"):
                raise ValueError(f"{path}: invalid RTT index or scope")
            key = k, scope
            if key in histograms:
                raise ValueError(f"{path}: duplicate histogram {key}")
            slots = json.loads(raw["slot_counts"])
            if not isinstance(slots, list) or len(slots) != rtt or any(type(n) is not int or n < 0 for n in slots):
                raise ValueError(f"{path}: invalid slot_counts")
            first = float(raw["first_tx_ms"]) if raw["first_tx_ms"] else None
            last = float(raw["last_tx_ms"]) if raw["last_tx_ms"] else None
            start = anchor + k * rtt
            exposure = max(0, min(rtt, end - start))
            if exposure <= 0:
                raise ValueError(f"{path}: bin outside observed data interval")
            if sum(slots):
                if not raw["first_seq"] or not raw["last_seq"]:
                    raise ValueError(f"{path}: nonempty bin needs first/last sequence numbers")
                occupied = [i for i, n in enumerate(slots) if n]
                if (round((first-start)*scale)//scale != occupied[0] or
                        round((last-start)*scale)//scale != occupied[-1]):
                    raise ValueError(f"{path}: timestamps do not match the RTT bin")
                if last >= end:
                    raise ValueError(f"{path}: DATA timestamp beyond the observation end")
            elif first is not None or last is not None or raw["first_seq"] or raw["last_seq"]:
                raise ValueError(f"{path}: empty bin has packet metadata")
            if any(slots[i] for i in range(math.ceil(exposure), rtt)):
                raise ValueError(f"{path}: histogram includes unobserved time")
            metrics = packet_metrics(slots, first, last, rtt, exposure)
            if int(raw["packet_count"]) != metrics["packet_count"]:
                raise ValueError(f"{path}: packet_count differs from histogram sum")
            contains_final = k == final_bin
            eligible = metrics['full_rtt'] and metrics['dispersion_valid'] and not contains_final
            row = dict(case_id=case["case_id"], g=float(case["g"]) if "g" in case else float(case["g_hundredths"]) / 100,
                       g_effective=case.get("g_effective"), gain_q16=case.get("gain_q16"),
                       repeat=int(case["repeat"]), status=summary["status"],
                       rtt_index=k, start_rtt=k, first_tx_ms=first, last_tx_ms=last,
                       first_seq=raw["first_seq"], last_seq=raw["last_seq"],
                       leading_idle_ms=first - start if first is not None else None,
                       trailing_idle_ms=start + exposure - last if last is not None else None,
                       scope=scope, payload_bytes=int(raw["payload_bytes"]),
                       contains_final_send=contains_final, analysis_eligible=eligible,
                       middle_late=math.floor(.5*final_bin) <= k < math.floor(.9*final_bin), **metrics)
            rows.append(row)
            histograms[key] = slots
            payloads[key] = int(raw["payload_bytes"])
            if payloads[key] < 0 or (not sum(slots) and payloads[key]):
                raise ValueError(f"{path}: invalid payload byte count")
    expected_bins = math.ceil((end - anchor) / rtt)
    for k in range(expected_bins):
        for scope in ("all", "new", "retrans"):
            if (k, scope) not in histograms:
                raise ValueError(f"{path}: missing RTT {k}, scope {scope}; include empty bins")
        if any(a != n + r for a, n, r in zip(histograms[k, "all"],
                                             histograms[k, "new"], histograms[k, "retrans"])):
            raise ValueError(f"{path}: all histogram must equal new + retrans")
        if payloads[k, "all"] != payloads[k, "new"] + payloads[k, "retrans"]:
            raise ValueError(f"{path}: all payload bytes must equal new + retrans")
    return rows


def summarize_packets(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["g"], row["rtt_index"], row["scope"]].append(row)
    result = []
    metrics = ("packet_count", "span_rtt", "occupied_fraction", "burst_fraction",
               "hhi", "effective_ms", "gap_mean_ms", "gap_cv", "zero_gap_fraction")
    for (gain, k, scope), group in sorted(grouped.items()):
        base = dict(g=gain, rtt_index=k, scope=scope, observed_runs=len(group),
                    full_rtt_runs=sum(row["full_rtt"] for row in group),
                    valid_dispersion_runs=sum(row["analysis_eligible"] for row in group))
        for metric in metrics:
            eligible = [row[metric] for row in group if row["analysis_eligible"] and row[metric] is not None]
            base[metric + "_median"] = median(eligible) if eligible else None
        result.append(base)
    return result


def plot_windows(curves, settings, path):
    fig, axes = plt.subplots(4, 1, figsize=(13, 16))
    selected = representative_gains(list(curves))
    colors = dict(zip(sorted(curves), plt.cm.turbo(np.linspace(.03, .97, len(curves)))))
    scale = settings["mss_bytes"]
    common_end = min(x[-1] for x, y in curves.values())
    reference_gain = min(curves, key=lambda gain: abs(gain-2*math.log(2)))
    reference_x, reference_y = curves[reference_gain]
    for gain, (x, y) in sorted(curves.items()):
        axes[0].step(x, y/scale, where="post", color=colors[gain], alpha=.65,
                     linewidth=.9, label=f"{gain:.6g}")
        if gain in selected:
            for ax, low, high in ((axes[1], 0, 20), (axes[2], .5*common_end, .9*common_end)):
                mask = (x >= low) & (x <= high)
                ax.step(x[mask], y[mask]/scale, where="post", color=colors[gain],
                        linewidth=1.2, label=f"{gain:.6g}")
            mask = (x >= .5*common_end) & (x <= .9*common_end)
            indices = np.searchsorted(reference_x, x[mask], side="right")-1
            axes[3].step(x[mask], (y[mask]-reference_y[indices])/scale, where="post",
                         color=colors[gain], linewidth=1.2, label=f"{gain:.6g}")
    axes[0].set_title("All fixed gains: full observed transfer")
    axes[1].set_title("Representative gains: first 20 RTTs")
    axes[1].set_xlim(0, 20)
    axes[2].set_title("Representative gains: middle/late common observed interval")
    axes[2].set_xlim(.5*common_end, .9*common_end)
    axes[3].set_title(f"Middle/late difference from g={reference_gain:.9g}: overlapping curves remain visible")
    axes[3].set_xlim(.5*common_end, .9*common_end)
    axes[3].axhline(0, color="gray", linestyle=":", linewidth=.7)
    for ax in axes:
        ax.set(xlabel="Time since first actual DATA send / fixed feedback RTT", ylabel="cwnd / MSS")
        if settings["mode"] == "always_slow_start" and ax is not axes[3]:
            ax.set_yscale("log", base=2)
        ax.grid(alpha=0.25)
        ax.legend(title="Target g", ncol=4, fontsize=9)
    axes[3].set_ylabel("(cwnd - reference cwnd) / MSS")
    phase = "SS" if settings["mode"] == "always_slow_start" else "CA"
    fig.suptitle(f"Native SCP {phase} · {settings['bytes']/2**30:g} GiB · feedback RTT {settings['rtt_ms']} ms\n"
                 "Next-slot pacing · Q16 fixed gain · no loss or link serialization")
    fig.tight_layout(rect=[0, 0, 1, .96])
    fig.savefig(path, dpi=180)
    fig.savefig(path.with_suffix('.pdf'))
    plt.close(fig)


def plot_dispersion(rows, path):
    selected = [row for row in rows if row["scope"] == "all"]
    if not selected:
        return
    gains = sorted({row["g"] for row in selected})
    ks = list(range(max(row["rtt_index"] for row in selected) + 1))
    index_g, index_k = {g: i for i, g in enumerate(gains)}, {k: i for i, k in enumerate(ks)}
    fig, axes = plt.subplots(1, 3, figsize=(17, 7), sharey=True)
    for ax, metric, title in zip(axes,
            ("span_rtt_median", "burst_fraction_median", "hhi_median"),
            ("First-to-last span / feedback RTT", "Largest 1 ms burst / packet count", "1 ms concentration (HHI)")):
        matrix = np.full((len(gains), len(ks)), np.nan)
        for row in selected:
            if row[metric] is not None:
                matrix[index_g[row["g"]], index_k[row["rtt_index"]]] = row[metric]
        picture = ax.imshow(matrix, origin="lower", aspect="auto", vmin=0, vmax=1,
                            extent=[ks[0], ks[-1]+1, -0.5, len(gains)-0.5], interpolation="nearest")
        ax.set(title=title, xlabel="Fixed feedback RTT bin")
        fig.colorbar(picture, ax=ax, shrink=0.85)
    ticks = sorted(set(np.linspace(0, len(gains)-1, min(12, len(gains))).astype(int)))
    axes[0].set_yticks(ticks, [f"{gains[i]:g}" for i in ticks])
    axes[0].set_ylabel("g rows (ordered categories; reference gains have unequal spacing)")
    fig.suptitle("DATA transport-send callback dispersion · 1 ms resolution\n"
                 "Full nonfinal bins with at least two packets; blank cells are excluded")
    fig.tight_layout(rect=[0, 0, 1, 0.92])
    fig.savefig(path, dpi=180)
    fig.savefig(path.with_suffix('.pdf'))
    plt.close(fig)


def analyze(root, out, allow_incomplete):
    manifest = json.loads((root / "experiment.json").read_text(encoding="utf-8"))
    gains = {g['id']:float(g['value']) for g in manifest['gains']} if 'gains' in manifest else {g:g/100 for g in manifest['g_hundredths']}
    expected_g = set(gains)
    if len(expected_g) != len(manifest.get('gains', manifest.get('g_hundredths'))):
        raise ValueError("Duplicate gains in manifest")
    repeats = int(manifest["repeats"])
    if repeats < 1 or not expected_g or any(not math.isfinite(g) or g <= 0 for g in gains.values()):
        raise ValueError("Invalid gain list or repeats")
    expected = {(gain, repeat) for gain in expected_g for repeat in range(1, repeats+1)}
    cases, audit, packet_rows, seen = defaultdict(list), [], [], set()
    state_rows = []
    for config in sorted((root / "runs").rglob("config.json")):
        case = json.loads(config.read_text(encoding="utf-8"))
        if case.get('gain_policy', 'fixed_q16') != 'fixed_q16':
            raise ValueError('Dynamic native-gain arm cannot be merged into the fixed-g sweep')
        gain_index = case['gain_id'] if 'gain_id' in case else int(case['g_hundredths'])
        gain, repeat = float(case['g']) if 'g' in case else gain_index/100, int(case['repeat'])
        if gain_index not in gains or gain != gains[gain_index]:
            raise ValueError('Gain value or case identity disagrees with manifest')
        if 'gain_q16' in case and case['g_effective'] != case['gain_q16']/65536:
            raise ValueError('Effective gain disagrees with the Q16 callback')
        key = gain_index, repeat
        if key not in expected or key in seen:
            raise ValueError(f"{config}: unplanned or duplicate case {key}")
        seen.add(key)
        for field in ("rtt_ms", "rate_mbps", "bytes", "loss_ppm", "backend", "mode", "seed"):
            if case[field] != manifest[field]:
                raise ValueError(f"{config}: mixed experimental condition {field}")
        summary_path = config.parent / "summary.json"
        if not summary_path.exists():
            audit.append(dict(case_id=case["case_id"], g=gain, repeat=repeat, status="missing_summary"))
            continue
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        status = summary["status"]
        if status not in ("complete", "failed", "sim_timeout", "wall_timeout", "infra_error"):
            raise ValueError(f"{summary_path}: invalid status")
        if int(summary.get("dropped", 0)) != 0:
            raise ValueError(f"{summary_path}: dropped packets in a zero-loss experiment")
        if status == "complete" and (int(summary["received_bytes"]) != int(case["bytes"])
                                     or not summary["checksum_ok"]):
            raise ValueError(f"{summary_path}: complete status contradicts integrity checks")
        audit.append(dict(case_id=case["case_id"], g=gain, g_effective=case.get('g_effective'),
                          gain_q16=case.get('gain_q16'), repeat=repeat, status=status,
                          first_data_tx_ms=summary.get("first_data_tx_ms"),
                          data_acked_ms=summary.get("data_acked_ms"),
                          receiver_payload_complete_ms=summary.get("receiver_payload_complete_ms"),
                          close_kind=summary.get("close_kind", "unknown")))
        if summary.get("first_data_tx_ms") is None:
            continue
        anchor = float(summary["first_data_tx_ms"])
        end = float(summary.get("data_acked_ms") or summary["last_observed_ms"])
        if end <= anchor:
            raise ValueError(f"{summary_path}: invalid data observation interval")
        times, windows = load_states(config.parent / case["state_log"], int(case["stream_fd"]))
        if times[-1] < end:
            raise ValueError(f"{config}: final state snapshot before data observation end")
        rtt = int(case["rtt_ms"])
        boundary = np.arange((end-anchor)//rtt+1, dtype=np.int64) * rtt
        values, ages = past_samples(times, windows, anchor + boundary)
        for k, (value, age) in enumerate(zip(values, ages)):
            state_rows.append(dict(case_id=case["case_id"], g=gain, g_effective=case.get('g_effective'),
                                   gain_q16=case.get('gain_q16'), repeat=repeat,
                                   status=status, rtt_index=k, elapsed_ms=k*rtt,
                                   cwnd_bytes=int(value) if np.isfinite(value) else None,
                                   sample_age_ms=float(age) if np.isfinite(age) else None))
        cases[gain].append((anchor, end, times, windows))
        packet_rows.extend(load_packets(config.parent / case["packet_rtt_file"], case, summary))
    missing = sorted(expected - seen)
    unfinished = [row for row in audit if row["status"] == "missing_summary"]
    if (missing or unfinished) and not allow_incomplete:
        raise ValueError(f"Incomplete campaign: {len(missing)} missing configs, {len(unfinished)} missing summaries. "
                         "Use --allow-incomplete only for an explicitly partial report.")
    if not cases:
        raise ValueError("No observed DATA intervals")
    out.mkdir(parents=True, exist_ok=True)
    write_csv(out / "case_audit.csv", audit)
    write_csv(out / "cwnd_by_rtt_long.csv", state_rows)
    write_csv(out / "packet_dispersion_by_rtt.csv", packet_rows)
    packet_summary = summarize_packets(packet_rows)
    write_csv(out / "packet_dispersion_summary.csv", packet_summary)
    grouped = defaultdict(list)
    for row in state_rows:
        grouped[row["g"], row["rtt_index"]].append(row)
    wide, stats = defaultdict(dict), []
    for (gain, k), group in sorted(grouped.items()):
        valid = [row["cwnd_bytes"] for row in group if row["cwnd_bytes"] is not None]
        common = len(group) == len(cases[gain]) and len(valid) == len(group)
        mid = median(valid) if valid and common else None
        stats.append(dict(g=gain, rtt_index=k, observed_runs=len(valid),
                          campaign_repeats=repeats, group_observed_runs=len(cases[gain]),
                          common_support=common, cwnd_median_bytes=mid,
                          cwnd_min_bytes=min(valid) if valid else None,
                          cwnd_max_bytes=max(valid) if valid else None,
                          max_sample_age_ms=max((row["sample_age_ms"] for row in group
                                                if row["sample_age_ms"] is not None), default=None)))
        wide[k][f"g_{gain:.17g}" if 'gains' in manifest else f"g_{gain:.2f}"] = mid
    fields = ["rtt_index", "elapsed_ms"] + [f"g_{g:.17g}" if 'gains' in manifest else f"g_{g:.2f}" for g in sorted(gains.values())]
    write_csv(out / "cwnd_by_rtt.csv", [dict(rtt_index=k, elapsed_ms=k*manifest["rtt_ms"], **row)
                                        for k, row in sorted(wide.items())], fields)
    write_csv(out / "cwnd_rtt_statistics.csv", stats)
    curves = {}
    for gain, group in sorted(cases.items()):
        duration = min(end-anchor for anchor, end, times, windows in group)
        dense_end = min(duration, 5000)
        grid = np.unique(np.concatenate((np.arange(dense_end+1, dtype=np.int64),
                                         np.arange(5000, duration+1, 100, dtype=np.int64),
                                         np.asarray([duration], dtype=float))))
        matrix = np.asarray([past_samples(times, windows, anchor+grid)[0]
                             for anchor, end, times, windows in group])
        valid = np.isfinite(matrix).all(axis=0)
        curves[gain] = grid[valid]/manifest["rtt_ms"], np.median(matrix[:, valid], axis=0)
    plot_windows(curves, manifest, out / "cwnd_all_g.png")
    plot_dispersion(packet_summary, out / "packet_dispersion_all_g.png")
    report = dict(planned_runs=len(expected), recorded_configs=len(seen),
                  summaries=len(audit)-len(unfinished), observed_data_cases=sum(map(len, cases.values())),
                  missing_cases=[dict(gain_id=g, repeat=r) if 'gains' in manifest else dict(g_hundredths=g, repeat=r) for g, r in missing],
                  unfinished_cases=[row["case_id"] for row in unfinished],
                  statuses={status:sum(row["status"] == status for row in audit)
                            for status in sorted({row["status"] for row in audit})},
                  interpretation=["Fixed feedback RTT bins [nR,(n+1)R), not tail-packet genealogies.",
                                  "Only transport-send callback DATA; 1 ms timestamp resolution.",
                                  "State values use last observed sample; no linear interpolation or extrapolation.",
                                  "All available repetitions participate in cwnd common-support medians, including failure prefixes.",
                                  "Final DATA-send bin, partial bins and N<2 bins are excluded from dispersion aggregates.",
                                  "All raw bins remain in packet_dispersion_by_rtt.csv, including excluded bins.",
                                  "Middle/late means floor(0.5*final_send_bin) <= n < floor(0.9*final_send_bin).",
                                  "Zero-loss virtual repeats check reproducibility; they are not independent random network samples."])
    (out / "analysis_audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output":str(out), "planned":len(expected), "observed":report["observed_data_cases"],
                      "statuses":report["statuses"]}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="Directory containing experiment.json and runs/")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--allow-incomplete", action="store_true")
    args = parser.parse_args()
    try:
        analyze(args.root.resolve(), args.out.resolve(), args.allow_incomplete)
    except (ValueError, KeyError, OSError) as error:
        parser.exit(2, f"Analysis error: {error}\n")


if __name__ == "__main__":
    main()
