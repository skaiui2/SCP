"""Plot existing native SCP logs; no simulation, fitting, or time shifting."""
import csv
import gzip
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.ticker import FuncFormatter
import numpy as np

if not __debug__:
    raise RuntimeError("Do not run evidence checks with python -O")

import argparse
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--data', type=Path, required=True, help='Dataset containing runs/ (scp-fixed-results)')
parser.add_argument('--out', type=Path, required=True, help='Separate generated analysis directory')
args = parser.parse_args()
ROOT = args.data.resolve()
OUT = args.out.resolve()
OUT.mkdir(parents=True, exist_ok=True)
plt.rcParams.update({
    'font.family': 'STIXGeneral', 'mathtext.fontset': 'stix',
    'font.size': 11, 'axes.labelsize': 12, 'xtick.labelsize': 10,
    'ytick.labelsize': 10, 'axes.linewidth': .7,
    'axes.spines.top': False, 'axes.spines.right': False,
    'pdf.fonttype': 42, 'ps.fonttype': 42, 'svg.fonttype': 'path',
    'path.simplify': False,
})

cases = []
source_commits = set()
window_rows = []
span_rows = []
for config_path in sorted((ROOT / 'runs').glob('*/config.json')):
    config = json.loads(config_path.read_text())
    source_commits.add(config['commit'])
    if config['repeat'] != 1:
        continue
    folder = config_path.parent
    summary = json.loads((folder / 'summary.json').read_text())
    origin, end = summary['first_data_tx_ms'], summary['data_acked_ms']
    last_tx = summary['last_data_tx_ms'] - origin
    states = []
    with gzip.open(folder / 'run.log.gz', 'rt') as source:
        for line in source:
            if not line.startswith('{'):
                continue
            row = json.loads(line)
            if row.get('type') == 'ss' and row['stream_fd'] == 1 and origin <= row['t'] <= end:
                t = row['t'] - origin
                states.append((t, row['cwnd']))
                window_rows.append({'gain_id': config['gain_id'], 'g_target': config['g'],
                                    'elapsed_ms': t, 'cwnd_bytes': row['cwnd'],
                                    'cwnd_MSS': row['cwnd'] / 1430})
    spans = []
    with gzip.open(folder / 'packet_rtt.csv.gz', 'rt') as source:
        for row in csv.DictReader(source):
            if row['scope'] != 'new':
                continue
            n = int(row['rtt_index'])
            slots = json.loads(row['slot_counts'])
            active = [i for i, count in enumerate(slots) if count]
            if not active:
                continue
            partial = n == last_tx // 100
            L = active[-1] - active[0]
            spans.append((n * 100, L, partial))
            span_rows.append({'gain_id': config['gain_id'], 'g_target': config['g'],
                              'interval_start_ms': n * 100, 'interval_end_ms': (n + 1) * 100,
                              'L_ms': L, 'packets': sum(slots), 'occupied_ms': len(active),
                              'final_DATA_interval': partial})
    cases.append({'g': config['g'], 'id': config['gain_id'], 'states': sorted(states),
                  'spans': sorted(spans), 'last_tx': last_tx})
cases.sort(key=lambda item: item['g'])
assert len(cases) == 22
for name, rows in [('all_gain_cwnd_time.csv', window_rows), ('all_gain_L_time.csv', span_rows)]:
    with (OUT / name).open('w', encoding='utf-8-sig', newline='') as target:
        writer = csv.DictWriter(target, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)




from matplotlib.ticker import MultipleLocator
plt.rcParams.update({'font.family':'DejaVu Sans','mathtext.fontset':'dejavusans',
 'font.size':11,'axes.labelsize':12,'xtick.labelsize':10,'ytick.labelsize':10})
palette=['#0072B2','#D55E00','#009E73','#A65AA0','#6753A3','#A17A00',
         '#008B9A','#BC3854','#63802A','#6A7077']
