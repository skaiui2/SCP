"""Audit a complete native SCP campaign; formula columns are labelled references only."""
import csv,gzip,json,math,hashlib
from pathlib import Path
from collections import defaultdict
import argparse
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--data', type=Path, required=True, help='Dataset containing runs/')
parser.add_argument('--out', type=Path, required=True, help='Separate generated analysis directory')
parser.add_argument('--expected-commit', default='70f170ed505a55d83044eda2a2d5684fc80f382b')
args=parser.parse_args()
DATA=args.data.resolve()
OUT=args.out.resolve();OUT.mkdir(parents=True,exist_ok=True)
if not __debug__: raise RuntimeError('Do not run audits with python -O')
def sha(p):
 with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def write(name,rows):
 with (OUT/name).open('w',newline='',encoding='utf-8-sig') as f:
  w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
def root_exponent(g):
 lo,hi=0,math.log(2)
 for _ in range(60):
  x=(lo+hi)/2
  if x*math.exp(x)<g:lo=x
  else:hi=x
 return (lo+hi)/2
cases=[];repeats=defaultdict(list);sources=set()
for path in sorted((DATA/'runs').glob('*/config.json')):
 d=path.parent;c=json.loads(path.read_text());s=json.loads((d/'summary.json').read_text());a=json.loads((d/'execution_audit.json').read_text())
 assert c['commit']==args.expected_commit
 assert s['received_bytes']==s['accepted_bytes']==1073741824 and s['checksum_a']==s['checksum_b']=='7a4bb4de42097f8a'
 assert s['dropped']==s['retrans_packets']==s['observer_errors']==0 and s['feedback_min_ms']==s['feedback_max_ms']==100
 assert s['feedback_count']==s['new_packets']==750869 and a['core_window_per_ack_verified']
 assert s['gain_reciprocal_q32']==round(2**32/c['g']) and not s['cwnd_cap_reached']
 integ=json.loads((d/'artifact_integrity.json').read_text());derived=json.loads((d/'derived_integrity.json').read_text())
 for name,digest in derived.items():assert sha(d/name)==digest
 for key,name in [('state_log','run.log.gz'),('packet_rtt_file','packet_rtt.csv.gz'),('network_events','network_events.csv.gz')]:
  assert sha(d/name)==integ[key]['compressed_sha256']
 repeats[c['gain_id']].append(tuple(integ[k]['uncompressed_sha256'] for k in ['state_log','packet_rtt_file','network_events']))
 sources.add(c['source_identity_sha256'])
 if c['repeat']!=1:continue
 origin=s['first_data_tx_ms'];end=s['data_acked_ms'];states={}
 with gzip.open(d/'run.log.gz','rt') as f:
  for line in f:
   if line.startswith('{'):
    r=json.loads(line)
    if r.get('type')=='ss' and r['stream_fd']==1 and origin<=r['t']<=end:states[r['t']-origin]=r
 bins={}
 with gzip.open(d/'packet_rtt.csv.gz','rt') as f:
  for r in csv.DictReader(f):
   if r['scope']=='new':bins[int(r['rtt_index'])]=json.loads(r['slot_counts'])
 g=c['g'];effective=c['g_effective']
 active=[k for k,n in enumerate(bins[18]) if n]
 w17=states[1700]['cwnd']/1430;w18=states[1800]['cwnd']/1430
 w10=states[1000]['cwnd']/1430
 rate=(w18/w10)**(1/8)
 row=dict(gain_id=c['gain_id'],g_target=g,g_effective=effective,W10_MSS=w10,W18_MSS=w18,
  r17_to_18=w18/w17,r10_to_18=rate,queued_reference_r=min(math.exp(effective/2),2),
  fluid_reference_r=math.exp(root_exponent(effective)),L18_ms=active[-1]-active[0],occupied_ms18=len(active),
  peak_packets_ms18=max(bins[18]),peak_fraction18=max(bins[18])/sum(bins[18]),
  data_duration_ms=end-origin,final_send_elapsed_ms=s['last_data_tx_ms']-origin)
 cases.append(row)
for gain,fingerprints in repeats.items():
 assert len(fingerprints)==3 and len(set(fingerprints))==1
assert len(cases)==22 and len(sources)==1
cases.sort(key=lambda r:r['g_target']);write('gain_summary_independent.csv',cases)
(OUT/'campaign_audit.json').write_text(json.dumps(dict(cases=66,gains=22,repeats=3,all_checks_passed=True,source_identities=len(sources),checksum='7a4bb4de42097f8a',note='All 66 artifact hashes/summaries/audits checked. Run review_critical.py separately for independent critical packet verification.'),indent=2))
print('PASS: 66 case hashes, summaries, gains and three identical repetitions per gain')
