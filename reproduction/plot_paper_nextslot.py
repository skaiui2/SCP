"""Draw compact paper figures from existing next-slot SCP observation tables.

No transfer or model simulation is run. Fixed-bin spans and SS packet cohorts
remain separate; deterministic repetitions are not statistical error bars.
"""
import argparse
import json
import math
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
import numpy as np
import pandas as pd

CRITICAL = 2 * math.log(2)
COLORS = ['#0072B2', '#D55E00', '#009E73', '#CC79A7', '#E69F00', '#332288']
STYLES = ['-', '--', '-.', ':', (0, (6, 2)), (0, (3, 1, 1, 1))]


def read(path):
    return pd.read_csv(path, float_precision='round_trip')


def truth(series):
    return series.astype(str).str.lower().isin(('true', '1'))


def gain_label(gain):
    if math.isclose(gain, CRITICAL, abs_tol=1e-12):
        return r'$2\ln 2$'
    if math.isclose(abs(gain - CRITICAL), .05, abs_tol=1e-12):
        sign = '+' if gain > CRITICAL else '-'
        return rf'$2\ln 2{sign}0.05$'
    return f'{gain:.4g}'


def selected(gains, phase):
    targets = [1, CRITICAL - .05, CRITICAL, CRITICAL + .05, 2, 8] if phase == 'ss' else [1, CRITICAL, 2, 4, 8]
    return sorted({min(gains, key=lambda value: abs(value - target)) for target in targets})


def save(fig, out, name, top=.89):
    fig.tight_layout(rect=(0, 0, 1, top))
    fig.savefig(out / f'{name}.pdf', metadata={'CreationDate': None, 'ModDate': None})
    fig.savefig(out / f'{name}.png', dpi=300)
    plt.close(fig)


def finish(axes):
    for ax in np.asarray(axes).flat:
        ax.grid(alpha=.2, linewidth=.5)
        ax.tick_params(direction='out')


def legend(fig, ax, title='Target gain g', ncol=6):
    fig.legend(*ax.get_legend_handles_labels(), title=title, loc='upper center',
               ncol=ncol, frameon=False, columnspacing=1.1, handlelength=2.6)


def native_data(root, phase):
    settings = json.loads((root / 'experiment.json').read_text(encoding='utf-8'))
    expected = dict(mode='always_slow_start' if phase == 'ss' else 'always_congestion_avoidance',
                    rtt_ms=100, loss_ppm=0, rate_mbps=0, bytes=1 << 30,
                    mss_bytes=1430, initial_cwnd_bytes=1430)
    if any(settings.get(key) != value for key, value in expected.items()):
        raise ValueError('Expected the 1 GiB, one-MSS, fixed-feedback 100 ms, zero-loss campaign')
    folder = root / 'analysis'
    transfers = read(folder / 'transfer_summary.csv')
    repeats = read(folder / 'deterministic_repeat_check.csv')
    gains = [float(g['value']) for g in settings['gains']]
    if (len(gains) != 22 or settings['repeats'] != 3 or len(transfers) != 66
            or not truth(repeats['identical']).all()
            or not all(transfers['engine'] == 'scp_virtual')
            or not truth(transfers['checksum_ok']).all()
            or not all(transfers['received_bytes'] == 1 << 30)
            or not all(transfers['feedback_min_ms'] == 100)
            or not all(transfers['feedback_max_ms'] == 100)
            or not all(transfers['retrans_packets'] == 0)
            or truth(transfers['cwnd_cap_reached']).any()):
        raise ValueError('Native campaign completeness, feedback or repeat check failed')
    frames = [read(folder / name) for name in ('cwnd_RTT_boundaries.csv', 'L_fixed_RTT_all.csv',
              'L_middle_late_summary.csv', 'formula_summary.csv')]
    return settings, sorted(gains), [frame[frame['repeat'] == 1].copy() for frame in frames]


def curve(frame, gain, key='g_target', time='rtt'):
    return frame[np.isclose(frame[key], gain, atol=1e-13, rtol=0)].sort_values(time)


