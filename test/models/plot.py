"""Plot observed packet-model and actual-SCP tables; no simulation is executed."""
import argparse
import csv
import gzip
import hashlib
import json
import math
import statistics
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

plt.rcParams.update({"font.size": 14, "axes.titlesize": 15,
                     "xtick.labelsize": 12, "ytick.labelsize": 12})
MSS, RTT = 1430, 100
COLORS = ["#0072B2", "#D55E00", "#009E73", "#CC79A7", "#E69F00", "#333333"]


def read(path):
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream))


def truth(value):
    return str(value).lower() == "true"


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1048576), b""):
            result.update(block)
    return result.hexdigest()


def label(gain):
    if gain == "policy":
        return "controller policy"
    if math.isclose(gain, 2 * math.log(2), abs_tol=1e-12):
        return r"$2\ln 2$"
    if math.isclose(gain, 1.5 * math.log(3), abs_tol=1e-12):
        return r"$3\ln 3/2$"
    return f"{gain:.6g}"


def native_boundaries(folder):
    config = json.loads((folder / "config.json").read_text())
    summary = json.loads((folder / "summary.json").read_text())
    assert summary["status"] == "complete" and summary["checksum_ok"]
    assert summary["feedback_min_ms"] == summary["feedback_max_ms"] == RTT
    assert summary["received_bytes"] == 1 << 30
    result, boundary, cwnd, count = [], 0, MSS, 0
    with gzip.open(folder / "ack_updates.csv.gz", "rt", newline="") as stream:
        for row in csv.DictReader(stream):
            elapsed = int(row["elapsed_ms"])
            while boundary < elapsed:
                result.append(dict(rtt=boundary // RTT, cwnd_bytes=cwnd, acked_packets=count))
                boundary += RTT
            cwnd, count = int(row["observed_cwnd_bytes"]), int(row["ack_index"])
    end = summary["data_acked_ms"] - summary["first_data_tx_ms"]
    while boundary <= end:
        result.append(dict(rtt=boundary // RTT, cwnd_bytes=cwnd, acked_packets=count))
        boundary += RTT
    return config, summary, result


def save(fig, folder, name):
    fig.tight_layout()
    fig.savefig(folder / (name + ".png"), dpi=220, bbox_inches="tight")
    fig.savefig(folder / (name + ".svg"), bbox_inches="tight")
    fig.savefig(folder / (name + ".pdf"), bbox_inches="tight", metadata={"CreationDate": None, "ModDate": None})
    plt.close(fig)


def panels(cases, phase, source, folder):
    reference = 1.5 * math.log(3) if phase == "ca" else 2 * math.log(2)
    chosen = [1, reference, 2, 4, 8, "policy"]
    fig, axes = plt.subplots(2, 2, figsize=(13, 8))
    for color, gain in zip(COLORS, chosen):
        case = cases.get((phase, source, gain))
        if case is None:
            continue
        rtts, groups = case["rtts"], case["groups"]
        axes[0, 0].step([float(r["rtt"]) for r in rtts],
                        [float(r["cwnd_bytes"]) / MSS for r in rtts], where="post", color=color, label=label(gain))
        complete = [g for g in groups if truth(g["complete"]) and not truth(g["final"])]
        x = [int(g["generation"]) for g in complete]
        for ax, field in [(axes[0, 1], "span"), (axes[1, 0], "occupied"), (axes[1, 1], "drift")]:
            ax.plot(x, [float(g[field]) / RTT for g in complete], color=color, label=label(gain))
    axes[0, 0].set(xlabel="Elapsed time / RTT", ylabel="cwnd / MSS", title="Window observations")
    if phase == "ss":
        axes[0, 0].set_yscale("log", base=2)
    for ax in axes.flat:
        ax.grid(alpha=.22)
    for ax, title, ylabel in [(axes[0, 1], "Complete descendant groups", "Actual first-to-last span / RTT"),
                             (axes[1, 0], "Retained trailing slot included", "Occupied span / RTT"),
                             (axes[1, 1], "Start delay accumulates", "Start drift / RTT")]:
        ax.set(xlabel="Descendant group index", ylabel=ylabel, title=title)
    axes[0, 1].axhline(1, color="gray", ls=":", lw=1)
    axes[1, 0].axhline(1, color="gray", ls=":", lw=1)
    axes[0, 0].legend(fontsize=9)
    fig.suptitle(f"{phase.upper()} | {source} | 100 ms feedback, zero loss, one MSS start", y=1.02)
    save(fig, folder, phase + "_" + source + "_groups")


def compare_ss(cases, checks, folder):
    references = {}
    for row in read(checks):
        references.setdefault(row["observed_run"], []).append(row)
    fig, axes = plt.subplots(2, 3, figsize=(14, 7), sharex="col")
    cohort, cohort_axes = plt.subplots(1, 3, figsize=(14, 4), sharey=True)
    for column, gain in enumerate((1, 2 * math.log(2), 2)):
        model, native = cases["ss", "python", gain], cases["ss", "scp", gain]
        ideal = references[model["directory"]]
        curves = [(native["rtts"], "cwnd_bytes", "SCP", "-"),
                  (model["rtts"], "cwnd_bytes", "Python", "--"),
                  (ideal, "computed_continuous_cwnd_bytes", "Ideal recurrence", ":")]
        for (rows, field, name, style), color in zip(curves, COLORS):
            axes[0, column].step([int(r["rtt"]) for r in rows],
                [float(r[field]) / MSS for r in rows], where="post", ls=style, color=color, label=name)
        measured = {int(r["rtt"]): float(r["cwnd_bytes"]) for r in native["rtts"]}
        for (rows, field, name, style), color in zip(curves[1:], COLORS[1:]):
            common = [(int(r["rtt"]), float(r[field])) for r in rows if int(r["rtt"]) in measured]
            axes[1, column].plot([n for n, value in common],
                [100 * (measured[n] / value - 1) for n, value in common], ls=style, color=color, label=name)
        for case, name, style, color in [(native, "SCP", "-", COLORS[0]),
                                         (model, "Python", "--", COLORS[1])]:
            groups = [g for g in case["groups"] if truth(g["complete"]) and not truth(g["final"])]
            cohort_axes[column].plot([int(g["generation"]) for g in groups],
                [float(g["span"]) / RTT for g in groups], ls=style, color=color, label=name)
        last = min(max(int(g["generation"]) for g in case["groups"]
                       if truth(g["complete"]) and not truth(g["final"])) for case in (native, model))
        x = range(1, last + 1)
        harmonic = [(2 * math.fsum(1 / k for k in range(2 ** (n - 1) + 1, 2 ** n + 1))
                     - 1 / 2 ** n) / gain for n in x]
        cohort_axes[column].plot(x, harmonic, ":", color=COLORS[2], label="Finite harmonic reference")
        cohort_axes[column].axhline(2 * math.log(2) / gain, color="gray", ls=":", lw=.8)
        for ax in (axes[0, column], axes[1, column], cohort_axes[column]):
            ax.grid(alpha=.2)
            ax.set_title("g = " + label(gain))
        axes[0, column].set_yscale("log", base=2)
        axes[1, column].set_xlabel("Elapsed time / RTT")
        cohort_axes[column].set_xlabel("Descendant group index")
    axes[0, 0].set_ylabel("cwnd / MSS")
    axes[1, 0].set_ylabel("(SCP / reference - 1) x 100%")
    cohort_axes[0].set_ylabel("Actual first-to-last span / RTT")
    axes[0, 0].legend(fontsize=11)
    axes[1, 0].legend(fontsize=11)
    cohort_axes[0].legend(fontsize=11)
    save(fig, folder, "ss-three-layers")
    save(cohort, folder, "ss-cohort-L")


def compare_ca(cases, folder, reference):
    fig, axes = plt.subplots(2, 3, figsize=(14, 7))
    for row, source in enumerate(("python", "scp")):
        for gain, color in zip((1, reference, 2, "policy"), COLORS):
            groups = [g for g in cases["ca", source, gain]["groups"]
                      if truth(g["complete"]) and not truth(g["final"])]
            for ax, field in zip(axes[row], ("span", "occupied", "drift")):
                ax.plot([int(g["generation"]) for g in groups],
                        [float(g[field]) / RTT for g in groups], color=color, label=label(gain))
        for ax, title in zip(axes[row], ("Actual span / RTT", "Occupied span / RTT", "Start drift / RTT")):
            ax.set(xlabel="Descendant group index", ylabel=source.upper(), title=title)
            ax.grid(alpha=.2)
        for ax in axes[row, :2]:
            ax.axhline(1, color="gray", ls=":", lw=.8)
    axes[0, 0].legend(fontsize=11)
    save(fig, folder, "ca-groups-compared")


def ca_sweep(cases, folder):
    selected = sorted((gain, case) for (phase, source, gain), case in cases.items()
                      if phase == "ca" and source == "python" and gain != "policy")
    if not any(math.isclose(g, 1.5 * math.log(3), abs_tol=1e-12) for g, _ in selected):
        return
    rows = []
    for gain, case in selected:
        row = dict(gain=gain)
        for stage, lo, hi in (("middle", 300, 600), ("late", 900, 1200)):
            groups = [g for g in case["groups"] if truth(g["complete"]) and not truth(g["final"])
                      and lo <= int(g["generation"]) <= hi]
            if len(groups) != hi - lo + 1:
                raise ValueError(f"Incomplete {stage} CA comparison window: g={gain}")
            spans = [float(g["span"]) for g in groups]
            row.update({stage + "_L_min_ms": min(spans), stage + "_L_median_ms": statistics.median(spans),
                        stage + "_L_max_ms": max(spans)})
            if stage == "late":
                row["late_occupied_median_ms"] = statistics.median(float(g["occupied"]) for g in groups)
                row["late_extra_start_total_ms"] = sum(float(g["next_start_extra_ms"]) for g in groups)
                row["start_drift_group1200_ms"] = float(groups[-1]["drift"])
        rows.append(row)
    with (folder / "ca-critical-sweep.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    for ax, zoom in zip(axes, (False, True)):
        for stage, color, marker in (("middle", COLORS[0], "o"), ("late", COLORS[1], "s")):
            data = [r for r in rows if not zoom or 1.49 <= r["gain"] <= 1.81]
            y = [r[stage + "_L_median_ms"] for r in data]
            errors = [[r[stage + "_L_median_ms"] - r[stage + "_L_min_ms"] for r in data],
                      [r[stage + "_L_max_ms"] - r[stage + "_L_median_ms"] for r in data]]
            ax.errorbar([r["gain"] for r in data], y, yerr=errors, marker=marker, color=color,
                        capsize=3, ms=4, lw=1, label=stage + ": median and group range")
        ax.axhline(RTT, color="black", ls=":", lw=1)
        ax.axvline(1.5 * math.log(3), color=COLORS[2], ls="--", lw=1, label=r"$C=3\ln3/2$")
        ax.set(xlabel="Fixed pacing gain g", ylabel="Actual group span L (ms)",
               title="Critical neighbourhood" if zoom else "Full sweep")
        ax.grid(alpha=.2)
        if not zoom:
            ax.set_xscale("log")
            ax.set_xticks([.9, 1, 1.5, 2, 3, 4, 8], labels=["0.9", "1", "1.5", "2", "3", "4", "8"])
    axes[0].legend(fontsize=9)
    fig.suptitle("Python CA clock model: 1 GiB, RTT 100 ms, no loss\n"
                 "Middle groups 300-600; late groups 900-1200; deterministic repeats checked separately", fontsize=12)
    save(fig, folder, "ca-critical-sweep")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-root", type=Path, required=True)
    parser.add_argument("--native-root", type=Path, action="append", default=[])
    parser.add_argument("--native-groups", type=Path, action="append", default=[])
    parser.add_argument("--checks", type=Path, help="check.py rtt_comparison.csv for paired SS references")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    cases, sources, summary_rows = {}, [], []
    for path in sorted(args.python_root.rglob("summary.csv")):
        sources.append(dict(path=str(path), sha256=digest(path)))
        for index, row in enumerate((r for r in read(path) if int(r["repeat"]) == 1), 1):
            if int(row["requested_bytes"]) != 1 << 30 or int(row["initial_mss"]) != 1:
                continue
            gain = "policy" if row["gain"] in ("adaptive", "policy") else float(row["gain"])
            directory = path.parent / f"g{index:02d}-r01"
            groups = read(directory / "generations.csv")
            case = dict(directory=str(directory), rtts=read(directory / "rtt.csv"), groups=[dict(g,
                span=g["span_ms"], occupied=g["occupancy_ms"], drift=g["start_drift_ms"],
                final=g["contains_last_packet"]) for g in groups])
            cases[row["mode"], "python", gain] = case
            for filename in ("rtt.csv", "generations.csv"):
                table = directory / filename
                sources.append(dict(path=str(table), sha256=digest(table)))
            full = [g for g in case["groups"] if truth(g["complete"]) and not truth(g["final"])]
            summary_rows.append(dict(phase=row["mode"], source="python", gain=gain,
                last_ack_ms=float(row["last_ack_ms"]), max_span_ms=max(float(g["span"]) for g in full),
                max_occupied_ms=max(float(g["occupied"]) for g in full), last_complete_drift_ms=float(full[-1]["drift"])))
    native_groups = {}
    for path in args.native_groups:
        sources.append(dict(path=str(path), sha256=digest(path)))
        for row in read(path):
            if int(row["repeat"]) != 1:
                continue
            phase = "ss" if row["phase"] == "always_slow_start" else "ca"
            gain = "policy" if row["gain_policy"] == "native_aimd" else float(row["g"])
            native_groups.setdefault((phase, gain), []).append(dict(row, span=row["actual_span_ms"],
                occupied=row["occupied_span_reconstructed_ms"], drift=row["start_drift_ms"], final=row["terminal"]))
    for root in args.native_root:
        for path in sorted(root.rglob("runs/*/config.json")):
            config = json.loads(path.read_text())
            if config["repeat"] != 1 or config["bytes"] != 1 << 30:
                continue
            config, summary, rtts = native_boundaries(path.parent)
            phase = "ss" if config["mode"] == "always_slow_start" else "ca"
            gain = "policy" if config["gain_policy"] == "native_aimd" else float(config["g"])
            case = dict(rtts=rtts, groups=native_groups.get((phase, gain), []))
            cases[phase, "scp", gain] = case
            ack_path = path.parent / "ack_updates.csv.gz"
            sources.append(dict(path=str(path), sha256=digest(path), ack_observations=str(ack_path), ack_sha256=digest(ack_path)))
            full = [g for g in case["groups"] if truth(g["complete"]) and not truth(g["final"])]
            if full:
                summary_rows.append(dict(phase=phase, source="scp", gain=gain,
                    last_ack_ms=summary["data_acked_ms"] - summary["first_data_tx_ms"],
                    max_span_ms=max(float(g["span"]) for g in full), max_occupied_ms=max(float(g["occupied"]) for g in full),
                    last_complete_drift_ms=float(full[-1]["drift"])))
    for phase in ("ss", "ca"):
        for source in ("python", "scp"):
            if any(key[:2] == (phase, source) for key in cases):
                panels(cases, phase, source, args.out)
        for source in ("python", "scp"):
            gains = sorted(g for p, s, g in cases if p == phase and s == source and g != "policy")
            if not gains:
                continue
            fig, ax = plt.subplots(figsize=(12, 5))
            for gain, color in zip(gains, plt.cm.viridis(np.linspace(0, 1, len(gains)))):
                rtts = cases[phase, source, gain]["rtts"]
                ax.step([float(r["rtt"]) for r in rtts], [float(r["cwnd_bytes"]) / MSS for r in rtts],
                        where="post", color=color, lw=1, label=label(gain))
            if phase == "ss": ax.set_yscale("log", base=2)
            ax.set(xlabel="Elapsed time / RTT", ylabel="cwnd / MSS", title=phase.upper() + ": all fixed gains, " + source + " observations")
            ax.grid(alpha=.2)
            ax.legend(ncol=4, fontsize=8)
            save(fig, args.out, phase + "_" + source + "_all_g_cwnd")
    ss_keys = {("ss", source, gain) for source in ("python", "scp") for gain in (1, 2 * math.log(2), 2)}
    ca_reference = 1.5 * math.log(3)
    if not all(("ca", source, ca_reference) in cases for source in ("python", "scp")):
        ca_reference = 2 * math.log(2)
    ca_keys = {("ca", source, gain) for source in ("python", "scp") for gain in (1, ca_reference, 2, "policy")}
    if args.checks and ss_keys.issubset(cases):
        compare_ss(cases, args.checks, args.out)
        sources.append(dict(path=str(args.checks), sha256=digest(args.checks)))
    elif args.checks:
        print("SS reference comparison skipped: requires paired SS runs at gains 1, 2 ln 2 and 2")
    if ca_keys.issubset(cases):
        compare_ca(cases, args.out, ca_reference)
    ca_sweep(cases, args.out)
    with (args.out / "results.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(summary_rows[0]))
        writer.writeheader()
        writer.writerows(summary_rows)
    with (args.out / "rtt_windows.csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("phase", "source", "gain", "rtt", "cwnd_bytes", "acked_packets"))
        for (phase, source, gain), case in cases.items():
            writer.writerows((phase, source, gain, r["rtt"], r["cwnd_bytes"], r["acked_packets"]) for r in case["rtts"])
    with (args.out / "group_metrics.csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("phase", "source", "gain", "generation", "span_ms", "occupied_ms", "start_drift_ms", "complete", "terminal"))
        for (phase, source, gain), case in cases.items():
            writer.writerows((phase, source, gain, r["generation"], r["span"], r["occupied"], r["drift"], r["complete"], r["final"]) for r in case["groups"])
    (args.out / "sources.json").write_text(json.dumps(dict(plot_sha256=digest(Path(__file__)),
        command=sys.argv, working_directory=str(Path.cwd()),
        method="Observed repeat 1; repeated table equality checked separately; no fitting or time shift",
        native_gain="Q16 effective gain differs from requested Python target", sources=sources), indent=2) + "\n")


if __name__ == "__main__":
    main()
