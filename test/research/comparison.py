"""Tables and shared axes from observed core logs. Formula values are references only."""
import csv
import json
import math
from pathlib import Path
import numpy as np
import matplotlib.pyplot as plt
from analyze_pacing_research import open_text, write_csv, load_packets


def continuous_exponent(gain):
    lo, hi = 0.0, math.log(2)
    for _ in range(60):
        mid = (lo+hi)/2
        if mid * math.exp(mid) < gain:
            lo = mid
        else:
            hi = mid
    return (lo+hi)/2


def compare(root, out):
    rows, spans, groups, curves, integrity = [], [], [], [], []
    for path in sorted((root/'runs').glob('*/config.json')):
        case = json.loads(path.read_text())
        summary = json.loads((path.parent/'summary.json').read_text())
        origin, end = summary['first_data_tx_ms'], summary['data_acked_ms']
        g, effective = case['g'], case['g_effective']
        trace = {}
        with open_text(path.parent/case['state_log']) as source:
            for line in source:
                if line.startswith('{'):
                    item=json.loads(line)
                    if item.get('type')=='ss' and item.get('stream_fd')==1 and origin <= item['t'] <= end:
                        trace[item['t']]=item
        if set(trace) != set(range(origin, end+1)):
            raise ValueError('Missing millisecond core observations')
        exponent=continuous_exponent(effective)
        local=[]
        for t, item in sorted(trace.items()):
            elapsed=t-origin
            reference=case['mss_bytes']*math.exp(exponent*elapsed/case['rtt_ms'])
            error=item['cwnd']-reference
            row=dict(case_id=case['case_id'], g_target=g, g_effective=effective,
                     repeat=case['repeat'], elapsed_ms=elapsed, rtt=elapsed/case['rtt_ms'],
                     cwnd_bytes=item['cwnd'], flight_bytes=item['flight'], reserved_bytes=item['reserved'],
                     srtt_ms=item['srtt'], ssthresh_bytes=item['ssthresh'],
                     continuous_reference_bytes=reference, signed_error_bytes=error,
                     absolute_error_bytes=abs(error), relative_error=error/reference,
                     all_payload_sent=t>=summary['last_data_tx_ms'],
                     cwnd_cap_reached=item['cwnd']>=case['cwnd_limit_bytes'])
            local.append(row)
        rows.extend(local)
        bins=load_packets(path.parent/case['packet_rtt_file'],case,summary)
        selected=[dict(r,g_target=g,contains_final_send=(
            origin+r['rtt_index']*case['rtt_ms'] <= summary['last_data_tx_ms'] <
            origin+(r['rtt_index']+1)*case['rtt_ms'])) for r in bins if r['scope']=='new']
        spans.extend(selected)
        with (path.parent/'packet_groups.csv').open() as source:
            groups.extend(dict(r,case_id=case['case_id'],g_target=g,g_effective=effective,repeat=case['repeat'])
                          for r in csv.DictReader(source))
        evidence=json.loads((path.parent/'artifact_integrity.json').read_text())
        integrity.append(dict(g=g,repeat=case['repeat'],fingerprints=[evidence[k]['uncompressed_sha256']
                              for k in ('state_log','packet_rtt_file','network_events')]))
        if case['repeat']==1:
            curves.append((g,local,selected))
    curves.sort(key=lambda item:item[0])
    write_csv(out/'cwnd_every_ms.csv',rows)
    write_csv(out/'cwnd_RTT_boundaries.csv',[r for r in rows if r['elapsed_ms']%100==0])
    write_csv(out/'L_fixed_RTT_all.csv',spans)
    wide={}
    for row in spans:
        if row['repeat']==1:
            wide.setdefault(row['rtt_index'],{})[f"g_{row['g_target']:.17g}"]=row['span_ms']
    write_csv(out/'L_by_RTT_wide.csv',[dict(rtt_index=k,**values) for k,values in sorted(wide.items())])
    write_csv(out/'L_packet_index_groups.csv',groups)
    checks=[]
    for g in sorted({r['g'] for r in integrity}):
        samples=[r for r in integrity if r['g']==g]
        checks.append(dict(g=g,repeats=len(samples),identical=len({tuple(r['fingerprints']) for r in samples})==1))
    write_csv(out/'deterministic_repeat_check.csv',checks)
    if any(not row['identical'] for row in checks):
        raise ValueError('Deterministic repeats differ; inspect raw traces')
    plt.rcParams.update({'font.size':12})
    colors=plt.cm.turbo(np.linspace(.03,.97,len(curves)))
    fig, axes=plt.subplots(2,1,figsize=(13,10))
    for (g,local,_),color in zip(curves,colors):
        x=[r['rtt'] for r in local]; y=[r['cwnd_bytes']/1430 for r in local]
        axes[0].step(x,y,where='post',label=f'{g:.9g}',color=color)
        subset=[r for r in local if 2*math.log(2)-.101 <= g <= 2*math.log(2)+.101 and 8<=r['rtt']<=10]
        if subset:
            axes[1].step([r['rtt'] for r in subset],[r['cwnd_bytes']/1430 for r in subset],where='post',label=f'{g:.9g}',color=color)
    axes[0].set_yscale('log',base=2)
    for ax in axes:
        ax.set(xlabel='RTTs since first actual DATA send',ylabel='Observed cwnd / MSS');ax.grid(alpha=.25)
    axes[0].legend(title='Target gain',ncol=4,fontsize=9)
    axes[1].legend(title='Near 2 ln 2',fontsize=10)
    fig.tight_layout();fig.savefig(out/'cwnd_time_and_internal.png',dpi=160);plt.close(fig)
    fig,axes=plt.subplots(2,1,figsize=(13,10))
    fig.suptitle('Actual DATA spans; finite-transfer ending is included',fontsize=13)
    for (g,_,bins),color in zip(curves,colors):
        selected=[r for r in bins if r['full_rtt'] and r['packet_count']>=2]
        axes[0].plot([r['rtt_index'] for r in selected],[r['span_ms'] for r in selected],'.-',label=f'{g:.9g}',color=color)
    for k in (1,2,4,8,12,16,18):
        points=[(g,r['span_ms']) for g,_,bins in curves for r in bins
                if r['rtt_index']==k and r['full_rtt'] and r['packet_count']>=2]
        if points: axes[1].plot(*zip(*points),'.-',label=f'RTT interval {k}')
    axes[0].set(xlabel='Fixed interval n: [nR, (n+1)R)',ylabel='Last minus first DATA send (ms)')
    axes[1].set(xlabel='Target gain',ylabel='Last minus first DATA send (ms)')
    axes[1].axvline(2*math.log(2),ls='--',color='black',label='2 ln 2 reference')
    for ax in axes:ax.grid(alpha=.25);ax.legend(ncol=4,fontsize=9)
    fig.tight_layout(rect=[0,0,1,.97]);fig.savefig(out/'L_by_RTT_and_gain.png',dpi=160);plt.close(fig)
    (out/'READ-ME.md').write_text('''# SCP core observations

All observed values originate from the baseline SCP core over its shared virtual network.
Time zero is the first forward DATA transport-send callback. The network and timers advance
in 1 ms steps; network arrivals precede timers. Core state is logged after timers, before the
application step. cwnd does not change in the application step. Flight at that sample may
precede application-driven sends at the same millisecond; packet events retain those sends.

The continuous reference is MSS * exp(min(W0(g_effective), ln(2)) * t/R), where W0 solves
lambda*exp(lambda)=g. Its amplitude is normalized at time zero, not fitted. It is an asymptotic
fluid reference, not an exact startup trajectory with the initial one-packet history.
The queued paper model retains a terminal gap that this core intentionally does not retain.
The old paper finite span is shown only in the separate packet-index group table; its limit
2 ln(2)/g must not be applied to fixed-RTT-bin spans or treated as this core's proven limit.

Tables retain all repeats, empty and partial final bins, and cap flags. Lines use repeat 1;
repeat hashes must agree. There are no statistical error bars. A missing long-RTT line for a
gain means that the finite transfer ended before that interval, not that its span is zero.
The time/gain plots include all gains. Partial bins remain in CSV and are not treated as
complete periods in the L plots. Results describe actual millisecond send timestamps,
including multiple packets in one tick, not infinitely precise uniform pacing.
An otherwise complete RTT interval can contain the final DATA send and then no more data;
the contains_final_send flag identifies that finite-transfer effect. Its shorter span is
not evidence that a steady pacing regime changed.
''',encoding='utf-8')
