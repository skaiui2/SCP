#!/usr/bin/env python3
"""
Plot every useful field from an SCP JSONL state log.

Updated for the self-calibrating probability controller:
    prob_mode, prob_gamma, prob_beta,
    probe_low, probe_prev, probe_noise, last_cycle

The script parses the log once, unwraps the uint32 millisecond clock,
normalizes fixed-point fields, preserves controller transitions during
downsampling, and writes plots into one output directory.

Example:
    python3 plot_scp_all.py scp.log --out scp_all
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

try:
    import ujson as fast_json
except ImportError:
    fast_json = None

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt


FIELDS = [
    "t",
    "snd_una", "snd_seq_q", "snd_nxt", "rcv_nxt",
    "snd_wnd", "rcv_wnd", "snd_wmem", "rcv_wmem",
    "snd_q", "rcv_q",
    "cwnd", "flight",
    "srtt", "rtt_base", "rto", "dup_acks", "sb_cc",
    "packet_bytes", "packet_count",
    "cong_q", "cong_q_ema",
    "loss_cnt", "sent_cnt",
    "p", "p_ema", "d", "z",
    "cc_phase", "cc_id", "ssthresh",
    "prob_mode", "prob_gamma", "prob_beta",
    "probe_low", "probe_prev", "probe_noise", "last_cycle",
]

Q16_FIELDS = {"d", "z", "prob_gamma"}
Q_FIELDS = {"cong_q", "cong_q_ema"}
P_FIELDS = {"p", "p_ema"}
WINDOW_FIELDS = {
    "snd_wnd", "rcv_wnd", "snd_wmem", "rcv_wmem",
    "cwnd", "flight", "sb_cc", "ssthresh",
}
BYTE_COUNTER_FIELDS = {"packet_bytes"}
PROB_MODE_NAMES = {
    0: "DRAIN",
    1: "PROBE_UP",
    2: "NORMAL",
}
UINT32_MOD = float(2**32)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plot all fields from an SCP JSONL state log."
    )
    parser.add_argument("logfile", type=Path)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("scp_all_plots"),
        help="output directory (default: scp_all_plots)",
    )
    parser.add_argument(
        "--max-points",
        type=int,
        default=100_000,
        help="maximum points per line plot while preserving transitions",
    )
    parser.add_argument(
        "--scatter-points",
        type=int,
        default=30_000,
        help="maximum points per scatter plot",
    )
    parser.add_argument("--dpi", type=int, default=220)
    return parser.parse_args()


def json_loads(line: str) -> dict:
    if fast_json is not None:
        return fast_json.loads(line)
    return json.loads(line)


def load_log(path: Path) -> pd.DataFrame:
    rows: list[dict] = []

    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line.startswith("{"):
                continue

            try:
                obj = json_loads(line)
            except Exception:
                continue

            if obj.get("type") != "ss":
                continue

            rows.append({field: obj.get(field, np.nan) for field in FIELDS})

    if not rows:
        raise ValueError(f"No SCP state records found in {path}")

    df = pd.DataFrame.from_records(rows)
    for field in FIELDS:
        df[field] = pd.to_numeric(df[field], errors="coerce")

    df = df.dropna(subset=["t"]).reset_index(drop=True)
    if df.empty:
        raise ValueError("State records were found, but all timestamps are invalid.")

    return normalize_fields(df)


def unwrap_u32(values: pd.Series) -> np.ndarray:
    raw = values.to_numpy(dtype=np.float64)
    if len(raw) == 0:
        return raw

    delta = np.diff(raw)
    wraps = np.concatenate(([0], np.cumsum(delta < -(2**31))))
    return raw + wraps * UINT32_MOD


def u32_age_ms(now: pd.Series, then: pd.Series) -> np.ndarray:
    now_num = pd.to_numeric(now, errors="coerce").to_numpy(dtype=np.float64)
    then_num = pd.to_numeric(then, errors="coerce").to_numpy(dtype=np.float64)

    result = np.full(len(now_num), np.nan, dtype=np.float64)
    valid = np.isfinite(now_num) & np.isfinite(then_num) & (then_num != 0)
    if not valid.any():
        return result

    now_u = now_num[valid].astype(np.uint64)
    then_u = then_num[valid].astype(np.uint64)
    result[valid] = ((now_u - then_u) & np.uint64(0xFFFFFFFF)).astype(np.float64)
    return result


def normalize_fields(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    t_unwrapped = unwrap_u32(df["t"])
    df["time_s"] = (t_unwrapped - t_unwrapped[0]) / 1000.0

    # Missing prob_mode means an old log. Treat it as normal operation.
    df["prob_mode"] = df["prob_mode"].fillna(2).astype(np.int16)
    df["cc_phase"] = df["cc_phase"].fillna(0).astype(np.int16)

    df["q_inst"] = df["cong_q"] / 65535.0
    df["q_ema"] = df["cong_q_ema"] / 65535.0
    df["p_float"] = df["p"] / 65536.0
    df["p_ema_float"] = df["p_ema"] / 65536.0
    df["d_ms"] = df["d"] / 65536.0
    df["z_float"] = df["z"] / 65536.0
    df["gamma_float"] = df["prob_gamma"] / 65536.0
    df["beta_per_ms"] = df["prob_beta"] / 65536.0

    for field in WINDOW_FIELDS:
        df[f"{field}_kib"] = df[field] / 1024.0

    for field in BYTE_COUNTER_FIELDS:
        df[f"{field}_mib"] = df[field] / (1024.0 * 1024.0)

    beta = df["prob_beta"].to_numpy(dtype=np.float64)
    gamma = df["prob_gamma"].to_numpy(dtype=np.float64)

    model_low = np.full(len(df), np.nan)
    model_high = np.full(len(df), np.nan)
    valid = np.isfinite(beta) & np.isfinite(gamma) & (beta > 0)
    model_low[valid] = (-26573.0 - gamma[valid]) / beta[valid]
    model_high[valid] = (26573.0 - gamma[valid]) / beta[valid]

    df["model_d_low_ms"] = model_low
    df["model_d_high_ms"] = model_high
    df["model_span_ms"] = model_high - model_low
    df["cycle_age_s"] = u32_age_ms(df["t"], df["last_cycle"]) / 1000.0

    return df


def downsample_preserve_events(df: pd.DataFrame, max_points: int) -> pd.DataFrame:
    n = len(df)
    if max_points <= 0 or n <= max_points:
        return df.copy()

    stride = max(1, math.ceil(n / max_points))
    keep = set(range(0, n, stride))
    keep.update((0, n - 1))

    event_fields = [
        "cc_phase",
        "prob_mode",
        "prob_gamma",
        "prob_beta",
    ]
    for field in event_fields:
        changed = df[field].diff().fillna(0).to_numpy() != 0
        for pos in np.flatnonzero(changed):
            for j in range(max(0, pos - 2), min(n, pos + 3)):
                keep.add(j)

    return df.iloc[sorted(keep)].reset_index(drop=True)


def add_probe_spans(ax: plt.Axes, df: pd.DataFrame) -> None:
    if df.empty or "prob_mode" not in df:
        return

    t = df["time_s"].to_numpy()
    modes = df["prob_mode"].to_numpy()

    start = 0
    for i in range(1, len(df)):
        if modes[i] != modes[start]:
            if modes[start] != 2:
                ax.axvspan(t[start], t[i], alpha=0.08, linewidth=0)
            start = i

    if modes[start] != 2:
        ax.axvspan(t[start], t[-1], alpha=0.08, linewidth=0)


def series_for_field(df: pd.DataFrame, field: str) -> tuple[pd.Series, str]:
    if field == "cong_q":
        return df["q_inst"], "q"
    if field == "cong_q_ema":
        return df["q_ema"], "q EMA"
    if field == "p":
        return df["p_float"], "loss estimate"
    if field == "p_ema":
        return df["p_ema_float"], "loss EMA"
    if field == "d":
        return df["d_ms"], "effective delay (ms)"
    if field == "z":
        return df["z_float"], "logit"
    if field == "prob_gamma":
        return df["gamma_float"], "gamma"
    if field == "prob_beta":
        return df["beta_per_ms"], "beta (1/ms)"
    if field == "last_cycle":
        return df["cycle_age_s"], "time since complete cycle (s)"
    if field in WINDOW_FIELDS:
        return df[f"{field}_kib"], f"{field} (KiB)"
    if field in BYTE_COUNTER_FIELDS:
        return df[f"{field}_mib"], f"{field} (MiB)"
    return df[field], field


def plot_field(
    df: pd.DataFrame,
    field: str,
    outdir: Path,
    dpi: int,
) -> None:
    if field not in df or not df[field].notna().any():
        return

    y, ylabel = series_for_field(df, field)
    valid = y.notna() & df["time_s"].notna()
    if not valid.any():
        return

    fig, ax = plt.subplots(figsize=(12, 5))
    add_probe_spans(ax, df.loc[valid])
    ax.plot(df.loc[valid, "time_s"], y.loc[valid], linewidth=0.65)

    if field == "prob_mode":
        ax.set_yticks([0, 1, 2])
        ax.set_yticklabels(
            [PROB_MODE_NAMES[0], PROB_MODE_NAMES[1], PROB_MODE_NAMES[2]]
        )

    if field == "cc_phase":
        ax.set_yticks([0, 1])
        ax.set_yticklabels(["INC", "DEC"])

    ax.set_title(field)
    ax.set_xlabel("Time (s)")
    ax.set_ylabel(ylabel)
    ax.grid(True)
    fig.tight_layout()
    fig.savefig(outdir / f"{field}.png", dpi=dpi)
    plt.close(fig)


def sample_scatter(df: pd.DataFrame, max_points: int) -> pd.DataFrame:
    if max_points <= 0 or len(df) <= max_points:
        return df
    step = max(1, math.ceil(len(df) / max_points))
    return df.iloc[::step]


def scatter(
    df: pd.DataFrame,
    x: str,
    y: str,
    filename: str,
    outdir: Path,
    dpi: int,
    max_points: int,
    normal_only: bool = False,
) -> None:
    work = df
    if normal_only:
        work = work[work["prob_mode"] == 2]

    work = work[[x, y]].dropna()
    if work.empty:
        return

    work = sample_scatter(work, max_points)

    fig, ax = plt.subplots(figsize=(12, 5))
    ax.scatter(work[x], work[y], s=3, alpha=0.5)
    ax.set_title(f"{x} vs {y}")
    ax.set_xlabel(x)
    ax.set_ylabel(y)
    ax.grid(True)
    fig.tight_layout()
    fig.savefig(outdir / filename, dpi=dpi)
    plt.close(fig)


def plot_model_thresholds(df: pd.DataFrame, outdir: Path, dpi: int) -> None:
    if not df["model_d_low_ms"].notna().any():
        return

    fig, ax = plt.subplots(figsize=(12, 5))
    add_probe_spans(ax, df)

    ax.plot(df["time_s"], df["d_ms"], label="d", linewidth=0.7)
    ax.plot(
        df["time_s"],
        df["model_d_low_ms"],
        linestyle="--",
        label="model q=0.4",
    )
    ax.plot(
        df["time_s"],
        df["model_d_high_ms"],
        linestyle="-.",
        label="model q=0.6",
    )
    if df["probe_low"].notna().any():
        ax.plot(
            df["time_s"],
            df["probe_low"],
            linestyle=":",
            label="measured probe low",
        )

    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Effective delay (ms)")
    ax.set_title("Self-calibrated model thresholds")
    ax.grid(True)
    ax.legend()
    fig.tight_layout()
    fig.savefig(outdir / "model_thresholds.png", dpi=dpi)
    plt.close(fig)


def write_summary(df: pd.DataFrame, outdir: Path) -> None:
    duration = float(df["time_s"].iloc[-1]) if len(df) else 0.0
    normal_fraction = float((df["prob_mode"] == 2).mean())
    drain_fraction = float((df["prob_mode"] == 0).mean())
    up_fraction = float((df["prob_mode"] == 1).mean())

    lines = [
        "SCP plotting summary",
        "====================",
        f"Samples: {len(df)}",
        f"Duration: {duration:.3f} s",
        f"Mean cwnd: {df['cwnd_kib'].mean():.3f} KiB",
        f"Mean flight: {df['flight_kib'].mean():.3f} KiB",
        f"Mean SRTT: {df['srtt'].mean():.3f} ms",
        f"Mean q: {df['q_inst'].mean():.6f}",
        f"NORMAL sample fraction: {normal_fraction:.6f}",
        f"DRAIN sample fraction: {drain_fraction:.6f}",
        f"PROBE_UP sample fraction: {up_fraction:.6f}",
    ]

    if df["prob_beta"].notna().any():
        refits = (
            df["prob_beta"].diff().fillna(0).ne(0)
            | df["prob_gamma"].diff().fillna(0).ne(0)
        )
        lines.append(f"Observed model parameter changes: {int(refits.sum())}")

    (outdir / "summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    full_df = load_log(args.logfile)
    plot_df = downsample_preserve_events(full_df, args.max_points)

    for field in FIELDS:
        if field != "t":
            plot_field(plot_df, field, args.out, args.dpi)

    plot_model_thresholds(plot_df, args.out, args.dpi)

    scatter(
        plot_df,
        "d_ms",
        "q_inst",
        "d_vs_q.png",
        args.out,
        args.dpi,
        args.scatter_points,
        normal_only=True,
    )
    scatter(
        plot_df,
        "d_ms",
        "q_ema",
        "d_vs_q_ema.png",
        args.out,
        args.dpi,
        args.scatter_points,
        normal_only=True,
    )
    scatter(
        plot_df,
        "cwnd_kib",
        "d_ms",
        "cwnd_vs_d.png",
        args.out,
        args.dpi,
        args.scatter_points,
    )
    scatter(
        plot_df,
        "cwnd_kib",
        "q_inst",
        "cwnd_vs_q.png",
        args.out,
        args.dpi,
        args.scatter_points,
        normal_only=True,
    )

    write_summary(full_df, args.out)
    print(f"Wrote plots to: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())