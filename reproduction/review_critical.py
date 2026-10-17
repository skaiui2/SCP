"""Independently reprocess native packet events, without synthesizing ACKs or windows."""
import csv,gzip,json
from pathlib import Path
from collections import deque,defaultdict
import argparse
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--data', type=Path, required=True, help='Dataset containing runs/')
parser.add_argument('--out', type=Path, required=True, help='Separate generated analysis directory')
args=parser.parse_args()
CASE=args.data.resolve()/'runs/critical-r01'
DEST=args.out.resolve();DEST.mkdir(parents=True,exist_ok=True)
if not __debug__: raise RuntimeError('Do not run audits with python -O')
def write(name,rows):
 with (DEST/name).open('w',newline='',encoding='utf-8-sig') as f:
  w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
s=json.loads((CASE/'summary.json').read_text());origin=s['first_data_tx_ms'];end=s['data_acked_ms']
states={}
with gzip.open(CASE/'run.log.gz','rt') as f:
 for line in f:
  if line.startswith('{'):
   row=json.loads(line)
   if row.get('type')=='ss' and row['stream_fd']==1 and origin<=row['t']<=end:states[row['t']-origin]=row
pending=deque();arrived=deque();bins=defaultdict(lambda:[0]*100)
sent=acked=payload=0;plan_fractional=0;first_events=[];max_early=0
with gzip.open(CASE/'network_events.csv.gz','rt') as f:
 for row in csv.DictReader(f):
  t=int(row['t_ms']);direction=int(row['direction']);flags=int(row['flags']);event=row['event']
  if event=='send' and direction==0 and flags&1:
   size=int(row['payload_bytes']);seq=int(row['seq']);plan=int(row['planned_q32_ms'])
   assert plan>>32==t
   assert not sent or seq==last_end
   last_end=seq+size
   pending.append((last_end,t,size));sent+=1
   bins[(t-origin)//100][(t-origin)%100]+=1
   plan_fractional+=bool(plan%(1<<32));max_early=max(max_early,plan/(1<<32)-t)
   if sent<=20:first_events.append(dict(packet=sent,elapsed_ms=t-origin,cwnd_MSS=int(row['cwnd_bytes'])/1430,planned_elapsed_ms=plan/(1<<32)-origin))
  if origin<=t<=end and direction==1 and flags==2 and event=='arrival':
   endpoint,tx,size=pending.popleft()
   assert int(row['ack'])==endpoint and t-tx==100 and int(row['echo_send_ms'])==tx
   arrived.append((endpoint,t,size))
  if origin<=t<=end and direction==1 and flags==2 and event=='input_returned':
   endpoint,ta,size=arrived.popleft();payload+=size;acked+=1
   assert int(row['ack'])==endpoint and t==ta and int(row['cwnd_bytes'])==1430+payload
assert not pending and not arrived and sent==acked==750869 and payload==1073741824
rows=[]
for n in range((end-origin)//100+1):
 row=states[n*100];slots=bins[n];active=[i for i,c in enumerate(slots) if c]
 rows.append(dict(n=n,cwnd_MSS=row['cwnd']/1430,reference_2_power_n=2**n,relative_error_percent=100*(row['cwnd']/1430/2**n-1),
  flight_MSS=row['flight']/1430,packets=sum(slots),first_offset_ms=active[0] if active else None,last_offset_ms=active[-1] if active else None,
  L_ms=active[-1]-active[0] if active else None,occupied_slots=len(active),max_packets_per_ms=max(slots),
  final_send_in_bin=n==(s['last_data_tx_ms']-origin)//100))
write('critical_boundaries_and_spans.csv',rows);write('critical_first_packets.csv',first_events)
internal=[]
for n in range(1,19):
 errors=[100*(states[n*100+u]['cwnd']/1430/2**(n+u/100)-1) for u in range(100)]
 # Compare scaled curve after subtracting its one-packet offset; no fit or time shift.
 diffs=[states[(n+1)*100+u]['cwnd']-2*states[n*100+u]['cwnd']+1430 for u in range(100)]
 slotdiff=[bins[n+1][u]-2*bins[n][u] for u in range(100)]
 internal.append(dict(n=n,minimum_error_percent=min(errors),maximum_error_percent=max(errors),
  max_next_RTT_affine_error_bytes=max(map(abs,diffs)),max_next_RTT_histogram_double_error=max(map(abs,slotdiff))))
for row in internal:
 n=row['n']
 row['max_next_RTT_double_error_bytes']=max(abs(states[(n+1)*100+u]['cwnd']-2*states[n*100+u]['cwnd']) for u in range(100))
write('critical_internal_error.csv',internal)
info=dict(packets=sent,ACKs=acked,bytes=payload,feedback_ms=100,all_ACK_cwnd_checks=True,
 fractional_reservations=plan_fractional,max_early_ms=max_early,origin_absolute_ms=origin,
 data_duration_ms=end-origin,g_target=s['g'],g_effective=s['g_effective'])
(DEST/'critical_independent_audit.json').write_text(json.dumps(info,indent=2))


audit = {
    'first_DATA_absolute_ms': origin,
    'comparison_elapsed_ms_inclusive': [800, 1899],
    'maximum_abs_W_t_plus_R_minus_2W_t_bytes': max(
        abs(states[t + 100]['cwnd'] - 2 * states[t]['cwnd']) for t in range(800, 1900)),
    'max_reserved_minus_flight_bytes_800_through_1899': max(
        states[t]['reserved'] - states[t]['flight'] for t in range(800, 1900)),
    'min_reserved_minus_flight_bytes_800_through_1899': min(
        states[t]['reserved'] - states[t]['flight'] for t in range(800, 1900)),
    'scope': 'Observed 1 ms states, not an infinite-time theorem.'
}
(DEST / 'critical_phase_audit.json').write_text(json.dumps(audit, indent=2))
print(json.dumps(audit))

print('PASS: 750869 native DATA/ACK pairs, 100 ms feedback and per-ACK core cwnd updates')