styles=['-',(0,(6,2.5)),(0,(2,2))]
handles=[]
for i,c in enumerate(cases):
 critical=c['id']=='critical'
 c['color']='#151B23' if critical else palette[i%10]
 c['ls']='-' if critical else styles[i//8]
 c['lw']=2 if critical else 1.25
 label=r'$2\ln 2$' if critical else f"{c['g']:.6f}".rstrip('0').rstrip('.')
 handles.append(Line2D([],[],color=c['color'],lw=c['lw'],ls=c['ls'],label=label))

def frame(ylabel,xlabel):
 fig,ax=plt.subplots(figsize=(9.2,6.3))
 fig.subplots_adjust(left=.12,right=.985,top=.965,bottom=.325)
 ax.set_xlabel(xlabel,labelpad=8)
 ax.set_ylabel(ylabel,labelpad=10)
 ax.grid(axis='y',color='#dfe4e8',linewidth=.6)
 ax.tick_params(direction='out',length=3.5,width=.7)
 ax.xaxis.set_minor_locator(MultipleLocator(1))
 ax.set_xlim(0,27)
 ax.set_xticks([0,5,10,15,20,25])
 fig.legend(handles=handles,title=r'Pacing gain $g$',ncol=6,
            loc='lower center',bbox_to_anchor=(.54,.018),frameon=False,
            fontsize=9,title_fontsize=10,handlelength=2.7,
            columnspacing=1.3,labelspacing=.8)
 return fig,ax

def save(fig,name):
 assert len(fig.axes)==1
 for ext in ('png','pdf','svg'):
  fig.savefig(OUT/f'{name}.{ext}',dpi=300,facecolor='white')
 plt.close(fig)

fig,ax=frame('Congestion window (MSS)',r'Elapsed RTTs, $t/R$')
for c in cases:
 arr=np.array(c['states'])
 x=arr[:,0]/100
 y=arr[:,1]/1430
 line,=ax.step(x,y,where='post',color=c['color'],ls=c['ls'],lw=c['lw'],
              zorder=4 if c['id']=='critical' else 2)
 assert np.array_equal(line.get_xdata(),x) and np.array_equal(line.get_ydata(),y)
 assert line.get_marker()=='None'
ax.set_ylim(0,800000)
ax.set_yticks([0,200000,400000,600000,800000])
ax.yaxis.set_major_formatter(FuncFormatter(lambda v,pos:f'{v:,.0f}'))
save(fig,'paper_cwnd_rtt')

fig,ax=frame(r'Within-RTT transmission span $L$ (ms)',r'RTT interval index, $n$')
for c in cases:
 arr=np.array([(t/100,L) for t,L,partial in c['spans'] if not partial])
 ax.plot(arr[:,0],arr[:,1],color=c['color'],ls=c['ls'],lw=c['lw'],
         zorder=4 if c['id']=='critical' else 2)
 final=[(t/100,L) for t,L,partial in c['spans'] if partial]
 if final:
  ax.plot([arr[-1,0],final[0][0]],[arr[-1,1],final[0][1]],
          color=c['color'],ls=':',lw=1,alpha=.55,zorder=1)
ax.set_ylim(0,104)
ax.set_yticks([0,20,40,60,80,100])
save(fig,'paper_span_rtt')
(OUT/'paper_rtt_captions.md').write_text("""# Figure captions

**Figure 1. Congestion-window evolution for 22 pacing gains.** Native SCP core measurements over a virtual network with a fixed send-to-ACK RTT R = 100 ms, no loss, a 1 GiB transfer, and MSS = 1430 bytes. Both axes are linear. Time is normalized by R, with zero at the first actual data transmission. All original 1 ms window samples are plotted; curves stop at the final data acknowledgment. The thick black curve denotes g = 2 ln 2. Overlapping curves have not been displaced. One of three identical deterministic repetitions is shown.

**Figure 2. Within-RTT transmission span for the same gains.** Each value is the last minus the first actual data transmission time in [nR, (n+1)R), plotted at interval index n. Single-packet intervals have zero span; empty intervals are omitted. Connecting lines guide the eye. Faint dotted terminal segments identify the intervals containing the final data transmission, whose spans are affected by the finite transfer size. These intervals are not the paper's packet-index groups. Colors and line styles match Figure 1.

Gain labels are rounded to six decimal places for readability; configuration values are unchanged. The highlighted target is 1.3862943611198906, with effective integer-configured gain 1.386294361280859. No smoothing, fitting, interpolation of missing observations, or fabricated values were used. Figure 1 uses a step rendering of the original state samples. Source commits are recorded in figure_provenance.json; identify an archived campaign using its manifest and verified archive hashes.
""",encoding='utf-8')
(OUT/'figure_provenance.json').write_text(json.dumps({
    'dataset': str(ROOT), 'source_commits': sorted(source_commits),
    'inputs': ['runs/*/config.json', 'runs/*/summary.json',
               'runs/*/run.log.gz', 'runs/*/packet_rtt.csv.gz'],
    'repeat_plotted': 1, 'window_samples': len(window_rows),
    'matplotlib': matplotlib.__version__, 'numpy': np.__version__,
}, indent=2), encoding='utf-8')
print(json.dumps({'figures':2,'panels_each':1,'gains_each':len(cases),'window_samples':len(window_rows),
                  'axes':'linear','source':'native SCP logs','RTT_ms':100,'MSS_bytes':1430}))
