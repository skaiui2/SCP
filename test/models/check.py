"""Evaluate ACK recurrences and check packet-model observations; this is not SCP or a clock simulation."""
import argparse
import bisect
import csv
import gzip
import hashlib
import json
import math
from array import array
from collections import defaultdict
from fractions import Fraction
from pathlib import Path

MSS, RTT_US = 1430, 100000
BACKEND = "mathematical_recurrence_evaluation"


def adaptive_counterexample():
    w = Fraction(1)
    sent, gaps, generations = [Fraction(0)], [Fraction(2, 3)], [0]
    slot = Fraction(2, 3)
    for k in range(1, 13):
        w += 1 / w
        gap = 1 / (w + Fraction(1, 2))
        slot = max(slot, sent[k - 1] + 1)
        while len(sent) < min(17, k + w.numerator // w.denominator):
            generations.append(generations[k - 1] + 1)
            sent.append(slot)
            gaps.append(gap)
            slot += gap
    group = [j for j, n in enumerate(generations) if n == 5]
    span = sent[group[-1]] - sent[group[0]]
    occupied = span + gaps[group[-1]]
    assert len(group) == 5 and span < 1 < occupied
    return dict(backend="exact_rational_mathematical_evaluation", native_scp_execution=False,
                policy="1+1/(2w)", initial_mss=1, group=5, packets=5,
                span_over_rtt=float(span), occupancy_over_rtt=float(occupied),
                next_start_extra_over_rtt=float(occupied - 1),
                exact_span_lt_one=span < 1, exact_occupancy_gt_one=occupied > 1,
                scope="Fraction arithmetic, first 12 ACKs; finite witness only")


def gain_at(mode, fixed, window):
    if fixed == "legacy-adaptive":
        return 2 * math.log(2) if mode == "ss" else 1 + 1 / (2 * window)
    if fixed not in ("adaptive", "policy"):
        return float(fixed)
    return 2 * math.log(2) if mode == "ss" else 1.5 * math.log(3)


def evaluate(mode, fixed, total, initial, truncate_gap, continuous=False, ca_update="real"):
    sent, reserved, gaps, windows = (array("d") for _ in range(4))
    generations, assigned = array("I"), array("I")
    expected, slot, window = {0: initial}, 0.0, float(initial)
    byte_window, credit = initial * MSS, 0
    credits = array("Q", [0])
    windows.append(window)

    def append(limit, generation, ack_index, gap):
        nonlocal slot
        while len(sent) < min(total, limit):
            reserved.append(slot)
            sent.append(slot if continuous else math.floor(slot))
            gaps.append(gap)
            generations.append(generation)
            assigned.append(ack_index)
            slot += gap

    gap = RTT_US / (gain_at(mode, fixed, window) * window)
    append(math.floor(window), 0, 0, math.floor(gap) if truncate_gap else gap)
    for k in range(1, total + 1):
        arrival = sent[k - 1] + RTT_US
        if mode == "ss":
            window += 1
        elif ca_update == "real":
            window += 1 / window
        else:
            if ca_update == "byte-floor":
                byte_window += max(1, MSS * MSS // byte_window)
            else:
                increase, credit = divmod(credit + MSS * MSS, byte_window)
                byte_window += increase
            window = byte_window / MSS
        windows.append(window)
        credits.append(credit)
        generation = generations[k - 1] + 1
        expected[generation] = math.floor(window)
        gap = RTT_US / (gain_at(mode, fixed, window) * window)
        slot = max(slot, arrival)
        append(k + math.floor(window), generation, k, math.floor(gap) if truncate_gap else gap)
    return dict(sent=sent, reserved=reserved, gaps=gaps, generations=generations,
                assigned=assigned, windows=windows, credits=credits, expected=expected,
                last_ack_ms=(sent[-1] + RTT_US) / 1000)


def spans(reference, generation):
    result = {}
    times, labels = reference["sent"], reference["generations"]
    for j, now in enumerate(times):
        key = labels[j] if generation else int(now // RTT_US)
        if key not in result:
            result[key] = dict(first=j, last=j, count=0, max_gap=0, bucket=-1, streak=0, burst=0)
        row = result[key]
        if row["count"]:
            row["max_gap"] = max(row["max_gap"], now - times[row["last"]])
        bucket = int(now // 1000)
        row["streak"] = row["streak"] + 1 if bucket == row["bucket"] else 1
        row["bucket"], row["last"] = bucket, j
        row["burst"] = max(row["burst"], row["streak"])
        row["count"] += 1
    return result


def group_rows(reference):
    observed = spans(reference, True)
    times, slots, gaps = reference["sent"], reference["reserved"], reference["gaps"]
    result = {}
    for n, row in observed.items():
        first, last, count = row["first"], row["last"], row["count"]
        start, end = times[first], times[last]
        tail = slots[last] + gaps[last]
        following = observed.get(n + 1)
        result[n] = dict(first_send_ms=start / 1000, last_send_ms=end / 1000,
                         span_ms=(end - start) / 1000, span_over_rtt=(end - start) / RTT_US,
                         packets=count, contains_last_packet=last + 1 == len(times),
                         complete=count == reference["expected"][n], expected_packets=reference["expected"][n],
                         tail_slot_ms=tail / 1000, occupancy_ms=(tail - start) / 1000,
                         start_drift_ms=(start - n * RTT_US) / 1000,
                         next_start_extra_ms=(times[following["first"]] - start - RTT_US) / 1000 if following else "",
                         first_reserved_ms=slots[first] / 1000, last_reserved_ms=slots[last] / 1000,
                         reservation_span_ms=(slots[last] - slots[first]) / 1000,
                         tail_gap_ms=gaps[last] / 1000, tail_residual_ms=(tail - end) / 1000)
    return result


def rtt_row(reference, bins, n):
    times, windows = reference["sent"], reference["windows"]
    acked = bisect.bisect_right(times, (n - 1) * RTT_US)
    sent_count = bisect.bisect_right(times, n * RTT_US)
    scheduled = min(len(times), acked + math.floor(windows[acked]))
    row = bins.get(n)
    count = row["count"] if row else 0
    return dict(cwnd_bytes=windows[acked] * MSS, flight_bytes=(scheduled - acked) * MSS,
                sent_unacked_bytes=(sent_count - acked) * MSS, queued_bytes=(scheduled - sent_count) * MSS,
                acked_packets=acked, first_send_ms=times[row["first"]] / 1000 if row else "",
                last_send_ms=times[row["last"]] / 1000 if row else "",
                span_ms=(times[row["last"]] - times[row["first"]]) / 1000 if row else "",
                packets=count, last_data_bin=row["last"] + 1 == len(times) if row else False,
                max_1ms_packets=row["burst"] if row else 0,
                max_1ms_share=row["burst"] / count if row else "",
                max_internal_gap_ms=row["max_gap"] / 1000 if row else 0)


def difference(actual, predicted):
    if isinstance(predicted, bool):
        return 0 if actual == str(predicted) else math.inf
    if predicted == "":
        return 0 if actual == "" else math.inf
    if actual == "":
        return math.inf
    error = abs(float(actual) - predicted)
    return 0 if math.isclose(float(actual), predicted, rel_tol=2e-12, abs_tol=1e-6) else error


def check_trace(path, reference):
    checked, worst, first = 0, 0.0, ""
    if not path.exists():
        return checked, worst, first
    acked = 0
    with path.open(newline="") as stream:
        for row in csv.DictReader(stream):
            j = int(row["seq"]) // MSS
            event = row["event"]
            if event == "ACK_RECEIVED":
                acked += 1
            due = reference["sent"][j]
            if event in ("DATA_RECEIVED", "ACK_SENT"):
                due += RTT_US / 2
            elif event == "ACK_RECEIVED":
                due += RTT_US
            elif event == "RESERVE":
                k = reference["assigned"][j]
                due = reference["sent"][k - 1] + RTT_US if k else 0
            fields = dict(now_ms=due / 1000, cwnd_bytes=reference["windows"][acked] * MSS,
                          reserved_us=reference["reserved"][j], gap_us=reference["gaps"][j])
            for field, predicted in fields.items():
                error = difference(row[field], predicted)
                checked += 1
                if error:
                    first = first or f"trace:{event}:packet{j}:{field}"
                    worst = max(worst, error)
    return checked, worst, first


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    cases, sources = defaultdict(list), {}
    for path in sorted(args.runs.rglob("summary.csv")):
        sources[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        indices = {}
        with path.open(newline="") as stream:
            for row in csv.DictReader(stream):
                row.setdefault("ca_update", "real")
                if row["gain"] == "adaptive" and row.get("gain_policy") == "continuous_aimd_candidate":
                    row["gain"] = "legacy-adaptive"
                key = tuple(row[f] for f in ("mode", "gain", "packets", "initial_mss", "pacing_rounding", "ca_update"))
                indices.setdefault(key, len(indices) + 1)
                directory = path.parent / f"g{indices[key]:02d}-r{int(row['repeat']):02d}"
                cases[key].append((directory, row))
    if not cases:
        raise ValueError("No packet-model summaries found")
    args.out.mkdir(parents=True, exist_ok=False)
    (args.out / "exact_ca_policy_counterexample.json").write_text(
        json.dumps(adaptive_counterexample(), indent=2) + "\n", encoding="utf-8")
    failed = 0
    with (args.out / "checks.csv").open("w", newline="") as checks, \
         (args.out / "rtt_comparison.csv").open("w", newline="") as rtts, \
         (args.out / "group_comparison.csv").open("w", newline="") as groups:
        cw, rw, gw = csv.writer(checks), csv.writer(rtts), csv.writer(groups)
        cw.writerow(("computed_backend", "observed_run", "checks", "passed", "largest_failed_error", "first_failed_coordinate"))
        rw.writerow(("observed_run", "rtt", "observed_cwnd_bytes", "computed_tick_cwnd_bytes", "computed_continuous_cwnd_bytes",
                     "tick_minus_observed_bytes", "tick_minus_continuous_bytes", "observed_acked_packets", "computed_acked_packets"))
        gw.writerow(("observed_run", "generation", "observed_span_ms", "computed_tick_span_ms", "computed_continuous_span_ms",
                     "observed_start_drift_ms", "computed_tick_start_drift_ms", "computed_continuous_start_drift_ms",
                     "observed_occupancy_ms", "computed_tick_occupancy_ms", "computed_continuous_occupancy_ms"))
        for (mode, gain, total, initial, rounding, ca_update), entries in cases.items():
            reference = evaluate(mode, gain, int(total), int(initial), rounding.startswith("floor_each_gap"), ca_update=ca_update)
            ideal = evaluate(mode, gain, int(total), int(initial), False, True, ca_update)
            bins, ideal_bins = spans(reference, False), spans(ideal, False)
            generation_rows, ideal_groups = group_rows(reference), group_rows(ideal)
            for directory, summary in entries:
                count, worst, first = check_trace(directory / "events.csv", reference)
                ack_path = directory / "ack_windows.csv.gz"
                if ack_path.exists():
                    ack_count = 0
                    with gzip.open(ack_path, "rt", newline="") as source:
                        for k, observed in enumerate(csv.DictReader(source), 1):
                            ack_count += 1
                            if k > int(total):
                                raise ValueError(f"Extra per-ACK window: {directory}")
                            for field, predicted in (("ack_index", k), ("now_us", reference["sent"][k - 1] + RTT_US),
                                                     ("cwnd_bytes", reference["windows"][k] * MSS),
                                                     ("ca_credit", reference["credits"][k])):
                                error = difference(observed[field], predicted)
                                count += 1
                                if error:
                                    first = first or f"ack_windows:{k}:{field}"
                                    worst = max(worst, error)
                    if ack_count != int(total):
                        raise ValueError(f"Missing per-ACK windows: {directory}")
                for field, predicted in (("last_ack_ms", reference["last_ack_ms"]),
                                         ("final_cwnd_bytes", reference["windows"][-1] * MSS),
                                         ("zero_gap_reservations", sum(g == 0 for g in reference["gaps"]))):
                    error = difference(summary[field], predicted)
                    count += 1
                    if error:
                        first = first or f"summary:{field}"
                        worst = max(worst, error)
                if "final_ca_credit" in summary:
                    error = difference(summary["final_ca_credit"], reference["credits"][-1])
                    count += 1
                    if error:
                        first = first or "summary:final_ca_credit"
                        worst = max(worst, error)
                for filename, coordinate in (("rtt.csv", "rtt"), ("generations.csv", "generation")):
                    with (directory / filename).open(newline="") as stream:
                        seen = set()
                        for observed in csv.DictReader(stream):
                            n = int(observed[coordinate])
                            if n in seen:
                                raise ValueError(f"Duplicate {coordinate}: {directory}, {n}")
                            seen.add(n)
                            predicted = rtt_row(reference, bins, n) if coordinate == "rtt" else generation_rows[n]
                            continuous = rtt_row(ideal, ideal_bins, n) if coordinate == "rtt" else ideal_groups[n]
                            for field, value in predicted.items():
                                error = difference(observed[field], value)
                                count += 1
                                if error:
                                    first = first or f"{filename}:{n}:{field}"
                                    worst = max(worst, error)
                            if coordinate == "rtt":
                                rw.writerow((str(directory), n, observed["cwnd_bytes"], predicted["cwnd_bytes"], continuous["cwnd_bytes"],
                                             predicted["cwnd_bytes"] - float(observed["cwnd_bytes"]),
                                             predicted["cwnd_bytes"] - continuous["cwnd_bytes"], observed["acked_packets"], predicted["acked_packets"]))
                            else:
                                gw.writerow((str(directory), n, observed["span_ms"], predicted["span_ms"], continuous["span_ms"],
                                             observed["start_drift_ms"], predicted["start_drift_ms"], continuous["start_drift_ms"],
                                             observed["occupancy_ms"], predicted["occupancy_ms"], continuous["occupancy_ms"]))
                        expected = set(range(int(reference["last_ack_ms"] // 100) + 1)) if coordinate == "rtt" else set(generation_rows)
                        if seen != expected:
                            raise ValueError(f"Missing or extra {coordinate}: {directory}")
                cw.writerow((BACKEND, str(directory), count, worst == 0, worst, first))
                failed += worst != 0
                checks.flush()
    (args.out / "metadata.json").write_text(json.dumps(dict(backend=BACKEND, native_scp_execution=False,
        method="ACK-index recurrence; no clock simulation, time shift or fitted amplitude",
        observed_source="independent Python packet-model CSVs",
        tick_reference="same gap profile; reservation sent at floor(slot) us",
        continuous_reference="unquantized gap and exact reservation departure",
        arithmetic="binary64 timing/real CA; exact integer byte CA, not interval certification",
        comparison_relative_tolerance=2e-12, comparison_absolute_tolerance=1e-6,
        observed_summary_sha256=sources,
        fixed_feedback_us=RTT_US, checked_runs=sum(map(len, cases.values())), failed_runs=failed,
        checker_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()), indent=2) + "\n", encoding="utf-8")
    print(f"{BACKEND}: {sum(map(len, cases.values()))} runs, {failed} failed")
    raise SystemExit(bool(failed))


if __name__ == "__main__":
    main()
