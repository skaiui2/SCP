"""Compare new next-slot SCP observations, Python packet models and mathematical references.

This reads existing data only. A native root contains experiment.json and analysis/.
Its optional runs/*/config.json supplies detailed provenance; compact-only artifacts
require --metadata with phase/run_id/head_sha/workflow_conclusion. Optional raw roots
provide representative network_events.csv.gz files. Dependencies: numpy, pandas,
matplotlib. No historical native result paths are hard-coded.
"""
import argparse
import csv
import gzip
import hashlib
import json
import math
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from math_reference import next_slot_reference, paper_ss_group_span

BASELINE = 'e41ac8821bc3e0864a552cea89c86d075744ae9e'
RULE = 'max_now_next_slot_then_reserve_then_add_gap'
COLORS = dict(native='#b52335', python='#1f77b4', math='#d88511')


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1 << 20), b''):
            value.update(block)
    return value.hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False)+'\n', encoding='utf-8')


def write_csv(frame, path):
    frame.to_csv(path, index=False, float_format='%.17g')


def truth(series):
    return series.astype(str).str.lower().isin(('true', '1'))


def open_text(path):
    return gzip.open(path, 'rt', encoding='utf-8') if path.suffix == '.gz' else path.open(encoding='utf-8')


def inventory(root, phase, args, policy='fixed_q16'):
    if not list((root/'runs').glob('*/config.json')) and policy=='fixed_q16':
        return compact_inventory(root,phase,args)
    records = []
    for path in sorted((root/'runs').glob('*/config.json')):
        config = read_json(path)
        summary_path, audit_path = path.parent/'summary.json', path.parent/'execution_audit.json'
        summary, audit = read_json(summary_path), read_json(audit_path)
        expected_mode = 'always_slow_start' if phase == 'ss' else 'always_congestion_avoidance'
        conditions = dict(mode=expected_mode, gain_policy=policy, bytes=args.expected_bytes,
            mss_bytes=1430, rtt_ms=100, rate_mbps=0, loss_ppm=0,
            baseline_sha=args.baseline_sha, initial_cwnd_bytes=1430)
        if any(config.get(key) != value for key, value in conditions.items()):
            raise ValueError(f'Native experiment conditions differ: {path}')
        if (summary.get('engine') != 'scp_virtual' or summary.get('status') != 'complete'
                or not summary.get('checksum_ok') or summary.get('received_bytes') != args.expected_bytes
                or summary.get('feedback_min_ms') != 100 or summary.get('feedback_max_ms') != 100
                or summary.get('feedback_count') != summary.get('new_packets')
                or summary.get('retrans_packets') or summary.get('dropped')
                or summary.get('cwnd_cap_reached')
                or not all(audit.get(key) for key in ('core_window_per_ack_verified',
                    'receiver_data_processing_verified', 'receiver_ack_generation_verified',
                    'next_slot_rule_verified'))):
            raise ValueError(f'Native transfer or execution audit failed: {path.parent}')
        if policy == 'fixed_q16' and config['g_effective'] != config['gain_q16']/65536:
            raise ValueError(f'Q16 gain mismatch: {path}')
        records.append(dict(config=config, summary=summary, folder=path.parent,
            evidence=dict(config=str(path), config_sha256=digest(path), summary_sha256=digest(summary_path),
                audit_sha256=digest(audit_path), case_id=config['case_id'], commit=config['commit'],
                binary_sha256=config['binary_sha256'], source_identity_sha256=config['source_identity_sha256'])))
    if not records:
        raise ValueError(f'No native runs/config.json under {root}')
    if policy == 'fixed_q16':
        gains = sorted({entry['config']['g'] for entry in records})
        if len(gains) != args.expected_gains:
            raise ValueError(f'Expected {args.expected_gains} native gains, got {len(gains)}')
        groups = [[r for r in records if r['config']['g'] == gain] for gain in gains]
    else:
        groups = [records]
    if any(sorted(r['config']['repeat'] for r in group) != list(range(1, args.repeats+1)) for group in groups):
        raise ValueError(f'Missing or duplicate actual repeat configs: {root}')
    return records


