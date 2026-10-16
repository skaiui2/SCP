"""Check native transport events independently of pacing prediction formulas."""
import csv
import hashlib
import json
from collections import deque
from pathlib import Path
import subprocess


def shared_source(root, baseline):
    paths = ['scp.c', 'scp.h', 'test/plot.py', 'test/analyze_scp.py', 'test/fig.py',
             'test/run.py', 'test/virtual/vnet.c', 'test/virtual/vnet.h',
             'test/virtual/case_transfer.c']
    tracked = subprocess.check_output(['git', 'ls-tree', '-r', '--name-only', baseline,
                                      '--', 'lib', 'test/common'], cwd=root, text=True).splitlines()
    result = {}
    for name in paths + tracked:
        expected = subprocess.check_output(['git', 'show', f'{baseline}:{name}'], cwd=root)
        actual = (root / name).read_bytes()
        # Checkout line endings may differ; source content must not.
        if actual.replace(b'\r\n', b'\n') != expected.replace(b'\r\n', b'\n'):
            raise RuntimeError(f'Shared source differs from baseline: {name}')
        result[name] = hashlib.sha256(actual).hexdigest()
    return result


def transport_events(path, summary, mss):
    pending = deque()
    received = deque()
    cumulative = sends = completed = early = fractional = 0
    first = None
    expected_seq = None
    plans = []
    with Path(path).open(newline='') as source:
        for row in csv.DictReader(source):
            event, direction = row['event'], int(row['direction'])
            flags, t = int(row['flags']), int(row['t_ms'])
            if direction == 0 and event == 'send' and flags & 1:
                seq, size = int(row['seq']), int(row['payload_bytes'])
                if expected_seq is not None and seq != expected_seq:
                    raise RuntimeError('New DATA is reordered, repeated or missing')
                expected_seq = (seq + size) % 2**32
                if first is None:
                    first = t
                if not row['planned_q32_ms']:
                    raise RuntimeError('DATA did not pass the core pacing queue')
                plan = int(row['planned_q32_ms'])
                if plan >> 32 != t:
                    raise RuntimeError('Actual DATA send does not equal integer-floor reservation')
                fractional += bool(plan % 2**32)
                early += plan > t * 2**32
                plans.append((t, plan, size))
                pending.append((expected_seq, size, t))
                sends += 1
            data_feedback = first is not None and first <= t <= summary['data_acked_ms']
            if direction == 1 and flags == 2 and event == 'arrival' and data_feedback:
                if not pending:
                    raise RuntimeError('ACK without a preceding DATA send')
                end, size, sent = pending.popleft()
                if int(row['ack']) != end or int(row['echo_send_ms']) != sent or t - sent != 100:
                    raise RuntimeError('ACK identity or full feedback RTT differs')
                received.append((end, size, t))
            if direction == 1 and flags == 2 and event == 'input_returned' and data_feedback:
                if not received:
                    raise RuntimeError('ACK processed without an arrival')
                end, size, arrived = received.popleft()
                cumulative += size
                if t != arrived or int(row['ack']) != end or int(row['cwnd_bytes']) != mss + cumulative:
                    raise RuntimeError('Core ACK processing did not produce observed slow-start growth')
                completed += 1
    if pending or received or cumulative != summary['received_bytes'] or completed != sends:
        raise RuntimeError('Incomplete native DATA/ACK trace')
    if sends != summary['new_packets'] or first != summary['first_data_tx_ms']:
        raise RuntimeError('Trace and C summary disagree')
    group_rows = []
    q = 1
    effective = 2**32 / summary['gain_reciprocal_q32']
    while 4 * q - 1 <= len(plans):
        group = plans[2*q-1:4*q-1]
        row = dict(first_packet_index=2*q-1, packet_count=2*q, full_mss=all(p[2] == mss for p in group),
                   first_ms=group[0][0], last_ms=group[-1][0], L_ms=group[-1][0]-group[0][0],
                   old_tail_model_finite_L_ms=100 * (2 * sum(1/k for k in range(q+1, 2*q+1)) - 1/(2*q)) / effective)
        group_rows.append(row)
        q *= 2
    with Path(path).with_name('packet_groups.csv').open('w', newline='') as output:
        writer = csv.DictWriter(output, fieldnames=['first_packet_index', 'packet_count', 'full_mss',
            'first_ms', 'last_ms', 'L_ms', 'old_tail_model_finite_L_ms'])
        writer.writeheader()
        writer.writerows(group_rows)
    return dict(data_packets=sends, acknowledged_bytes=cumulative, feedback_ms=100,
                fractional_reservations=fractional, sends_before_fractional_deadline=early,
                actual_send_rule='floor(Q32.32 reservation)', core_window_per_ack_verified=True)