def span_curve(frame, gain):
    data = curve(frame, gain, 'g', 'rtt_index').copy()
    valid = truth(data['analysis_eligible']) & ~truth(data['contains_final_send']) & (data['packet_count'] >= 2)
    data['span_ms'] = data['span_ms'].where(valid)
    return data


def plot_ss_window(boundary, gains, chosen, mss, out):
    fig, axes = plt.subplots(1, 2, figsize=(7, 2.9))
    for ax in axes:
        for gain in gains:
            data = curve(boundary, gain)
            ax.plot(data['rtt'], data['cwnd_bytes'] / mss, color='.82', linewidth=.5, zorder=1)
        for gain, color, style in zip(chosen, COLORS, STYLES):
            data = curve(boundary, gain)
            ax.plot(data['rtt'], data['cwnd_bytes'] / mss, color=color, linestyle=style,
                    linewidth=1.15, label=gain_label(gain), zorder=2)
        ax.set(xlabel='RTTs since first DATA send', ylabel='cwnd / MSS', yscale='log')
        ax.set_yscale('log', base=2)
    axes[0].set(title='(a) First eight RTTs', xlim=(0, 8))
    axes[0].set_ylim(.9, 1.15 * boundary.loc[boundary['rtt'] <= 8, 'cwnd_bytes'].max() / mss)
    axes[1].set(title='(b) Full observed growth')
    finish(axes)
    legend(fig, axes[0])
    save(fig, out, 'cwnd-rtt')


def plot_spans(spans, late, chosen, phase, out):
    fig, axes = plt.subplots(1, 2, figsize=(7, 2.9))
    for gain, color, style in zip(chosen, COLORS, STYLES):
        data = span_curve(spans, gain)
        axes[0].plot(data['rtt_index'], data['span_ms'], color=color, linestyle=style,
                     linewidth=1.05, label=gain_label(gain))
    late = late.sort_values('g_target')
    axes[1].fill_between(late['g_target'], late['span_ms_min'], late['span_ms_max'],
                         color='#0072B2', alpha=.17, label='Within-run min–max')
    axes[1].plot(late['g_target'], late['span_ms_median'], 'o-', color='#0072B2',
                 markersize=2.8, linewidth=1, label='Within-run median')
    axes[1].axvline(CRITICAL, color='.55', linestyle=':', linewidth=.8)
    axes[1].legend(loc='lower right', fontsize=8)
    axes[0].set(title='(a) Fixed-bin first-to-last span', xlabel='Fixed RTT interval n', ylabel=r'$L_n$ (ms)')
    axes[1].set(title='(b) Middle/late complete intervals', xlabel='Target gain g', ylabel=r'$L_n$ (ms)')
    if phase == 'ss':
        axes[0].set_xlim(0, min(35, spans['rtt_index'].max() + 1))
    for ax in axes:
        ax.axhline(100, color='.5', linestyle=':', linewidth=.8)
        ax.set_ylim(-2, 103)
    finish(axes)
    legend(fig, axes[0])
    save(fig, out, 'span-rtt')


