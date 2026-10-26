"""Join native observations to ACK-count references for fixed-g SS or CA runs."""
import csv
import gzip
import json
import math
from collections import defaultdict
from statistics import median

import numpy as np
from analyze_pacing_research import open_text, write_csv, load_packets, representative_gains
import matplotlib.pyplot as plt


def native_states(path, origin, end):
    previous = None
    with open_text(path) as source:
        for line in source:
            if not line.startswith('{'):
                continue
            row = json.loads(line)
            if row.get('type') != 'ss' or row.get('stream_fd') != 1 or not origin <= row['t'] <= end:
                continue
            if previous is not None and row['t'] != previous['t']:
                yield previous
            previous = row
    if previous is not None:
        yield previous


def save_figure(fig, path):
    fig.tight_layout()
    fig.savefig(path.with_suffix('.png'), dpi=180)
    fig.savefig(path.with_suffix('.pdf'))
    plt.close(fig)


def compare(root, out):
    out.mkdir(parents=True, exist_ok=True)
    settings = json.loads((root/'experiment.json').read_text(encoding='utf-8'))
    mode = settings['mode']
    if mode not in ('always_slow_start', 'always_congestion_avoidance'):
        raise ValueError('Only fixed-g SS or CA datasets belong in this comparison')
    slow_start = mode == 'always_slow_start'
    label = 'Slow start' if slow_start else 'Congestion avoidance'
    rule = 'MSS + acknowledged payload bytes' if slow_start else 'real-valued ACK recurrence'
    fields = ['case_id', 'mode', 'g_target', 'g_effective', 'gain_q16', 'repeat', 'elapsed_ms', 'rtt',
              'cwnd_bytes', 'flight_bytes', 'reserved_bytes', 'srtt_ms', 'ssthresh_bytes',
              'ack_count', 'ack_reference_bytes', 'ack_error_bytes', 'ack_relative_error',
              'sqrt_ack_reference_bytes', 'linear_1_MSS_per_RTT_reference_bytes',
              'all_payload_sent', 'cwnd_cap_reached']
    curves, summaries, fingerprints, wide = [], [], defaultdict(list), {}
    late_rows = []
    with gzip.open(out/'cwnd_every_ms.csv.gz', 'wt', newline='', compresslevel=1) as dense, \
            (out/'cwnd_RTT_boundaries.csv').open('w', newline='') as boundary, \
            (out/'L_fixed_RTT_all.csv').open('w', newline='') as spans:
        dense_writer = csv.DictWriter(dense, fieldnames=fields)
        boundary_writer = csv.DictWriter(boundary, fieldnames=fields)
        dense_writer.writeheader()
        boundary_writer.writeheader()
        span_writer = None
        for path in sorted((root/'runs').glob('*/config.json')):
            case = json.loads(path.read_text(encoding='utf-8'))
            if case['mode'] != mode or case['gain_policy'] != 'fixed_q16':
                raise ValueError('Mixed mode or dynamic-gain arm in fixed-g comparison')
            folder = path.parent
            summary = json.loads((folder/'summary.json').read_text(encoding='utf-8'))
            audit = json.loads((folder/'execution_audit.json').read_text(encoding='utf-8'))
            origin, end, mss = summary['first_data_tx_ms'], summary['data_acked_ms'], case['mss_bytes']
            g, effective = case['g'], case['g_effective']
            if effective != case['gain_q16']/65536:
                raise ValueError('Effective gain disagrees with the Q16 callback')
            curve = dict(g=g, ack=[], spans=[], late_spans=[])
            sample_step = max(1, summary['new_packets']//2000)
            with gzip.open(folder/'ack_updates.csv.gz', 'rt') as source:
                reader = csv.DictReader(source)
                ack = next(reader, None)
                reference, count, acknowledged, expected_elapsed = mss, 0, 0, 0
                for state in native_states(folder/case['state_log'], origin, end):
                    elapsed = state['t']-origin
                    if elapsed != expected_elapsed:
                        raise ValueError('Missing millisecond core observations')
                    expected_elapsed += 1
                    while ack is not None and float(ack['elapsed_ms']) <= elapsed:
                        count = int(ack['ack_index'])
                        acknowledged += int(ack['payload_bytes'])
                        reference = mss+acknowledged if slow_start else float(ack['paper_ack_reference_bytes'])
                        if case['repeat'] == 1 and (count % sample_step == 0 or count == summary['new_packets']):
                            observed = int(ack['observed_cwnd_bytes'])
                            curve['ack'].append((count, observed/mss, reference/mss,
                                                 (observed-reference)/reference*100))
                        ack = next(reader, None)
                    phase_ok = (state['cwnd'] < state['ssthresh'] if slow_start else
                                state['ssthresh'] == mss and state['cwnd'] >= mss)
                    if not phase_ok:
                        raise ValueError('Observed state is outside the configured congestion phase')
                    row = dict(case_id=case['case_id'], mode=mode, g_target=g, g_effective=effective,
                        gain_q16=case['gain_q16'], repeat=case['repeat'], elapsed_ms=elapsed,
                        rtt=elapsed/case['rtt_ms'], cwnd_bytes=state['cwnd'], flight_bytes=state['flight'],
                        reserved_bytes=state['reserved'], srtt_ms=state['srtt'], ssthresh_bytes=state['ssthresh'],
                        ack_count=count, ack_reference_bytes=reference, ack_error_bytes=state['cwnd']-reference,
                        ack_relative_error=(state['cwnd']-reference)/reference,
                        sqrt_ack_reference_bytes=None if slow_start else mss*math.sqrt(1+2*count),
                        linear_1_MSS_per_RTT_reference_bytes=None if slow_start else mss*(1+elapsed/case['rtt_ms']),
                        all_payload_sent=state['t']>=summary['last_data_tx_ms'],
                        cwnd_cap_reached=state['cwnd']>=case['cwnd_limit_bytes'])
                    dense_writer.writerow(row)
                    if elapsed % case['rtt_ms'] == 0:
                        boundary_writer.writerow(row)
                if not expected_elapsed - 1 <= end-origin < expected_elapsed:
                    raise ValueError('Incomplete millisecond state observations')
                while ack is not None:
                    if float(ack['elapsed_ms']) > end-origin:
                        raise ValueError('ACK is beyond the actual transfer end')
                    count = int(ack['ack_index'])
                    acknowledged += int(ack['payload_bytes'])
                    ack = next(reader, None)
                if count != summary['new_packets'] or acknowledged != summary['received_bytes']:
                    raise ValueError('Incomplete state/ACK observation join')
            eligible_late = []
            for row in load_packets(folder/case['packet_rtt_file'], case, summary):
                if row['scope'] != 'new':
                    continue
                if span_writer is None:
                    span_writer = csv.DictWriter(spans, fieldnames=list(row))
                    span_writer.writeheader()
                span_writer.writerow(row)
                if row['analysis_eligible'] and row['middle_late']:
                    eligible_late.append(row)
                if case['repeat'] == 1:
                    span = row['span_ms'] if row['analysis_eligible'] else None
                    wide.setdefault(row['rtt_index'], {})[f'g_{g:.17g}'] = span
                    curve['spans'].append((row['rtt_index'], span if span is not None else np.nan))
                    if row['middle_late']:
                        curve['late_spans'].append((row['rtt_index'], span if span is not None else np.nan))
            late = dict(case_id=case['case_id'], g_target=g, g_effective=effective,
                        gain_q16=case['gain_q16'], repeat=case['repeat'], eligible_bins=len(eligible_late),
                        first_rtt=min((r['rtt_index'] for r in eligible_late), default=None),
                        last_rtt=max((r['rtt_index'] for r in eligible_late), default=None))
            for name in ('span_ms', 'span_rtt', 'occupied_fraction', 'burst_fraction', 'hhi'):
                values = [r[name] for r in eligible_late]
                late.update({name+'_min': min(values) if values else None,
                             name+'_median': median(values) if values else None,
                             name+'_max': max(values) if values else None})
            late_rows.append(late)
            evidence = json.loads((folder/'artifact_integrity.json').read_text(encoding='utf-8'))
            fingerprints[g].append(tuple(evidence[k]['uncompressed_sha256'] for k in
                                         ('state_log', 'packet_rtt_file', 'network_events')))
            summaries.append(dict(g_target=g, g_effective=effective, gain_q16=case['gain_q16'],
                repeat=case['repeat'], mode=mode, ack_reference_rule=rule,
                data_duration_ms=end-origin, observed_max_cwnd_bytes=summary['max_cwnd'],
                mean_growth_MSS_per_RTT=(summary['max_cwnd']/mss-1)/((end-origin)/case['rtt_ms']),
                **{k: audit[k] for k in ('max_integer_update_error_bytes',
                    'last_full_packet_observed_cwnd_bytes', 'last_full_packet_paper_reference_bytes',
                    'last_full_packet_paper_relative_error', 'max_paper_square_identity_residual') if k in audit}))
            if case['repeat'] == 1:
                curves.append(curve)
    write_csv(out/'formula_summary.csv', summaries)
    gain_fields = [f'g_{entry["value"]:.17g}' for entry in settings['gains']]
    write_csv(out/'L_by_RTT_wide.csv', [dict(rtt_index=k, **row) for k,row in sorted(wide.items())],
              ['rtt_index']+gain_fields)
    write_csv(out/'L_middle_late_summary.csv', late_rows)
    checks = [dict(g=g, repeats=len(values), identical=len(set(values))==1) for g,values in sorted(fingerprints.items())]
    write_csv(out/'deterministic_repeat_check.csv', checks)
    if any(not row['identical'] for row in checks):
        raise ValueError('Deterministic repeats differ; inspect raw observations')
    curves.sort(key=lambda row: row['g'])
    selected = representative_gains([curve['g'] for curve in curves])
    colors = dict(zip([curve['g'] for curve in curves], plt.cm.turbo(np.linspace(.03,.97,len(curves)))))
    plt.rcParams.update({'font.size':12})
    fig, axes = plt.subplots(2, 1, figsize=(13, 10))
    for curve in curves:
        points = np.asarray(curve['ack'])
        if not len(points):
            continue
        axes[0].plot(points[:,0], points[:,1], color=colors[curve['g']], alpha=.55)
        axes[1].plot(points[:,0], points[:,3], color=colors[curve['g']], alpha=.55)
    points = np.asarray(curves[0]['ack'])
    axes[0].plot(points[:,0], points[:,2], '--', color='black', label=rule)
    if not slow_start:
        axes[0].plot(points[:,0], np.sqrt(1+2*points[:,0]), ':', color='gray', label='sqrt(1+2k) leading reference')
    axes[0].set(ylabel='Window / MSS', title=f'{label}: observed core versus ACK-count reference')
    axes[0].legend()
    axes[1].set(ylabel='(observed - reference) / reference (%)',
                title='Same ACK count; final short ACK uses actual acknowledged bytes')
    for ax in axes:
        ax.set_xlabel('Observed forward DATA ACK count')
        ax.grid(alpha=.25)
    save_figure(fig, out/'formula_comparison')
    fig, axes = plt.subplots(3, 1, figsize=(13, 13))
    for curve in curves:
        color, g = colors[curve['g']], curve['g']
        if curve['spans']:
            axes[0].plot(*zip(*curve['spans']), color=color, alpha=.65, label=f'{g:.6g}')
        if g in selected and curve['late_spans']:
            axes[1].plot(*zip(*curve['late_spans']), color=color, label=f'{g:.6g}')
    summary_rows = sorted((row for row in late_rows if row['repeat']==1 and row['eligible_bins']),
                          key=lambda row: row['g_target'])
    if summary_rows:
        gs = [row['g_target'] for row in summary_rows]
        axes[2].plot(gs, [row['span_ms_median'] for row in summary_rows], '.-', label='Middle/late-bin median')
        axes[2].fill_between(gs, [row['span_ms_min'] for row in summary_rows],
                             [row['span_ms_max'] for row in summary_rows], alpha=.2, label='Observed bin min-max')
    axes[0].set_title(f'{label}: all fixed gains; complete, nonfinal bins with at least two packets')
    axes[1].set_title('Representative gains: middle/late transfer interval')
    for ax in axes[:2]:
        ax.set_xlabel('Fixed RTT interval n: [nR, (n+1)R) since first actual DATA send')
        ax.legend(title='Target g', ncol=4, fontsize=9)
    axes[2].set(xlabel='Target gain', title='Per-run middle/late bins: 50%-90% of final-send bin index')
    axes[2].legend()
    for ax in axes:
        ax.set_ylabel('L = last minus first DATA send (ms)')
        ax.grid(alpha=.25)
    save_figure(fig, out/'L_all_g')
