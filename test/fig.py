#!/usr/bin/env python3
"""
Generate publication-quality figures from SCP JSONL state logs.

Expected JSON fields include:
    t, cwnd, flight, srtt, rto, cong_q, cong_q_ema,
    d, z, p, p_ema, cc_phase

Outputs:
    fig_q_hysteresis.{pdf,png}
    fig_window.{pdf,png}
    fig_rtt_signal.{pdf,png}
    fig_phase_portrait.{pdf,png}
    fig_switching_accuracy.{pdf,png}
    cycle_stats.csv
    run_summary.txt

Example:
    python3 plot_scp_paper.py scp.log --out figures
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Iterable

try:
    import ujson as fast_json
except ImportError:
    fast_json = None

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator, ScalarFormatter


FIELDS = [
    "t",
    "cwnd",
    "flight",
    "srtt",
    "rto",
    "rtt_base",
    "cong_q",
    "cong_q_ema",
    "d",
    "z",
    "p",
    "p_ema",
    "cc_phase",
    "loss_cnt",
    "sent_cnt",
    "packet_bytes",
    "packet_count",
]

Q_LOW = 0.4
Q_HIGH = 0.6
Q_DEC_STAR = 4.0 / 13.0
Q_INC_STAR = 9.0 / 13.0
UINT32_MOD = float(2**32)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create publication-quality SCP congestion-control figures."
    )
    parser.add_argument("logfile", type=Path, help="JSONL SCP state log")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("scp_figures"),
        help="output directory (default: scp_figures)",
    )
    parser.add_argument(
        "--max-points",
        type=int,
        default=30000,
        help="maximum plotted points while preserving phase transitions",
    )
    parser.add_argument(
        "--width",
        type=float,
        default=3.45,
        help="figure width in inches; 3.45 is IEEE single-column width",
    )
    parser.add_argument(
        "--height",
        type=float,
        default=2.35,
        help="figure height in inches",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=400,
        help="PNG resolution",
    )
    parser.add_argument(
        "--title",
        action="store_true",
        help="include titles inside figures; normally captions belong in the paper",
    )
    return parser.parse_args()


def configure_matplotlib() -> None:
    """Use a compact, vector-friendly paper style."""
    plt.rcParams.update(
        {
            "font.family": "serif",
            "font.size": 8.0,
            "axes.labelsize": 8.0,
            "axes.titlesize": 8.5,
            "legend.fontsize": 7.0,
            "xtick.labelsize": 7.0,
            "ytick.labelsize": 7.0,
            "axes.linewidth": 0.7,
            "lines.linewidth": 1.0,
            "lines.markersize": 3.0,
            "grid.linewidth": 0.45,
            "grid.alpha": 0.35,
            "legend.frameon": False,
            "figure.dpi": 120,
            "savefig.dpi": 400,
            "savefig.bbox": "tight",
            "savefig.pad_inches": 0.025,
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
            "axes.unicode_minus": True,
        }
    )


def json_loads(line: str) -> dict:
    if fast_json is not None:
        return fast_json.loads(line)
    return json.loads(line)


def load_log(path: Path) -> pd.DataFrame:
    rows: list[dict] = []

    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line.startswith("{"):
                continue

            try:
                obj = json_loads(line)
            except Exception:
                continue

            if obj.get("type") != "ss":
                continue

            row = {field: obj.get(field, np.nan) for field in FIELDS}
            rows.append(row)

    if not rows:
        raise ValueError(f"No JSON state records found in {path}")

    df = pd.DataFrame.from_records(rows)

    for field in FIELDS:
        df[field] = pd.to_numeric(df[field], errors="coerce")

    df = df.dropna(subset=["t", "cwnd", "cong_q", "cc_phase"]).reset_index(drop=True)

    if df.empty:
        raise ValueError("State records exist, but required fields are missing.")

    return normalize_fields(df)


def unwrap_u32_ms(values: pd.Series) -> np.ndarray:
    raw = values.to_numpy(dtype=np.float64)
    if len(raw) == 0:
        return raw

    delta = np.diff(raw)
    wraps = np.concatenate(([0], np.cumsum(delta < -(2**31))))
    return raw + wraps * UINT32_MOD


def normalize_fields(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    t_unwrapped = unwrap_u32_ms(df["t"])
    df["time_s"] = (t_unwrapped - t_unwrapped[0]) / 1000.0

    df["cwnd_kib"] = df["cwnd"] / 1024.0
    df["flight_kib"] = df["flight"] / 1024.0

    df["q_inst"] = df["cong_q"] / 65535.0
    df["q_ema"] = df["cong_q_ema"] / 65535.0

    df["d_ms"] = df["d"] / 65536.0
    df["z_float"] = df["z"] / 65536.0
    df["p_float"] = df["p"] / 65536.0
    df["p_ema_float"] = df["p_ema"] / 65536.0

    df["cc_phase"] = df["cc_phase"].fillna(0).astype(np.int8)

    if "rtt_base" in df:
        df["rtt_base"] = pd.to_numeric(df["rtt_base"], errors="coerce")

    return df


def downsample_preserve_switches(df: pd.DataFrame, max_points: int) -> pd.DataFrame:
    """
    Uniformly downsample while always preserving:
      - first and last sample
      - phase transitions
      - local q extrema near each transition
    """
    n = len(df)
    if n <= max_points or max_points <= 0:
        return df.copy()

    stride = max(1, math.ceil(n / max_points))
    keep = set(range(0, n, stride))
    keep.add(0)
    keep.add(n - 1)

    switch_positions = np.flatnonzero(df["cc_phase"].diff().fillna(0).to_numpy() != 0)
    for pos in switch_positions:
        for j in range(max(0, pos - 2), min(n, pos + 3)):
            keep.add(j)

    return df.iloc[sorted(keep)].reset_index(drop=True)


def add_phase_spans(ax: plt.Axes, df: pd.DataFrame) -> None:
    """Shade DEC intervals without relying on a fixed color palette."""
    if df.empty:
        return

    t = df["time_s"].to_numpy()
    phase = df["cc_phase"].to_numpy()

    start = 0
    for i in range(1, len(df)):
        if phase[i] != phase[start]:
            if phase[start] == 1:
                ax.axvspan(t[start], t[i], alpha=0.08, linewidth=0)
            start = i

    if phase[start] == 1:
        ax.axvspan(t[start], t[-1], alpha=0.08, linewidth=0)


def style_axis(ax: plt.Axes) -> None:
    ax.grid(True, which="major")
    ax.tick_params(direction="in", top=True, right=True, length=3)
    ax.xaxis.set_major_locator(MaxNLocator(nbins=6))
    ax.yaxis.set_major_locator(MaxNLocator(nbins=6))


def maybe_title(ax: plt.Axes, text: str, enabled: bool) -> None:
    if enabled:
        ax.set_title(text)


def save_figure(fig: plt.Figure, outdir: Path, stem: str, dpi: int) -> None:
    fig.savefig(outdir / f"{stem}.pdf")
    fig.savefig(outdir / f"{stem}.png", dpi=dpi)
    plt.close(fig)


def plot_q_hysteresis(
    df: pd.DataFrame,
    outdir: Path,
    width: float,
    height: float,
    dpi: int,
    title: bool,
) -> None:
    fig, ax = plt.subplots(figsize=(width, height))

    add_phase_spans(ax, df)

    ax.plot(df["time_s"], df["q_inst"], label=r"$q$")
    ax.plot(
        df["time_s"],
        df["q_ema"],
        linestyle="--",
        label=r"$\bar{q}$",
    )

    ax.axhline(Q_LOW, linestyle=":", linewidth=0.9, label=r"$q_L=0.4$")
    ax.axhline(Q_HIGH, linestyle="-.", linewidth=0.9, label=r"$q_H=0.6$")

    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Congestion factor")
    ax.set_ylim(0.0, 1.0)
    maybe_title(ax, "Hysteretic congestion signal", title)
    style_axis(ax)
    ax.legend(ncol=2, loc="best")

    save_figure(fig, outdir, "fig_q_hysteresis", dpi)


def plot_window(
    df: pd.DataFrame,
    outdir: Path,
    width: float,
    height: float,
    dpi: int,
    title: bool,
) -> None:
    fig, ax = plt.subplots(figsize=(width, height))

    add_phase_spans(ax, df)

    ax.plot(df["time_s"], df["cwnd_kib"], label="cwnd")
    ax.plot(
        df["time_s"],
        df["flight_kib"],
        linestyle="--",
        label="in flight",
    )

    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Window (KiB)")
    ax.set_ylim(bottom=0)
    maybe_title(ax, "Congestion window dynamics", title)
    style_axis(ax)
    ax.legend(loc="best")

    save_figure(fig, outdir, "fig_window", dpi)


def plot_rtt_signal(
    df: pd.DataFrame,
    outdir: Path,
    width: float,
    height: float,
    dpi: int,
    title: bool,
) -> None:
    fig, ax = plt.subplots(figsize=(width, height))

    add_phase_spans(ax, df)

    ax.plot(df["time_s"], df["srtt"], label="SRTT")
    ax.plot(
        df["time_s"],
        df["d_ms"],
        linestyle="--",
        label=r"$d$",
    )

    if df["rto"].notna().any():
        ax.plot(
            df["time_s"],
            df["rto"],
            linestyle=":",
            label="RTO",
        )

    if df["rtt_base"].notna().any():
        ax.plot(
            df["time_s"],
            df["rtt_base"],
            linestyle="-.",
            label="base RTT",
        )

    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Time (ms)")
    ax.set_ylim(bottom=0)
    maybe_title(ax, "RTT-derived control signal", title)
    style_axis(ax)
    ax.legend(loc="best")

    save_figure(fig, outdir, "fig_rtt_signal", dpi)


def plot_phase_portrait(
    df: pd.DataFrame,
    outdir: Path,
    width: float,
    height: float,
    dpi: int,
    title: bool,
) -> None:
    fig, ax = plt.subplots(figsize=(width, height))

    inc = df[df["cc_phase"] == 0]
    dec = df[df["cc_phase"] == 1]

    if not inc.empty:
        ax.scatter(
            inc["q_inst"],
            inc["cwnd_kib"],
            s=5,
            alpha=0.45,
            marker="o",
            label="INC",
            rasterized=True,
        )

    if not dec.empty:
        ax.scatter(
            dec["q_inst"],
            dec["cwnd_kib"],
            s=7,
            alpha=0.55,
            marker="x",
            label="DEC",
            rasterized=True,
        )

    ax.axvline(Q_LOW, linestyle=":", linewidth=0.9)
    ax.axvline(Q_HIGH, linestyle="-.", linewidth=0.9)

    ax.set_xlabel("Congestion factor")
    ax.set_ylabel("cwnd (KiB)")
    ax.set_xlim(0.0, 1.0)
    ax.set_ylim(bottom=0)
    maybe_title(ax, "Hybrid-controller phase portrait", title)
    style_axis(ax)
    ax.legend(loc="best")

    save_figure(fig, outdir, "fig_phase_portrait", dpi)


def transition_table(df: pd.DataFrame) -> pd.DataFrame:
    previous = df["cc_phase"].shift(1)
    changed = previous.notna() & (previous != df["cc_phase"])

    events = df.loc[
        changed,
        ["time_s", "q_inst", "q_ema", "cwnd_kib", "cc_phase"],
    ].copy()

    if events.empty:
        events["transition"] = pd.Series(dtype=str)
        return events

    events["transition"] = np.where(
        events["cc_phase"] == 1,
        "INC→DEC",
        "DEC→INC",
    )
    events["event"] = np.arange(1, len(events) + 1)

    return events.reset_index(drop=True)


def plot_switching_accuracy(
    events: pd.DataFrame,
    outdir: Path,
    width: float,
    height: float,
    dpi: int,
    title: bool,
) -> None:
    if events.empty:
        return

    fig, ax = plt.subplots(figsize=(width, height))

    high = events[events["transition"] == "INC→DEC"]
    low = events[events["transition"] == "DEC→INC"]

    if not high.empty:
        ax.scatter(
            high["event"],
            high["q_inst"],
            marker="^",
            s=18,
            label=r"INC$\rightarrow$DEC",
        )

    if not low.empty:
        ax.scatter(
            low["event"],
            low["q_inst"],
            marker="v",
            s=18,
            label=r"DEC$\rightarrow$INC",
        )

    ax.axhline(Q_LOW, linestyle=":", linewidth=0.9, label=r"$q_L=0.4$")
    ax.axhline(Q_HIGH, linestyle="-.", linewidth=0.9, label=r"$q_H=0.6$")

    ax.set_xlabel("Switch event")
    ax.set_ylabel("Observed switching factor")
    ax.set_ylim(0.0, 1.0)
    ax.xaxis.set_major_locator(MaxNLocator(integer=True, nbins=7))
    maybe_title(ax, "Observed hysteresis switching points", title)
    style_axis(ax)
    ax.legend(ncol=2, loc="best")

    save_figure(fig, outdir, "fig_switching_accuracy", dpi)


def compute_cycle_stats(df: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    """
    Define one complete cycle from a DEC→INC switch to the next DEC→INC switch.
    """
    low_events = events[events["transition"] == "DEC→INC"].copy()

    if len(low_events) < 2:
        return pd.DataFrame()

    rows: list[dict] = []

    for cycle_no in range(len(low_events) - 1):
        start_t = float(low_events.iloc[cycle_no]["time_s"])
        end_t = float(low_events.iloc[cycle_no + 1]["time_s"])

        segment = df[(df["time_s"] >= start_t) & (df["time_s"] < end_t)]
        if segment.empty:
            continue

        high_switch = events[
            (events["transition"] == "INC→DEC")
            & (events["time_s"] >= start_t)
            & (events["time_s"] < end_t)
        ]

        rows.append(
            {
                "cycle": cycle_no + 1,
                "start_s": start_t,
                "end_s": end_t,
                "period_s": end_t - start_t,
                "q_min": segment["q_inst"].min(),
                "q_max": segment["q_inst"].max(),
                "q_mean": segment["q_inst"].mean(),
                "q_std": segment["q_inst"].std(ddof=0),
                "cwnd_min_kib": segment["cwnd_kib"].min(),
                "cwnd_max_kib": segment["cwnd_kib"].max(),
                "cwnd_mean_kib": segment["cwnd_kib"].mean(),
                "srtt_mean_ms": segment["srtt"].mean(),
                "low_switch_q": float(low_events.iloc[cycle_no]["q_inst"]),
                "high_switch_q": (
                    float(high_switch.iloc[0]["q_inst"])
                    if not high_switch.empty
                    else np.nan
                ),
            }
        )

    return pd.DataFrame(rows)


def write_summary(
    df: pd.DataFrame,
    events: pd.DataFrame,
    cycles: pd.DataFrame,
    outdir: Path,
) -> None:
    lines = [
        "SCP run summary",
        "===============",
        f"Samples: {len(df)}",
        f"Duration: {df['time_s'].iloc[-1]:.3f} s",
        f"Mean q: {df['q_inst'].mean():.6f}",
        f"Min q: {df['q_inst'].min():.6f}",
        f"Max q: {df['q_inst'].max():.6f}",
        f"Mean cwnd: {df['cwnd_kib'].mean():.3f} KiB",
        f"Min cwnd: {df['cwnd_kib'].min():.3f} KiB",
        f"Max cwnd: {df['cwnd_kib'].max():.3f} KiB",
        f"Mean SRTT: {df['srtt'].mean():.3f} ms",
        f"Switch events: {len(events)}",
        f"Complete cycles: {len(cycles)}",
        "",
        f"Configured q_low: {Q_LOW:.6f}",
        f"Configured q_high: {Q_HIGH:.6f}",
        f"Virtual q_dec*: {Q_DEC_STAR:.9f}",
        f"Virtual q_inc*: {Q_INC_STAR:.9f}",
    ]

    if not cycles.empty:
        lines.extend(
            [
                "",
                "Cycle statistics",
                "----------------",
                f"Mean period: {cycles['period_s'].mean():.6f} s",
                f"Period std: {cycles['period_s'].std(ddof=0):.6f} s",
                f"Mean cycle q: {cycles['q_mean'].mean():.6f}",
                f"Mean q minimum: {cycles['q_min'].mean():.6f}",
                f"Mean q maximum: {cycles['q_max'].mean():.6f}",
                f"Mean low-switch q: {cycles['low_switch_q'].mean():.6f}",
                f"Mean high-switch q: {cycles['high_switch_q'].mean():.6f}",
            ]
        )

    (outdir / "run_summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    configure_matplotlib()
    args.out.mkdir(parents=True, exist_ok=True)

    full_df = load_log(args.logfile)
    plot_df = downsample_preserve_switches(full_df, args.max_points)

    events = transition_table(full_df)
    cycles = compute_cycle_stats(full_df, events)

    if not cycles.empty:
        cycles.to_csv(args.out / "cycle_stats.csv", index=False)
    else:
        pd.DataFrame(
            columns=[
                "cycle",
                "start_s",
                "end_s",
                "period_s",
                "q_min",
                "q_max",
                "q_mean",
                "q_std",
                "cwnd_min_kib",
                "cwnd_max_kib",
                "cwnd_mean_kib",
                "srtt_mean_ms",
                "low_switch_q",
                "high_switch_q",
            ]
        ).to_csv(args.out / "cycle_stats.csv", index=False)

    events.to_csv(args.out / "switch_events.csv", index=False)

    plot_q_hysteresis(
        plot_df, args.out, args.width, args.height, args.dpi, args.title
    )
    plot_window(
        plot_df, args.out, args.width, args.height, args.dpi, args.title
    )
    plot_rtt_signal(
        plot_df, args.out, args.width, args.height, args.dpi, args.title
    )
    plot_phase_portrait(
        plot_df, args.out, args.width, args.height, args.dpi, args.title
    )
    plot_switching_accuracy(
        events, args.out, args.width, args.height, args.dpi, args.title
    )

    write_summary(full_df, events, cycles, args.out)

    print(f"Wrote figures and statistics to: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())