def plot_ca(boundary, spans, formula, chosen, mss, policy, out):
    fig, axes = plt.subplots(2, 2, figsize=(7, 5.2))
    baseline = curve(boundary, min(boundary['g_target'].unique(), key=lambda g: abs(g-1)))
    for gain, color, style in zip(chosen, COLORS, STYLES):
        data = curve(boundary, gain)
        axes[0, 0].plot(data['rtt'], data['cwnd_bytes'] / mss, color=color,
                        linestyle=style, linewidth=1.1, label=gain_label(gain))
        common = data.merge(baseline[['rtt', 'cwnd_bytes']], on='rtt', suffixes=('', '_g1'))
        axes[0, 1].plot(common['rtt'], (common['cwnd_bytes'] - common['cwnd_bytes_g1']) / mss,
                        color=color, linestyle=style, linewidth=1)
        packet = span_curve(spans, gain)
        axes[1, 1].plot(packet['rtt_index'], packet['span_ms'], color=color,
                        linestyle=style, linewidth=.9)
    critical = curve(boundary, min(chosen, key=lambda g: abs(g-CRITICAL)))
    critical = critical[critical['ack_count'] <= (1 << 30) // mss]
    axes[1, 0].plot(critical['ack_count'], critical['ack_relative_error'] * 100,
                    color=COLORS[1], linewidth=1.2)
    full = curve(formula, min(chosen, key=lambda g: abs(g-CRITICAL)), time='repeat')
    if len(full) and pd.notna(full.iloc[0]['last_full_packet_paper_relative_error']):
        axes[1, 0].plot([(1 << 30) // mss], [100 * full.iloc[0]['last_full_packet_paper_relative_error']],
                        'o', color=COLORS[1], markersize=3)
    axes[1, 0].axhline(0, color='.5', linewidth=.7)
    axes[1, 0].ticklabel_format(axis='x', style='sci', scilimits=(0, 0), useMathText=True)
    if policy is not None:
        data = policy[policy['repeat'] == 1].sort_values('rtt')
        valid = ~truth(data['contains_final_send']) & (data['packets'] >= 2)
        axes[1, 1].plot(data['rtt'], data['native_span_ms'].where(valid), color='black',
                        linewidth=1.6, label=r'Native $1+M/(2\,cwnd)$')
    axes[0, 0].set(title='(a) Observed window growth', xlabel='Elapsed RTTs', ylabel='cwnd / MSS')
    axes[0, 1].set(title='(b) Difference from g = 1', xlabel='Elapsed RTTs', ylabel='Window difference / MSS')
    axes[1, 0].set(title='(c) Core minus real-valued ACK reference\n' + r'$g=2\ln2$',
                    xlabel='Full-size DATA ACK count', ylabel='Difference / reference (%)')
    axes[1, 1].set(title='(d) Fixed-bin first-to-last span', xlabel='Fixed RTT interval n',
                    ylabel=r'$L_n$ (ms)', ylim=(-2, 103))
    axes[1, 1].axhline(100, color='.5', linestyle=':', linewidth=.7)
    if policy is not None:
        axes[1, 1].legend(fontsize=8, loc='lower right')
    finish(axes)
    legend(fig, axes[0, 0], ncol=5)
    save(fig, out, 'ca-growth', top=.92)


def plot_groups(groups, out):
    groups = groups[truth(groups['complete']) & ~truth(groups['contains_final_packet'])]
    available = sorted(groups['g'].unique())
    gains = sorted({min(available, key=lambda g: abs(g-target)) for target in [1, CRITICAL, 8]})
    fig, ax = plt.subplots(figsize=(7, 2.8))
    for gain, color in zip(gains, COLORS):
        data = curve(groups, gain, 'g', 'generation')
        ax.plot(data['generation'], data['native_span_ms'], color=color, linewidth=1.2,
                marker='o', markersize=2.5, label=f'g={gain_label(gain)}')
        ax.plot(data['generation'], data['python_span_ms'], color=color,
                linestyle='--', linewidth=1.1)
        applicable = truth(data['finite_formula_conditions_met'])
        if applicable.any():
            ax.plot(data['generation'], data['ideal_finite_group_q16_ms'].where(applicable),
                    color=color, linestyle=':', linewidth=.8)
            ax.axhline(data['ideal_limit_q16_ms'].iloc[0], color=color, linestyle=':', linewidth=.6)
    ax.axhline(100, color='.5', linestyle='--', linewidth=.7)
    ax.set(xlabel='Packet-index dyadic generation', ylabel='Cohort first-to-last L (ms)',
           title='Complete SS packet cohorts; dotted: ideal formula / limit at native Q16 gain')
    ax.set_xticks(np.arange(0, groups['generation'].max() + 1, 2))
    finish([ax])
    legend(fig, ax, title='Packet-index cohorts (different from fixed-time bins)', ncol=3)
    handles = [Line2D([0], [0], color='black', marker='o', markersize=2.5, label='SCP core (1 ms)'),
               Line2D([0], [0], color='black', linestyle='--', label='Python packet model (1 us)'),
               Line2D([0], [0], color='black', linestyle=':', label='Q16 ideal finite formula / limit')]
    ax.legend(handles=handles, loc='center right', fontsize=8)
    save(fig, out, 'ss-cohort-L', top=.83)


def plot_layers(layers, phase, out):
    data = layers[(layers['phase'] == phase) & (layers['repeat'] == 1)]
    gains = sorted(data['g'].unique())
    chosen = sorted({min(gains, key=lambda g: abs(g-target)) for target in [1, CRITICAL, 8]})
    fig, axes = plt.subplots(2, len(chosen), figsize=(7, 4.1), squeeze=False)
    for column, gain in enumerate(chosen):
        row = curve(data, gain, 'g')
        for layer, color, style, label in [('math', '#332288', ':', 'Ideal ACK-index reference'),
                ('python', '#0072B2', '--', 'Python packet model (1 us)'),
                ('native', '#D55E00', '-', 'SCP core (1 ms)')]:
            axes[0, column].plot(row['rtt'], row[f'{layer}_cwnd_bytes'] / 1430,
                    color=color, linestyle=style, linewidth=1.05, label=label)
        for layer, color, style, label in [('math', '#332288', ':', 'SCP minus ideal'),
                ('python', '#0072B2', '--', 'SCP minus Python')]:
            axes[1, column].plot(row['rtt'], row[f'native_minus_{layer}_cwnd_percent'],
                    color=color, linestyle=style, linewidth=1.05, label=label)
        axes[0, column].set(title=f'g = {gain_label(gain)}', xlabel='Elapsed RTTs', ylabel='cwnd / MSS')
        axes[1, column].set(xlabel='Elapsed RTTs', ylabel='Difference / reference (%)')
        axes[1, column].axhline(0, color='.5', linewidth=.6)
        if phase == 'ss':
            axes[0, column].set_yscale('log', base=2)
    axes[1, 0].legend(fontsize=8, loc='best')
    finish(axes)
    legend(fig, axes[0, 0], title='Three distinct execution objects', ncol=3)
    save(fig, out, f'{phase}-three-layers', top=.88)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', choices=('ss', 'ca'), required=True)
    parser.add_argument('--data', type=Path, required=True, help='Native compact root with experiment.json and analysis/')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--layers', type=Path, help='Optional three_layer_by_RTT.csv')
    parser.add_argument('--policy', type=Path, help='Optional ca_native_policy_by_RTT.csv')
    parser.add_argument('--ss-groups', type=Path, help='Optional ss_packet_index_group_L.csv')
    args = parser.parse_args()
    settings, gains, (boundary, spans, late, formula) = native_data(args.data, args.phase)
    chosen = selected(gains, args.phase)
    plt.rcParams.update({'font.size': 8, 'axes.titlesize': 8, 'legend.fontsize': 8,
                         'axes.labelsize': 8, 'pdf.fonttype': 42, 'ps.fonttype': 42})
    args.out.mkdir(parents=True, exist_ok=True)
    if args.phase == 'ss':
        plot_ss_window(boundary, gains, chosen, settings['mss_bytes'], args.out)
    else:
        plot_ca(boundary, spans, formula, chosen, settings['mss_bytes'],
                read(args.policy) if args.policy else None, args.out)
    plot_spans(spans, late, chosen, args.phase, args.out)
    if args.layers:
        plot_layers(read(args.layers), args.phase, args.out)
    if args.ss_groups:
        if args.phase != 'ss':
            parser.error('--ss-groups applies only to --phase ss')
        plot_groups(read(args.ss_groups), args.out)
    table = late[late['g_target'].isin(chosen)].merge(formula,
                on=['g_target', 'g_effective', 'gain_q16', 'repeat'], how='inner')
    table.to_csv(args.out / f'{args.phase}_selected_results.csv', index=False, float_format='%.17g')
    print(json.dumps(dict(phase=args.phase, selected_g=chosen, output=str(args.out),
          repeat=1, repeats_verified_identical=True, L='[nR,(n+1)R), >=2 packets, terminal bin excluded')))


if __name__ == '__main__':
    main()