def compact_inventory(root, phase, args):
    settings = read_json(root/'experiment.json')
    metadata = read_json(args.metadata).get(phase,{}) if args.metadata else {}
    if not metadata.get('head_sha') or not metadata.get('run_id') or metadata.get('workflow_conclusion') != 'success':
        raise ValueError('Compact artifacts require --metadata phase: {head_sha, run_id, workflow_conclusion: success}')
    conditions = dict(mode='always_slow_start' if phase=='ss' else 'always_congestion_avoidance',
        bytes=args.expected_bytes,mss_bytes=1430,rtt_ms=100,rate_mbps=0,loss_ppm=0,
        initial_cwnd_bytes=1430,baseline_sha=args.baseline_sha)
    if any(settings.get(key)!=value for key,value in conditions.items()):
        raise ValueError(f'Compact experiment conditions differ: {root}')
    summaries = pd.read_csv(root/'analysis/transfer_summary.csv')
    boundaries = pd.read_csv(root/'analysis/cwnd_RTT_boundaries.csv')
    spans = pd.read_csv(root/'analysis/L_fixed_RTT_all.csv')
    audit = read_json(root/'analysis/analysis_audit.json')
    repeats = pd.read_csv(root/'analysis/deterministic_repeat_check.csv')
    if (len(summaries)!=args.expected_gains*args.repeats or summaries['g'].nunique()!=args.expected_gains
            or audit.get('missing_cases') or audit.get('unfinished_cases')
            or audit.get('statuses')!={'complete':len(summaries)}
            or not truth(summaries['checksum_ok']).all()
            or not all(summaries['engine']=='scp_virtual')
            or not all(summaries['received_bytes']==args.expected_bytes)
            or not all(summaries['feedback_min_ms']==100) or not all(summaries['feedback_max_ms']==100)
            or not all(summaries['retrans_packets']==0) or truth(summaries['cwnd_cap_reached']).any()
            or not truth(repeats['identical']).all()):
        raise ValueError(f'Compact completeness/transfer/repeat checks failed: {root}')
    records = []
    for row in summaries.to_dict('records'):
        matching = boundaries[(boundaries['repeat']==row['repeat']) & np.isclose(boundaries['g_target'],row['g'],rtol=0,atol=1e-14)]
        cases = matching['case_id'].unique()
        if len(cases)!=1:
            raise ValueError('Compact boundary table does not identify exactly one case')
        case = cases[0]
        packet_bins = spans[spans['case_id']==case]
        final = packet_bins[truth(packet_bins['contains_final_send'])]
        if len(final)!=1:
            raise ValueError('Compact L table does not identify exactly one terminal bin')
        q16 = int(matching.iloc[0]['gain_q16'])
        config = dict(settings,case_id=case,gain_id=case.rsplit('-r',1)[0],g=row['g'],
            g_effective=q16/65536,gain_q16=q16,gain_policy='fixed_q16',repeat=int(row['repeat']),
            commit=metadata['head_sha'],binary_sha256=None,source_identity_sha256=None)
        summary = dict(row,new_packets=int(row['feedback_count']),
            last_data_tx_ms=int(final.iloc[0]['last_tx_ms']))
        records.append(dict(config=config,summary=summary,folder=root/'runs'/case,
            evidence=dict(case_id=case,commit=metadata['head_sha'],actions_run_id=metadata['run_id'],
                evidence_level='compact analysis artifacts; per-run binary/config/audit not present',
                transfer_summary_sha256=digest(root/'analysis/transfer_summary.csv'),
                experiment_sha256=digest(root/'experiment.json'))))
    for gain in summaries['g'].unique():
        if sorted(summaries[summaries['g']==gain]['repeat'])!=list(range(1,args.repeats+1)):
            raise ValueError('Compact repeats missing or duplicated')
    return records


