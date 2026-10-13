"""Independent event simulation and closed-form checks for the pacing paper."""
import argparse
from array import array
import csv
import gzip
import hashlib
import json
import math
from pathlib import Path
import subprocess

import numpy as np

HERE = Path(__file__).resolve().parent
CRITICAL = 2 * math.log(2)


def write_csv(path, records):
    records = iter(records)
    first = next(records, None)
    if first is None:
        return
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'wt', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(first))
        writer.writeheader()
        writer.writerow(first)
        writer.writerows(records)


def digest(path, uncompressed=False):
    h = hashlib.sha256()
    opener = gzip.open if uncompressed else open
    with opener(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def simulate(g, horizon):
    """Run ACK/send events with separate flight, reservations, window and tail."""
    if not math.isfinite(g) or g < 1 or horizon <= 0:
        raise ValueError('This paper model requires g >= 1 and a positive horizon')
    reserved = array('d', [0.0])
    sent = array('d')
    windows = array('Q')
    ack_index = send_index = flight = 0
    queued = window = 1
    tail = 1 / g
    while True:
        ack_time = sent[ack_index] + 1 if ack_index < len(sent) else math.inf
        send_time = reserved[send_index] if send_index < len(reserved) else math.inf
        if min(ack_time, send_time) > horizon:
            break
        if ack_time <= send_time:
            ack_index += 1
            flight -= 1
            window += 1
            spacing = 1 / (g * window)
            first = max(ack_time, tail)
            reserved.extend([first, first + spacing])
            tail = first + 2 * spacing
            queued += 2
        else:
            sent.append(send_time)
            windows.append(window)
            send_index += 1
            flight += 1
            queued -= 1
        if flight < 0 or queued < 0 or flight > window or flight + queued != window:
            raise AssertionError('Flight/reservation credit invariant violated')
    return np.asarray(sent), np.asarray(windows), window


def closed_form(g, count):
    """Generate timestamps from the paper's harmonic block formula, no events."""
    batches = (count + 1) // 2
    k = np.arange(1, batches + 1, dtype=np.int64)
    harmonic = np.concatenate(([0.0], np.cumsum(1 / np.arange(1, 2 * batches + 1, dtype=float))))
    m = np.floor(np.log2(k)).astype(int)
    q = np.left_shift(1, m)
    starts = [1.0]
    for group in range(int(m.max())):
        power = 2**group
        starts.append(starts[-1] + max(1.0, 2 / g * (harmonic[2 * power] - harmonic[power])))
    b = np.asarray(starts)[m] + 2 / g * (harmonic[k] - harmonic[q])
    sends = np.empty(2 * batches + 1)
    sends[0] = 0
    sends[1::2] = b
    sends[2::2] = b + 1 / (g * (k + 1))
    return sends[:count], harmonic


def run_case(folder, gain, repeat, settings):
    folder.mkdir(parents=True, exist_ok=True)
    g = float(gain['value'])
    rtt, payload, horizon = settings['rtt_ms'], settings['packet_payload_bytes'], settings['duration_rtts']
    times, windows, final_window = simulate(g, horizon)
    predicted, harmonic = closed_form(g, len(times))
    tx_error_ms = float(np.max(np.abs(times - predicted)) * rtt)
    if tx_error_ms > 1e-7:
        raise AssertionError(f'Event trace disagrees with closed form: {tx_error_ms} ms')
    config = dict(settings, gain_id=gain['id'], g=g, g_expression=gain['expression'],
                  repeat=repeat, source_commit=subprocess.check_output(
                      ['git','rev-parse','HEAD'], cwd=HERE, text=True).strip())
    config.pop('gains', None)
    paper = HERE.parents[1] / 'papers/pacing/Pacing_and_Congestion_Window_Growth.tex'
    config['paper_sha256'] = digest(paper)
    (folder / 'config.json').write_text(json.dumps(config, indent=2), encoding='utf-8')
    write_csv(folder / 'packet_events.csv.gz', (
        dict(packet_index=i, send_ms=float(t * rtt), scheduled_ack_ms=float((t + 1) * rtt),
             cwnd_at_send_bytes=int(w) * payload)
        for i, (t, w) in enumerate(zip(times, windows))))
    grid = np.arange(horizon * rtt // settings['sample_interval_ms'] + 1) * settings['sample_interval_ms']
    # Boundary convention: ACKs at the sample time are already processed.
    measured_w = 1 + np.searchsorted(times + 1, grid / rtt, side='right')
    predicted_w = 1 + np.searchsorted(predicted + 1, grid / rtt, side='right')
    if measured_w[-1] != final_window:
        raise AssertionError('Event window disagrees with ACK count')
    errors = (measured_w - predicted_w) * payload
    max_window_error = int(np.max(np.abs(errors)))
    write_csv(folder / 'cwnd_formula_comparison.csv', (
        dict(g=g, repeat=repeat, elapsed_ms=int(t), time_rtts=float(t / rtt),
             measured_cwnd_bytes=int(w) * payload, predicted_cwnd_bytes=int(p) * payload,
             signed_error_bytes=int(e), absolute_error_bytes=abs(int(e)),
             relative_error=float(e / (p * payload)))
        for t, w, p, e in zip(grid, measured_w, predicted_w, errors)))
    boundaries = np.arange(horizon + 1)
    boundary_w = 1 + np.searchsorted(times + 1, boundaries, side='right')
    if g >= CRITICAL and not np.array_equal(boundary_w[1:], 1 + 2**(boundaries[1:] - 1)):
        raise AssertionError('High-g integer-boundary formula failed')
    write_csv(folder / 'cwnd_boundaries.csv', (
        dict(g=g, repeat=repeat, rtt_index=int(n), elapsed_ms=int(n * rtt),
             cwnd_bytes=int(w) * payload,
             high_gain_boundary_prediction_bytes=(1 + 2**(int(n) - 1)) * payload if n and g >= CRITICAL else None)
        for n, w in zip(boundaries, boundary_w)))
    left = np.searchsorted(times, np.arange(horizon), side='left')
    right = np.searchsorted(times, np.arange(1, horizon + 1), side='left')
    write_csv(folder / 'L_by_rtt.csv', (
        dict(g=g, repeat=repeat, rtt_index=k, packet_count=int(b-a),
             first_send_ms=float(times[a] * rtt) if b > a else None,
             last_send_ms=float(times[b-1] * rtt) if b > a else None,
             L_ms=float((times[b-1] - times[a]) * rtt) if b-a >= 2 else None,
             L_limit_ms=2 * math.log(2) / g * rtt if g >= CRITICAL else rtt)
        for k, (a, b) in enumerate(zip(left, right))))
    spans = []
    for m in range(horizon):
        q = 2**m
        first, last, following = 2*q-1, 4*q-2, 4*q-1
        if last >= len(times):
            break
        d = harmonic[2*q] - harmonic[q]
        observed = (times[last] - times[first]) * rtt
        expected = (2/g*d - 1/(2*g*q)) * rtt
        spans.append(dict(g=g, repeat=repeat, group_m=m, packets_in_group=2*q,
            first_packet_index=first,last_packet_index=last,first_send_ms=float(times[first]*rtt),
            last_send_ms=float(times[last]*rtt),measured_L_ms=float(observed),predicted_L_ms=float(expected),
            signed_L_error_ms=float(observed-expected), predicted_extra_wait_ms=max(0,1-2/g*d)*rtt,
            measured_last_to_next_first_ms=float((times[following]-times[last])*rtt) if following < len(times) else None,
            predicted_last_to_next_first_ms=(max(0,1-2/g*d)+1/(2*g*q))*rtt))
    write_csv(folder / 'group_spans_formula_comparison.csv', spans)
    summary = dict(g=g, gain_id=gain['id'], repeat=repeat, status='complete',
        sent_packets=len(times), final_cwnd_bytes=final_window*payload,
        max_send_timestamp_error_ms=tx_error_ms, max_cwnd_error_bytes=max_window_error,
        max_group_L_error_ms=max(abs(s['signed_L_error_ms']) for s in spans),
        critical_boundary_checked=g >= CRITICAL, duration_ms=horizon*rtt,
        packet_trace_sha256=digest(folder / 'packet_events.csv.gz'),
        packet_trace_uncompressed_sha256=digest(folder / 'packet_events.csv.gz', True))
    (folder / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary), flush=True)
    return summary


def merge(root, out, settings):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import pandas as pd
    out.mkdir(parents=True, exist_ok=True)
    folders = sorted(root.glob('**/runs/*/summary.json'))
    summaries = [json.loads(p.read_text(encoding='utf-8')) for p in folders]
    expected = {(g['id'], r) for g in settings['gains'] for r in range(1, settings['repeats']+1)}
    keys = [(s['gain_id'], s['repeat']) for s in summaries]
    if set(keys) != expected or len(keys) != len(expected):
        raise ValueError('Missing or duplicate model cases')
    traces, spans, groups, bounds = [], [], [], []
    commits, paper_hashes = set(), set()
    gain_map = {g['id']: g for g in settings['gains']}
    for p in folders:
        config = json.loads((p.parent / 'config.json').read_text(encoding='utf-8'))
        commits.add(config['source_commit'])
        paper_hashes.add(config['paper_sha256'])
        target = gain_map[config['gain_id']]
        if config['g'] != target['value'] or config['g_expression'] != target['expression']:
            raise ValueError('Incorrect gain value or expression')
        result = json.loads(p.read_text(encoding='utf-8'))
        if result['packet_trace_sha256'] != digest(p.parent / 'packet_events.csv.gz'):
            raise ValueError('Packet trace checksum mismatch')
        for field in ('rtt_ms','duration_rtts','sample_interval_ms','packet_payload_bytes','model'):
            if config[field] != settings[field]:
                raise ValueError(f'Mixed model setting: {field}')
        traces.append(pd.read_csv(p.parent / 'cwnd_formula_comparison.csv'))
        spans.append(pd.read_csv(p.parent / 'L_by_rtt.csv'))
        groups.append(pd.read_csv(p.parent / 'group_spans_formula_comparison.csv'))
        bounds.append(pd.read_csv(p.parent / 'cwnd_boundaries.csv'))
    if len(commits) != 1 or len(paper_hashes) != 1:
        raise ValueError('Mixed source commits or paper versions')
    for gain in settings['gains']:
        hashes={s['packet_trace_uncompressed_sha256'] for s in summaries if s['gain_id']==gain['id']}
        if len(hashes)!=1:
            raise ValueError('Deterministic repetitions disagree')
    for name, parts in [('cwnd_formula_comparison_all_g',traces),('L_by_rtt_all_g',spans),
                        ('group_spans_formula_comparison_all_g',groups),('cwnd_boundaries_all_g',bounds)]:
        pd.concat(parts,ignore_index=True).sort_values(['g','repeat']).to_csv(out / (name+'.csv'),index=False)
    pd.DataFrame(summaries).sort_values(['g','repeat']).to_csv(out / 'case_summary.csv',index=False)
    curve = pd.concat(traces,ignore_index=True)
    spread = pd.concat(spans,ignore_index=True)
    boundary = pd.concat(bounds,ignore_index=True)
    for source,value,name in [(boundary,'cwnd_bytes','cwnd_by_RTT_wide'),(spread,'L_ms','L_by_RTT_wide')]:
        table = source[source['repeat']==1].pivot(index='rtt_index',columns='g',values=value)
        table.to_csv(out / (name+'.csv'))
    plt.rcParams.update({'font.size':14,'axes.spines.top':False,'axes.spines.right':False,'pdf.fonttype':42})
    palette = plt.get_cmap('plasma')
    gain_values = sorted(curve.g.unique())
    for value,ylabel,frame,name in [('measured_cwnd_bytes','cwnd (packets, log2 scale)',curve,'cwnd_all_g'),
                                   ('L_ms','L = last send - first send (ms)',spread,'L_all_g')]:
        fig,ax = plt.subplots(figsize=(14,7),layout='constrained')
        for j,g in enumerate(gain_values):
            rows=frame[(frame.g==g)&(frame['repeat']==1)]
            x=rows.elapsed_ms/settings['rtt_ms'] if 'elapsed_ms' in rows else rows.rtt_index
            y=rows[value]/settings['packet_payload_bytes'] if value=='measured_cwnd_bytes' else rows[value]
            ax.plot(x,y,color=palette(j/max(1,len(gain_values)-1)),lw=1.4,
                    label='g=2 ln 2' if g==CRITICAL else f'g={g:.4g}')
        ax.set(xlabel='Time / fixed RTT (100 ms)' if value=='measured_cwnd_bytes' else 'Fixed RTT bin k',ylabel=ylabel)
        if value=='measured_cwnd_bytes': ax.set_yscale('log',base=2)
        ax.grid(alpha=.2)
        ax.legend(ncol=2,fontsize=10,bbox_to_anchor=(1.01,1),loc='upper left')
        for ext in ('png','pdf','svg'): fig.savefig(out/(name+'.'+ext),dpi=220)
        plt.close(fig)
    critical = curve[(curve.g==CRITICAL)&(curve['repeat']==1)]
    fig,ax=plt.subplots(figsize=(12,6),layout='constrained')
    ax.plot(critical.time_rtts,critical.predicted_cwnd_bytes/settings['packet_payload_bytes'],color='black',lw=3,label='Paper exact formula')
    ax.plot(critical.time_rtts,critical.measured_cwnd_bytes/settings['packet_payload_bytes'],color='#0072b2',ls='--',lw=1.8,label='Independent event simulation')
    ax.set(xlabel='Time / RTT',ylabel='cwnd (packets, log2 scale)',title='g = 2 ln 2: event simulation versus exact formula')
    ax.set_yscale('log',base=2);ax.legend();ax.grid(alpha=.2)
    for ext in ('png','pdf','svg'):fig.savefig(out/('critical_cwnd_formula.'+ext),dpi=220)
    plt.close(fig)
    fig,ax=plt.subplots(figsize=(12,6),layout='constrained')
    for k in (5,8,11,14,19):
        rows=spread[(spread.rtt_index==k)&(spread['repeat']==1)].sort_values('g')
        ax.plot(rows.g,rows.L_ms,'o-',ms=4,lw=1.5,label=f'RTT bin k={k}')
    ax.axvline(CRITICAL,color='gray',ls=':',label='2 ln 2')
    ax.set(xlabel='g',ylabel='First-to-last span L (ms)',title='Packet spread versus g at fixed RTT bins')
    ax.legend();ax.grid(alpha=.2)
    for ext in ('png','pdf','svg'):fig.savefig(out/('L_vs_g_fixed_RTT.'+ext),dpi=220)
    plt.close(fig)
    actual=max(s['max_cwnd_error_bytes'] for s in summaries)
    messages=['# Fixed-feedback pacing model validation','',
        f'{len(summaries)} completed cases; fixed send-to-ACK RTT = {settings["rtt_ms"]} ms.',
        'No link serialization, queue delay, loss, window cap, or finite application data limit.',
        f'Maximum cwnd difference from exact formula at sampled times: {actual} bytes.',
        f'Maximum packet timestamp difference: {max(s["max_send_timestamp_error_ms"] for s in summaries):.9g} ms.',
        'This validates the paper model implementation against its formula; it is not a protocol-stack measurement.',
        'Each send trace is retained in the individual gain artifact; aggregate CSV files retain every repeat.']
    (out/'report.md').write_text('\n'.join(messages)+'\n',encoding='utf-8')
    (out/'experiment.json').write_text(json.dumps(settings,indent=2),encoding='utf-8')
    provenance=dict(source_commit=next(iter(commits)),paper_sha256=next(iter(paper_hashes)),
        complete_cases=len(summaries),repeated_traces_identical=True,
        files={p.name:digest(p) for p in out.iterdir() if p.is_file() and p.name!='provenance.json'})
    (out/'provenance.json').write_text(json.dumps(provenance,indent=2),encoding='utf-8')
    print('\n'.join(messages),flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--gain-id')
    parser.add_argument('--merge',type=Path)
    parser.add_argument('--settings',type=Path,default=HERE/'paper_experiment.json')
    args=parser.parse_args()
    settings=json.loads(args.settings.read_text(encoding='utf-8'))
    if args.merge:
        merge(args.merge,args.out,settings)
        return
    selected=[g for g in settings['gains'] if args.gain_id is None or g['id']==args.gain_id]
    if not selected:raise ValueError('Unknown gain id')
    for gain in selected:
        for repeat in range(1,settings['repeats']+1):
            run_case(args.out/'runs'/f'{gain["id"]}-r{repeat:02d}',gain,repeat,settings)


if __name__=='__main__':
    main()
