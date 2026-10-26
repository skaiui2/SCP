"""Check native transport events independently of pacing prediction formulas."""
import csv
import gzip
import hashlib
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
        expected = expected.replace(b'\r\n', b'\n')
        # Checkout line endings may differ; source content must not.
        if actual.replace(b'\r\n', b'\n') != expected:
            raise RuntimeError(f'Shared source differs from baseline: {name}')
        result[name] = hashlib.sha256(actual).hexdigest()
    return result


def transport_events(path, summary, mss):
    mode = summary['mode']
    if mode not in ('always_slow_start', 'always_congestion_avoidance'):
        raise RuntimeError('Unknown native congestion-control experiment')
    ca_update = summary.get('ca_update', 'byte-floor')
    if ca_update not in ('byte-floor', 'byte-remainder'):
        raise RuntimeError('Unknown native CA update rule')
    scale = int(summary.get('tick_scale', 1))
    end_tick = summary.get('data_acked_tick', round(summary['data_acked_ms'] * scale))
    policy = summary['gain_policy']
    if policy not in ('fixed_q16', 'native_aimd'):
        raise RuntimeError('Unknown native pacing gain policy')
    forward = deque()
    delivered = deque()
    ack_sends = deque()
    reverse = deque()
    received = deque()
    reservations = deque()
    cumulative = sends = completed = early = fractional = 0
    forward_returns = generated_acks = assigned_bytes = assigned_packets = slot_updates = 0
    next_slot = 0
    first = first_growth = None
    expected_seq = None
    window, counter = mss, 0
    paper_window = 1.0
    square_sum = max_square_residual = 0.0
    min_paper_error = max_paper_error = 0.0
    last_full_paper_reference = mss
    last_full_observed = mss
    fields = ['ack_index', 'elapsed_ms', 'payload_bytes', 'full_mss', 'observed_before_bytes',
              'observed_cwnd_bytes', 'integer_prediction_bytes', 'integer_error_bytes',
              'paper_ack_reference_bytes', 'paper_error_bytes', 'paper_relative_error',
              'paper_square_identity_residual', 'ca_update', 'observed_cnt_before',
              'observed_cnt_after', 'integer_prediction_cnt', 'integer_cnt_error']
    with Path(path).open(newline='') as source, gzip.open(
            Path(path).with_name('ack_updates.csv.gz'), 'wt', newline='', compresslevel=1) as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for row in csv.DictReader(source):
            event, direction = row['event'], int(row['direction'])
            flags = int(row['flags'])
            t = int(row['t_tick']) if 't_tick' in row else round(float(row['t_ms']) * scale)
            echo = int(row['echo_send_tick']) if 'echo_send_tick' in row else round(float(row['echo_send_ms']) * scale)
            if float(row['t_ms']) != t / scale or float(row['echo_send_ms']) != echo / scale:
                raise RuntimeError('Tick and physical-ms event timestamps disagree')
            if row['gain_q16']:
                gain = int(row['gain_q16'])
                cwnd = int(row['cwnd_bytes'])
                expected_gain = summary['gain_q16'] if policy == 'fixed_q16' else (
                    90852 if mode == 'always_slow_start' else 107998)
                if gain != expected_gain:
                    raise RuntimeError('Observed gain callback differs from its registered policy')
                observed_slot = int(row['next_slot_q32_tick']) if 'next_slot_q32_tick' in row else int(row['next_slot_q32_ms']) * scale
                if observed_slot != next_slot:
                    cursor = max(next_slot, t << 32)
                    if observed_slot < cursor:
                        raise RuntimeError('Next pacing slot moved backwards or before current time')
                    while cursor < observed_slot:
                        size = min(mss, summary['received_bytes'] - assigned_bytes)
                        if not size:
                            raise RuntimeError('Pacing assigned more than the transfer payload')
                        gap = scale << 32
                        if first is not None and t >= first + 100 * scale:
                            gap = size * 100 * scale * ((65536 << 32) // gain) // cwnd
                        following = cursor + gap
                        if following > observed_slot:
                            raise RuntimeError('Next pacing slot is not the end of its reserved gaps')
                        reservations.append((cursor, size))
                        assigned_bytes += size
                        assigned_packets += 1
                        cursor = following
                    next_slot = observed_slot
                    slot_updates += 1
            if direction == 0 and event == 'send' and flags & 1:
                seq, size = int(row['seq']), int(row['payload_bytes'])
                if expected_seq is not None and seq != expected_seq:
                    raise RuntimeError('New DATA is reordered, repeated or missing')
                expected_seq = (seq + size) % 2**32
                if first is None:
                    first = t
                if not row.get('planned_q32_tick', row.get('planned_q32_ms')):
                    raise RuntimeError('DATA did not pass the core pacing queue')
                plan = int(row['planned_q32_tick']) if 'planned_q32_tick' in row else int(row['planned_q32_ms']) * scale
                if not reservations or reservations.popleft() != (plan, size):
                    raise RuntimeError('DATA reservation differs from the retained next-slot scheduler')
                if plan >> 32 != t:
                    raise RuntimeError('Actual DATA send does not equal integer-floor reservation')
                fractional += bool(plan % 2**32)
                early += plan > t * 2**32
                forward.append((expected_seq, size, t))
                sends += 1
            data_feedback = first is not None and first <= t <= end_tick
            if direction == 0 and flags & 1 and event == 'arrival' and data_feedback:
                if not forward:
                    raise RuntimeError('DATA arrived without a preceding transport send')
                end, size, sent = forward.popleft()
                if (int(row['seq']) + int(row['payload_bytes'])) % 2**32 != end or t - sent != 50 * scale:
                    raise RuntimeError('Forward DATA identity or one-way delay differs')
                delivered.append((end, size, t))
                ack_sends.append((end, size, sent))
            if direction == 1 and flags == 2 and event == 'send' and data_feedback:
                if not ack_sends:
                    raise RuntimeError('Receiver emitted an ACK without receiving DATA')
                end, size, sent = ack_sends.popleft()
                if int(row['ack']) != end or echo != sent or t - sent != 50 * scale:
                    raise RuntimeError('Receiver ACK identity or emission time differs')
                reverse.append((end, size, sent, t))
                generated_acks += 1
            if direction == 0 and flags & 1 and event == 'input_returned' and data_feedback:
                if not delivered:
                    raise RuntimeError('Receiver DATA processing returned without an arrival')
                end, size, arrived = delivered.popleft()
                if (int(row['seq']) + int(row['payload_bytes'])) % 2**32 != end or t != arrived:
                    raise RuntimeError('Receiver DATA processing identity or time differs')
                forward_returns += 1
            if direction == 1 and flags == 2 and event == 'arrival' and data_feedback:
                if not reverse:
                    raise RuntimeError('ACK without a preceding DATA send')
                end, size, sent, emitted = reverse.popleft()
                if t - emitted != 50 * scale:
                    raise RuntimeError('Reverse ACK one-way delay differs')
                if int(row['ack']) != end or echo != sent or t - sent != 100 * scale:
                    raise RuntimeError('ACK identity or full feedback RTT differs')
                if int(row['cwnd_bytes']) != window:
                    raise RuntimeError('ACK arrival window differs from the preceding core update')
                if ca_update == 'byte-remainder' and int(row['cwnd_cnt']) != counter:
                    raise RuntimeError('ACK arrival CA remainder differs from preceding core update')
                received.append((end, size, t))
            if direction == 1 and flags == 2 and event == 'input_returned' and data_feedback:
                if not received:
                    raise RuntimeError('ACK processed without an arrival')
                end, size, arrived = received.popleft()
                cumulative += size
                before, counter_before = window, counter
                if mode == 'always_slow_start':
                    increment = size
                elif ca_update == 'byte-remainder':
                    increment, counter = divmod(mss * size + counter_before, before)
                else:
                    increment = max(1, mss * size // before)
                prediction = min(summary.get('cwnd_limit_bytes', 2147483647), before + increment)
                window = int(row['cwnd_bytes'])
                if t != arrived or int(row['ack']) != end or window != prediction:
                    raise RuntimeError('Core ACK processing differs from its configured AIMD phase')
                observed_counter = int(row['cwnd_cnt']) if row.get('cwnd_cnt') != '' and 'cwnd_cnt' in row else None
                if ca_update == 'byte-remainder' and observed_counter != counter:
                    raise RuntimeError('Core ACK CA remainder differs from divmod update')
                completed += 1
                if completed == 1:
                    first_growth = t
                paper_before = paper_window
                if mode == 'always_congestion_avoidance':
                    paper_window += (size / mss) / paper_window
                paper_reference = mss + cumulative if mode == 'always_slow_start' else paper_window * mss
                paper_error = window - paper_reference
                min_paper_error = min(min_paper_error, paper_error)
                max_paper_error = max(max_paper_error, paper_error)
                residual = None
                if size == mss:
                    if mode == 'always_congestion_avoidance':
                        square_sum += 1 / paper_before**2
                        residual = paper_window**2 - 1 - 2 * completed - square_sum
                        max_square_residual = max(max_square_residual, abs(residual))
                    last_full_paper_reference, last_full_observed = paper_reference, window
                writer.writerow(dict(ack_index=completed, elapsed_ms=(t-first)/scale, payload_bytes=size,
                    full_mss=size == mss, observed_before_bytes=before, observed_cwnd_bytes=window,
                    integer_prediction_bytes=prediction, integer_error_bytes=window-prediction,
                    paper_ack_reference_bytes=paper_reference,
                    paper_error_bytes=window-paper_reference,
                    paper_relative_error=(window-paper_reference)/paper_reference,
                    paper_square_identity_residual=residual, ca_update=ca_update,
                    observed_cnt_before=counter_before if ca_update == 'byte-remainder' else None,
                    observed_cnt_after=observed_counter, integer_prediction_cnt=counter if ca_update == 'byte-remainder' else None,
                    integer_cnt_error=observed_counter-counter if ca_update == 'byte-remainder' else None))
    if (any((forward, delivered, ack_sends, reverse, received, reservations)) or
            cumulative != summary['received_bytes'] or completed != sends or
            generated_acks != sends or forward_returns != sends or assigned_packets != sends):
        raise RuntimeError('Incomplete native DATA/ACK trace')
    if sends != summary['new_packets'] or first != summary.get('first_data_tx_tick', round(summary['first_data_tx_ms'] * scale)):
        raise RuntimeError('Trace and C summary disagree')
    return dict(ca_update=ca_update, remainder_per_ack_verified=ca_update == "byte-remainder",
                min_paper_error_bytes=min_paper_error, max_paper_error_bytes=max_paper_error,
                max_absolute_paper_error_bytes=max(abs(min_paper_error), abs(max_paper_error)),
                tick_scale=scale, data_packets=sends, acknowledged_bytes=cumulative, feedback_ms=100, first_window_growth_ms=first_growth/scale,
                first_window_growth_elapsed_ms=(first_growth-first)/scale,
                fractional_reservations=fractional, sends_before_fractional_deadline=early,
                actual_send_rule='floor(Q32.32 reservation)', core_window_per_ack_verified=True,
                mode=mode, gain_policy=policy, max_integer_update_error_bytes=0,
                receiver_data_processing_verified=True, receiver_ack_generation_verified=True,
                next_slot_rule_verified=True, assigned_packets=assigned_packets,
                observed_next_slot_updates=slot_updates,
                last_full_packet_observed_cwnd_bytes=last_full_observed,
                last_full_packet_paper_reference_bytes=last_full_paper_reference,
                last_full_packet_paper_relative_error=(last_full_observed-last_full_paper_reference)
                    / last_full_paper_reference,
                max_paper_square_identity_residual=max_square_residual,
                paper_tail_rule='Final short ACK uses byte weighting; CA square identity excludes it')