def math_tables(reference, mss=1430, rtt_ms=100):
    sent, acked = np.asarray(reference['send_ms']), np.asarray(reference['ack_ms'])
    windows, generations = np.asarray(reference['cwnd_mss']), np.asarray(reference['generation'])
    boundaries = np.arange(int(acked[-1]//rtt_ms)+1)
    counts = np.searchsorted(acked, boundaries*rtt_ms, side='right')
    before = np.searchsorted(acked, boundaries*rtt_ms-1e-9, side='right')
    after = np.searchsorted(acked, boundaries*rtt_ms+1e-9, side='right')
    result = pd.DataFrame(dict(rtt=boundaries, math_cwnd_bytes=windows[counts]*mss,
        math_ACKs_within_1e_9_ms_of_boundary=after-before,
        math_cwnd_minus_1e_9_ms_bytes=windows[before]*mss,
        math_cwnd_plus_1e_9_ms_bytes=windows[after]*mss))
    bins = np.floor(sent/rtt_ms).astype(int)
    keys, start, sizes = np.unique(bins, return_index=True, return_counts=True)
    spans = pd.DataFrame(dict(rtt=keys, math_span_ms=sent[start+sizes-1]-sent[start],
        math_packets=sizes, math_final=keys == bins[-1]))
    result = result.merge(spans, on='rtt', how='outer')
    keys, start, sizes = np.unique(generations, return_index=True, return_counts=True)
    groups = pd.DataFrame(dict(generation=keys, math_span_ms=sent[start+sizes-1]-sent[start],
        math_packets=sizes, math_final=keys == generations[-1]))
    return result, groups


def load_python(batch, phase, manifest, records, gain, mode_root=None):
    index = next(i for i,g in enumerate(manifest['gains'], 1) if math.isclose(g, gain, rel_tol=0, abs_tol=1e-14))
    folder = (mode_root or batch/phase)/f'g{index:02d}'
    summary = pd.read_csv(folder/'summary.csv')
    if (len(summary) != len(records)
            or sorted(summary['repeat']) != sorted(r['config']['repeat'] for r in records)
            or not all(summary['script_sha256'] == manifest['model_sha256'])
            or not all(summary['requested_bytes'] == records[0]['config']['bytes'])
            or not all(summary['rtt_ms'] == 100) or not all(summary['tick_ms'] == .001)
            or not all(summary['reservation_rule'] == RULE)
            or not all(summary['mode'] == phase)
            or not np.allclose(summary['gain'], gain, rtol=0, atol=1e-14)):
        raise ValueError(f'Python source/conditions differ: {folder}')
    return folder, summary


def native_tables(root):
    boundary_path, span_path = root/'analysis/cwnd_RTT_boundaries.csv', root/'analysis/L_fixed_RTT_all.csv'
    boundary, spans = pd.read_csv(boundary_path), pd.read_csv(span_path)
    return boundary, spans, {str(path): digest(path) for path in (boundary_path, span_path)}


def combine(boundary, spans, record, python, mathematical):
    case = record['config']['case_id']
    native = boundary[boundary['case_id'] == case][['rtt','cwnd_bytes','ack_count','ack_reference_bytes']].rename(
        columns=dict(cwnd_bytes='native_cwnd_bytes', ack_count='native_ack_count',
            ack_reference_bytes='native_ack_reference_bytes'))
    span = spans[spans['case_id'] == case][['rtt_index','span_ms','packet_count','analysis_eligible','middle_late','contains_final_send']].rename(
        columns=dict(rtt_index='rtt', span_ms='native_span_ms', packet_count='native_packets',
            analysis_eligible='native_eligible', middle_late='native_middle_late', contains_final_send='native_final'))
    if native.empty or span.empty or native['rtt'].duplicated().any() or span['rtt'].duplicated().any():
        raise ValueError(f'Missing/duplicate native table observations: {case}')
    result = python.add_prefix('python_').rename(columns=dict(python_rtt='rtt'))
    result = result.merge(mathematical, on='rtt', how='outer').merge(native, on='rtt', how='outer').merge(span, on='rtt', how='outer')
    result = result.sort_values('rtt')
    for reference in ('python', 'math'):
        result[f'native_minus_{reference}_cwnd_bytes'] = result['native_cwnd_bytes']-result[f'{reference}_cwnd_bytes']
        result[f'native_minus_{reference}_cwnd_percent'] = 100*result[f'native_minus_{reference}_cwnd_bytes']/result[f'{reference}_cwnd_bytes']
    result['python_minus_math_cwnd_bytes'] = result['python_cwnd_bytes']-result['math_cwnd_bytes']
    result['native_minus_ACK_reference_bytes'] = result['native_cwnd_bytes']-result['native_ack_reference_bytes']
    result['native_minus_ACK_reference_percent'] = 100*result['native_minus_ACK_reference_bytes']/result['native_ack_reference_bytes']
    result['native_initial_state_pre_send'] = result['rtt'] == 0
    return result


def ss_group_paths(raw_root, records):
    expected = {entry['config']['case_id']:entry['config'] for entry in records
                if entry['config']['repeat']==1 and entry['config']['gain_policy']=='fixed_q16'}
    paths = {}
    for path in sorted(raw_root.rglob('network_events.csv.gz')):
        actual = read_json(path.parent/'config.json')
        config = expected.get(actual.get('case_id'))
        if config is None or any(actual.get(key)!=config.get(key)
                for key in ('bytes','mode','gain_policy','commit')):
            continue
        case = config['case_id']
        if case in paths:
            raise ValueError(f'Duplicate in-scope SS raw evidence for {case}: {paths[case]} and {path}')
        paths[case] = path
    return paths


def ss_groups(raw_root, records, python_groups, out):
    if raw_root is None:
        return []
    paths = ss_group_paths(raw_root,records)
    rows = []
    for entry in records:
        config, summary = entry['config'], entry['summary']
        if config['repeat'] != 1 or config['case_id'] not in paths:
            continue
        path = paths[config['case_id']]
        raw_config = path.parent/'config.json'
        raw_audit = path.parent/'execution_audit.json'
        if raw_config.exists():
            actual = read_json(raw_config)
            if any(actual.get(key)!=config.get(key) for key in ('case_id','bytes','g','repeat','baseline_sha','commit')):
                raise ValueError(f'Optional raw evidence does not belong to this new experiment: {path}')
        if raw_audit.exists():
            audit = read_json(raw_audit)
            if not all(audit.get(key) for key in ('core_window_per_ack_verified','receiver_data_processing_verified',
                    'receiver_ack_generation_verified','next_slot_rule_verified')):
                raise ValueError(f'Optional raw execution audit failed: {raw_audit}')
        groups, count, last_seq = {}, 0, None
        with open_text(path) as stream:
            for event in csv.DictReader(stream):
                if event['event'] != 'send' or int(event['direction']) != 0 or int(event['payload_bytes']) == 0:
                    continue
                seq = int(event['seq'])
                if last_seq is not None and seq <= last_seq:
                    raise ValueError(f'Unexpected retransmission/order in SS group extraction: {path}')
                last_seq = seq
                generation = (count+1).bit_length()-1
                t = int(event['t_ms'])-summary['first_data_tx_ms']
                group = groups.setdefault(generation, [t,t,0])
                group[1], group[2] = t, group[2]+1
                count += 1
        if count != summary['new_packets']:
            raise ValueError(f'Incomplete optional network event log: {path}')
        gain, raw_hash = config['g'], digest(path)
        for generation, (first,last,size) in sorted(groups.items()):
            complete = size == 2**generation
            py = python_groups[gain]
            py = py[py['generation'] == generation]
            observed_py = float(py.iloc[0]['span_ms']) if len(py) else np.nan
            formula = paper_ss_group_span(gain, 2**(generation-1)) if generation and complete else np.nan
            q16_formula = paper_ss_group_span(config['g_effective'],2**(generation-1)) if generation and complete else np.nan
            rows.append(dict(g=gain, g_effective=config['g_effective'], case_id=config['case_id'], generation=generation,
                packet_index_first=2**generation-1, packet_index_last=2**generation+size-2,
                packets=size, complete=complete, contains_final_packet=last == summary['last_data_tx_ms']-summary['first_data_tx_ms'],
                native_span_ms=last-first, python_span_ms=observed_py,
                ideal_finite_group_ms=formula, ideal_limit_ms=200*math.log(2)/gain,
                ideal_finite_group_q16_ms=q16_formula,ideal_limit_q16_ms=200*math.log(2)/config['g_effective'],
                finite_formula_conditions_met=complete and generation>0 and gain>=1,
                raw_path=str(path), raw_sha256=raw_hash))
    if rows:
        frame = pd.DataFrame(rows)
        write_csv(frame, out/'ss_packet_index_group_L.csv')
        gains = sorted(frame['g'].unique())
        fig,axes = plt.subplots(1,len(gains),figsize=(5*len(gains),4.5),squeeze=False)
        for ax,gain in zip(axes.flat,gains):
            group = frame[frame['g']==gain]
            complete = truth(group['complete']) & ~truth(group['contains_final_packet'])
            ax.plot(group['generation'],group['native_span_ms'].where(complete),'o-',label='SCP dyadic group',color=COLORS['native'])
            ax.plot(group['generation'],group['python_span_ms'].where(complete),'s-',label='Python genealogy group',color=COLORS['python'])
            applicable = complete & truth(group['finite_formula_conditions_met'])
            if applicable.any():
                ax.plot(group['generation'],group['ideal_finite_group_ms'].where(applicable),'--',label='Ideal finite formula: target g',color=COLORS['math'])
                ax.plot(group['generation'],group['ideal_finite_group_q16_ms'].where(applicable),'-.',label='Ideal finite formula: Q16 g',color='#6f4096')
                ax.axhline(200*math.log(2)/gain,linestyle=':',color=COLORS['math'],label='Ideal limit: target g')
                ax.axhline(float(group.iloc[0]['ideal_limit_q16_ms']),linestyle=':',color='#6f4096',label='Ideal limit: Q16 g')
            ax.scatter(group.loc[~complete,'generation'],group.loc[~complete,'native_span_ms'],marker='x',color=COLORS['native'],alpha=.5,label='Incomplete/terminal group')
            ax.axhline(100,linestyle=':',color='gray')
            ax.set(title=f'g={gain:.9g}',xlabel='Packet-index dyadic generation',ylabel='First-to-last group L (ms)')
            ax.grid(alpha=.25)
            ax.legend(fontsize=8)
        fig.suptitle('SS packet cohorts: different from fixed 100 ms time bins')
        save_figure(fig,out/'ss_packet_index_group_L')
    return rows


def summarize(table, record, py_row, reference, phase):
    config, observed = record['config'], record['summary']
    common_end = min(py_row['last_ack_ms'], observed['data_acked_ms']-observed['first_data_tx_ms'])/100
    lo, hi = math.ceil(.5*common_end), math.floor(.8*common_end)
    mask = table['rtt'].between(lo,hi)
    result = dict(phase=phase, g=config['g'], g_effective=config['g_effective'], gain_q16=config['gain_q16'],
        repeat=config['repeat'], native_bytes=config['bytes'], python_bytes=int(py_row['transferred_bytes']),
        native_packets=observed['new_packets'], python_packets=int(py_row['packets']),
        native_completion_RTT=(observed['data_acked_ms']-observed['first_data_tx_ms'])/100,
        python_completion_RTT=py_row['last_ack_ms']/100, math_completion_RTT=reference['ack_ms'][-1]/100,
        shared_middle_late_first_RTT=lo, shared_middle_late_last_RTT=hi,
        native_commit=config['commit'], native_binary_sha256=config['binary_sha256'])
    for layer in ('native','python','math'):
        eligible = mask & (table[f'{layer}_packets'] >= 2)
        final = f'{layer}_final' if layer != 'python' else 'python_last_data_bin'
        eligible &= ~truth(table[final])
        if layer == 'native':
            eligible &= truth(table['native_eligible'])
        values = table.loc[eligible, f'{layer}_span_ms']
        result.update({f'{layer}_middle_late_L_{name}_ms': float(getattr(values,name)()) for name in ('min','median','max')})
        result[f'{layer}_middle_late_valid_bins'] = len(values)
    for reference_name in ('python','math'):
        error = table[f'native_minus_{reference_name}_cwnd_percent'].dropna()
        result[f'max_abs_native_minus_{reference_name}_cwnd_percent'] = float(error.abs().max())
    return result


def save_figure(fig, path):
    fig.tight_layout(rect=(0,0,1,.94))
    fig.savefig(path.with_suffix('.png'), dpi=180)
    fig.savefig(path.with_suffix('.pdf'))
    plt.close(fig)


def figures(phase, curves, summary, out, size):
    targets = (1,2*math.log(2)-.05,2*math.log(2),2*math.log(2)+.05,2,8)
    selected = sorted({min(curves,key=lambda g:abs(g-target)) for target in targets})
    for metric in ('cwnd','L','error'):
        fig, axes = plt.subplots(3,2,figsize=(13,12))
        for ax, gain in zip(axes.flat, selected):
            table = curves[gain]
            if metric == 'error':
                for reference, label in (('python','Python 1 us model'),('math','Ideal ACK-index reference')):
                    ax.plot(table['rtt'],table[f'native_minus_{reference}_cwnd_percent'],label=f'SCP minus {label}')
                ax.axhline(0,color='gray',linewidth=.7)
                ax.set_ylabel('Difference / reference (%)')
            else:
                for layer,label in (('math','Ideal ACK-index reference'),('python','Python 1 us model'),('native','SCP core 1 ms')):
                    column = f'{layer}_cwnd_bytes' if metric == 'cwnd' else f'{layer}_span_ms'
                    values = table[column]/1430 if metric == 'cwnd' else table[column].copy()
                    if metric == 'L':
                        final = f'{layer}_final' if layer != 'python' else 'python_last_data_bin'
                        valid = (table[f'{layer}_packets']>=2) & ~truth(table[final])
                        if layer == 'native':
                            valid &= truth(table['native_eligible'])
                        values = values.where(valid)
                    ax.plot(table['rtt'],values,label=label,color=COLORS[layer],linestyle='--' if layer=='math' else '-',linewidth=1.5)
                if metric == 'cwnd':
                    ax.set_ylabel('cwnd / MSS')
                    if phase == 'ss':
                        ax.set_yscale('log',base=2)
                else:
                    ax.axhline(100,color='gray',linestyle=':')
                    ax.set(ylabel='Fixed-bin L (ms)',ylim=(-2,102))
            ax.set(title=f'g = {gain:.9g}',xlabel='RTTs since first actual DATA send')
            ax.grid(alpha=.25)
        for ax in list(axes.flat)[len(selected):]:
            ax.set_visible(False)
        handles,labels = axes.flat[0].get_legend_handles_labels()
        fig.legend(handles,labels,loc='upper center',ncol=3,bbox_to_anchor=(.5,.96),fontsize=9)
        fig.suptitle(f'{phase.upper()} {metric}: {size:,} requested bytes, fixed feedback RTT 100 ms',y=.995)
        save_figure(fig,out/f'{phase}_{metric}_three_layers')
    chosen = summary[summary['repeat']==1].sort_values('g')
    fig,axes = plt.subplots(2,1,figsize=(12,9))
    for layer,label in (('math','Ideal ACK-index reference'),('python','Python 1 us model'),('native','SCP core 1 ms')):
        axes[0].plot(chosen['g'],chosen[f'{layer}_completion_RTT'],'.-',label=label,color=COLORS[layer])
        axes[1].plot(chosen['g'],chosen[f'{layer}_middle_late_L_median_ms'],'.-',label=label,color=COLORS[layer])
    axes[0].set(ylabel='Completion / RTT',title=f'{phase.upper()}: all fixed gains')
    axes[1].set(xlabel='Target gain g',ylabel='Middle/late fixed-bin median L (ms)')
    for ax in axes:
        ax.grid(alpha=.25)
        ax.legend()
    save_figure(fig,out/f'{phase}_all_gain_trends')


def policy_tables(root, phase, args, out):
    records = inventory(root,phase,args,policy='native_aimd')
    rows, windows, spans = [], [], []
    for entry in records:
        config,summary = entry['config'],entry['summary']
        folder,origin = entry['folder'],summary['first_data_tx_ms']
        state_path,packet_path = folder/config['state_log'],folder/config['packet_rtt_file']
        if not state_path.exists() or not packet_path.exists():
            raise ValueError(f'Native policy arm needs compact RTT tables or its state/histogram logs: {folder}')
        previous, states = None, []
        with open_text(state_path) as stream:
            for line in stream:
                if not line.startswith('{'):
                    continue
                state = json.loads(line)
                if state.get('type') != 'ss' or state.get('stream_fd') != 1:
                    continue
                if previous is not None and state['t'] != previous['t']:
                    states.append(previous)
                previous = state
        if previous is not None:
            states.append(previous)
        for state in states:
            elapsed = state['t']-origin
            if 0 <= elapsed <= summary['data_acked_ms']-origin and elapsed%100 == 0:
                gain_q16 = 90852 if phase=='ss' else 65536+(1430*65536)//(state['cwnd']<<1)
                windows.append(dict(phase=phase,case_id=config['case_id'],repeat=config['repeat'],rtt=elapsed//100,
                    native_cwnd_bytes=state['cwnd'],candidate_gain_q16=gain_q16,
                    candidate_g_effective=gain_q16/65536,
                    formula='2ln2 Q16' if phase=='ss' else '1+MSS/(2*cwnd) Q16'))
        with open_text(packet_path) as stream:
            for raw in csv.DictReader(stream):
                if raw['scope'] != 'new':
                    continue
                count = int(raw['packet_count'])
                span = int(raw['last_tx_ms'])-int(raw['first_tx_ms']) if count>=2 else None
                index = int(raw['rtt_index'])
                spans.append(dict(phase=phase,case_id=config['case_id'],repeat=config['repeat'],rtt=index,
                    native_span_ms=span,packets=count,
                    contains_final_send=index==(summary['last_data_tx_ms']-origin)//100))
        rows.append(dict(phase=phase,case_id=config['case_id'],repeat=config['repeat'],
            gain_policy='native_aimd',scalar_g=None,requested_bytes=config['bytes'],
            completion_RTT=(summary['data_acked_ms']-origin)/100,
            comparison_status='native-only candidate; no matching dynamic-policy Python campaign',
            source_commit=config['commit'],binary_sha256=config['binary_sha256']))
    write_csv(pd.DataFrame(rows),out/f'{phase}_native_policy_summary.csv')
    table = pd.DataFrame(windows).merge(pd.DataFrame(spans),on=['phase','case_id','repeat','rtt'],how='outer')
    write_csv(table,out/f'{phase}_native_policy_by_RTT.csv')
    sample = table[table['repeat']==1]
    fig,axes = plt.subplots(3,1,figsize=(12,10),sharex=True)
    axes[0].plot(sample['rtt'],sample['native_cwnd_bytes']/1430)
    axes[0].set_ylabel('cwnd / MSS')
    axes[1].plot(sample['rtt'],sample['candidate_g_effective'])
    axes[1].set_ylabel('Q16 policy gain')
    axes[2].plot(sample['rtt'],sample['native_span_ms'].where(~truth(sample['contains_final_send'])))
    axes[2].set(xlabel='RTTs since first actual DATA send',ylabel='Fixed-bin L (ms)')
    fig.suptitle(f'{phase.upper()} native gain policy: native-only candidate, {args.expected_bytes:,} bytes')
    for ax in axes:
        ax.grid(alpha=.25)
    save_figure(fig,out/f'{phase}_native_policy')
    return [entry['evidence'] for entry in records]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python-batch',type=Path)
    for phase in ('ss','ca'):
        parser.add_argument(f'--native-{phase}',type=Path)
        parser.add_argument(f'--python-{phase}',type=Path,help='Existing one-phase model root containing g01/, etc.; manifest/source in its parent')
        parser.add_argument(f'--native-policy-{phase}',type=Path)
        parser.add_argument(f'--raw-{phase}',type=Path)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--metadata',type=Path,help='Optional source SHA / GitHub run IDs; copied verbatim into provenance')
    parser.add_argument('--expected-bytes',type=int,default=1<<30)
    parser.add_argument('--expected-gains',type=int,default=22)
    parser.add_argument('--repeats',type=int,default=3)
    parser.add_argument('--baseline-sha',default=BASELINE)
    args = parser.parse_args()
    if not args.native_ss and not args.native_ca:
        parser.error('Provide at least one --native-ss or --native-ca root')
    args.out.mkdir(parents=True,exist_ok=False)
    all_summary,all_tables,all_groups,provenance = [],[],[],{}
    for phase in ('ss','ca'):
        root = getattr(args,f'native_{phase}')
        if root is None:
            continue
        mode_root = getattr(args,f'python_{phase}')
        batch = mode_root.parent if mode_root else args.python_batch
        if batch is None:
            parser.error(f'Provide --python-{phase} or --python-batch')
        manifest_path = batch/'manifest.json'
        manifest = read_json(manifest_path)
        frozen = batch/'source/pacing.py'
        if (manifest.get('reservation_rule') != RULE or manifest.get('native_scp_execution') is not False
                or manifest.get('requested_bytes') != args.expected_bytes
                or digest(frozen) != manifest['model_sha256']):
            raise ValueError('Python batch source, size or reservation policy mismatch')
        records = inventory(root,phase,args)
        boundary,spans,table_hashes = native_tables(root)
        grouped = {gain:[r for r in records if r['config']['g']==gain] for gain in sorted({r['config']['g'] for r in records})}
        curves,python_groups = {},{}
        model_integrity = []
        for gain,entries in grouped.items():
            print(f'Compare existing {phase} g={gain:.9g}, {args.expected_bytes:,} requested bytes',flush=True)
            folder,python_summary = load_python(batch,phase,manifest,entries,gain,mode_root)
            reference = next_slot_reference(phase,gain,int(python_summary.iloc[0]['packets']))
            mathematical,mathematical_groups = math_tables(reference)
            fingerprints = []
            for entry in entries:
                repeat = entry['config']['repeat']
                run = folder/f'g01-r{repeat:02d}'
                python,gen = pd.read_csv(run/'rtt.csv'),pd.read_csv(run/'generations.csv')
                fingerprints.append((digest(run/'rtt.csv'),digest(run/'generations.csv')))
                if repeat==1:
                    python_groups[gain] = gen
                    mg = mathematical_groups.copy()
                    mg.insert(0,'g',gain)
                    mg.insert(0,'phase',phase)
                    all_groups.append(mg)
                table = combine(boundary,spans,entry,python,mathematical)
                table.insert(0,'repeat',repeat)
                table.insert(0,'g_effective',entry['config']['g_effective'])
                table.insert(0,'g',gain)
                table.insert(0,'phase',phase)
                row = python_summary[python_summary['repeat']==repeat].iloc[0]
                all_summary.append(summarize(table,entry,row,reference,phase))
                all_tables.append(table)
                if repeat==1:
                    curves[gain] = table
            identical = len(set(fingerprints))==1
            if not identical:
                raise ValueError(f'Python deterministic repeats differ: {phase} g={gain}')
            model_integrity.append(dict(g=gain,repeats=len(entries),identical_rtt_and_generation_tables=identical,
                observed_hash_pairs=fingerprints))
        phase_summary = pd.DataFrame([r for r in all_summary if r['phase']==phase])
        figures(phase,curves,phase_summary,args.out,args.expected_bytes)
        if phase=='ss':
            ss_groups(getattr(args,'raw_ss'),records,python_groups,args.out)
        provenance[phase] = dict(root=str(root),analysis_files_sha256=table_hashes,
            python_manifest=str(manifest_path),python_manifest_sha256=digest(manifest_path),
            python_frozen_source_sha256=digest(frozen),
            runs=[entry['evidence'] for entry in records],python_repeat_checks=model_integrity)
        policy_root = getattr(args,f'native_policy_{phase}')
        if policy_root is not None:
            provenance[phase]['native_policy'] = policy_tables(policy_root,phase,args,args.out)
    write_csv(pd.DataFrame(all_summary),args.out/'three_layer_summary.csv')
    write_csv(pd.concat(all_tables,ignore_index=True),args.out/'three_layer_by_RTT.csv')
    write_csv(pd.concat(all_groups,ignore_index=True),args.out/'ideal_ACK_index_generation_L.csv')
    write_json(args.out/'comparison_provenance.json',dict(
        baseline_sha=args.baseline_sha,requested_bytes=args.expected_bytes,
        metadata=read_json(args.metadata) if args.metadata else {},native=provenance,
        comparison_source_sha256=digest(Path(__file__)),
        mathematical_source_sha256=digest(Path(__file__).with_name('math_reference.py')),
        scope='Existing native SCP execution versus existing independent Python packet simulation and ideal ACK-index mathematical reference; no transfer is run by this script',
        mathematical_reference='Unquantized real-valued next-slot ACK-index recurrence. Numerical evaluation of this model is not a proof of a closed-form time law or SCP execution.',
        mathematical_boundary_precision='Double precision. Additional +/-1e-9 ms columns flag boundary-sensitive ACK jumps; these are numerical sensitivity brackets, not statistical error bars or fitted shifts.',
        differences=['Python 1 us fixed ticks, per-gap truncation and unquantized g; native 1 ms ticks, Q32.32 reservations with send-at-floor and Q16 g',
            'Python real-valued CA; native integer-byte CA with minimum 1 byte increase',
            'Python full-MSS rounding: 1 GiB request sends 1 GiB + 846 bytes; native exactly 1 GiB with final 584-byte packet',
            'Mathematical and Python reference assume full MSS; native final short ACK is not silently replaced',
            'Native CA dynamic-gain arm has no matching Python campaign and is reported native-only'],
        time_origin='First actual forward DATA send, independently observed in each layer; no curve-fitting time shift',
        state_phase='Native RTT0 state precedes initial send; later core states follow due ACK/timer processing and can precede application refill. Only cwnd, not all flight fields, is compared.',
        fixed_bin_L='Actual send last-first in [nR,(n+1)R); L<=R is a binning fact and cannot prove packet-cohort non-crossing',
        middle_late='50%-80% of shorter observed Python/native ACK completion interval; at least 2 DATA packets; exclude terminal bin and incomplete native bins',
        SS_group_L='Optional native packet-index dyadic groups j=2^n-1..2^(n+1)-2; finite ideal formula conditions are flagged; incomplete terminal group excluded from formula claims',
        CA_closed_form='No unproved closed-form CA time law or exact scalar optimum is asserted',
        repeats='Actual deterministic repeats; no fabricated random uncertainty bars'))
    print(json.dumps(dict(output=str(args.out),fixed_native_runs=len(all_summary)),ensure_ascii=False))


if __name__ == '__main__':
    main()
