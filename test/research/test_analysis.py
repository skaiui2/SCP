import csv
import gzip
import importlib.util
import json
from pathlib import Path
import unittest
import tempfile
from unittest.mock import patch
import run_scp_fixed_feedback as runner
import audit

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('analysis', ROOT / 'analyze_pacing_research.py')
analysis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analysis)


class AnalysisTests(unittest.TestCase):
    def native_trace(self, mode='always_congestion_avoidance', policy='fixed_q16', sizes=(1430, 1430), ca_update='byte-floor'):
        mss = window = 1430
        counter = 0
        next_slot = 0
        seq = 10
        rows = []
        fields = ['event', 'direction', 't_ms', 'flags', 'seq', 'ack', 'payload_bytes',
                  'echo_send_ms', 'cwnd_bytes', 'cwnd_cnt', 'planned_q32_ms', 'gain_q16', 'next_slot_q32_ms']

        def record(event, direction, t, flags, data_seq, ack, size, echo, plan=''):
            gain = 65536 if policy == 'fixed_q16' else (
                90852 if mode == 'always_slow_start' else 107998)
            rows.append(dict(zip(fields, (event, direction, t, flags, data_seq, ack, size,
                                         echo, window, counter, plan, gain, next_slot))))

        for index, size in enumerate(sizes):
            sent = 100 + index * 100
            plan = max(next_slot, sent << 32)
            gain = 65536 if policy == 'fixed_q16' else (
                90852 if mode == 'always_slow_start' else 107998)
            gap = 1 << 32 if not index else size * 100 * ((65536 << 32) // gain) // window
            next_slot = plan + gap
            end = seq + size
            record('send', 0, sent, 1, seq, 0, size, sent, plan)
            record('arrival', 0, sent + 50, 1, seq, 0, size, sent)
            record('send', 1, sent + 50, 2, 0, end, 0, sent)
            record('input_returned', 0, sent + 50, 1, seq, 0, size, sent)
            record('arrival', 1, sent + 100, 2, 0, end, 0, sent)
            if mode == 'always_slow_start':
                window += size
            elif ca_update == 'byte-remainder':
                increment, counter = divmod(mss * size + counter, window)
                window += increment
            else:
                window += max(1, mss * size // window)
            record('input_returned', 1, sent + 100, 2, 0, end, 0, sent)
            seq = end
        summary = dict(mode=mode, gain_policy=policy, ca_update=ca_update,
                       gain_q16=65536 if policy == 'fixed_q16' else None,
                       data_acked_ms=sent + 100, received_bytes=sum(sizes), new_packets=len(sizes),
                       first_data_tx_ms=100)
        return fields, rows, summary

    def write_trace(self, path, fields, rows):
        with path.open('w', newline='') as output:
            writer = csv.DictWriter(output, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    def test_shared_sources_require_the_exact_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            names = ['scp.c', 'scp.h', 'test/plot.py', 'test/analyze_scp.py', 'test/fig.py',
                     'test/run.py', 'test/virtual/vnet.c', 'test/virtual/vnet.h',
                     'test/virtual/case_transfer.c']
            baseline = {name: b'unchanged\n' for name in names}
            baseline['scp.c'] = (b'static void scp_aimd_init(struct scp_stream *ss)\n{\n'
                                 b'    ss->cwnd = MSS;\n    ss->ssthresh = CWND_WIN_MAX;\n}\n')
            baseline['scp.h'] = b'#define MTU 1460\n'
            for name, contents in baseline.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(contents.replace(b'\n', b'\r\n'))

            def git_output(command, **kwargs):
                return '' if command[1] == 'ls-tree' else baseline[command[2].split(':', 1)[1]]

            with patch.object(audit.subprocess, 'check_output', side_effect=git_output):
                self.assertEqual(set(audit.shared_source(root, 'baseline')), set(names))
                (root / 'scp.c').write_bytes(baseline['scp.c'].replace(
                    b'CWND_WIN_MAX', b'SCP_AIMD_SSTHRESH_INIT'))
                with self.assertRaisesRegex(RuntimeError, 'scp.c'):
                    audit.shared_source(root, 'baseline')

    def test_transport_audit_rejects_changed_feedback_and_window(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'events.csv'
            fields, rows, summary = self.native_trace(sizes=(1430,))
            self.write_trace(path, fields, rows)
            self.assertTrue(audit.transport_events(path, summary, 1430)['core_window_per_ack_verified'])
            for index, field, value in ((1, 't_ms', 151), (2, 'ack', 1441),
                                        (4, 't_ms', 201), (5, 'cwnd_bytes', 4000),
                                        (0, 'planned_q32_ms', (100 << 32) + 1),
                                        (0, 'next_slot_q32_ms', (101 << 32) + 1),
                                        (0, 'gain_q16', 65535)):
                changed = [dict(row) for row in rows]
                changed[index][field] = value
                self.write_trace(path, fields, changed)
                with self.assertRaises(RuntimeError):
                    audit.transport_events(path, summary, 1430)
            self.write_trace(path, fields, [row for row in rows if row['event'] != 'send' or row['direction'] != 1])
            with self.assertRaises(RuntimeError):
                audit.transport_events(path, summary, 1430)

    def test_native_summary_validation(self):
        settings = json.loads((ROOT / 'scp_fixed_experiment.json').read_text())
        settings['bytes'] = 2 * settings['mss_bytes'] + 17
        gain = next(g for g in settings['gains'] if g['id'] == 'critical')
        summary = dict(engine='scp_virtual', status='complete', g=gain['value'], ca_update=settings['ca_update'],
                       mode=settings['mode'], gain_policy='fixed_q16',
                       gain_q16=round(gain['value'] * 65536),
                       transport='fixed_delay', rate_mbps=0, delay_ms=50, loss_ppm=0, seed=1,
                       received_bytes=settings['bytes'], accepted_bytes=settings['bytes'],
                       new_bytes=settings['bytes'], checksum_ok=True, checksum_a='same', checksum_b='same',
                       mss_bytes=1430, data_payload_packet_counts={'1430': 2, '17': 1},
                       new_packets=3, nonfinal_short_packets=0, last_new_payload_bytes=17,
                       feedback_count=3, feedback_min_ms=100, feedback_max_ms=100,
                       observer_errors=0, dropped=0, retrans_packets=0,
                       **{field: settings[field] for field in
                          ('cwnd_limit_bytes', 'send_window_bytes', 'recv_window_bytes', 'application_backlog_bytes')})
        summary.update(tick_scale=settings.get('tick_scale', 1), initial_cwnd_bytes=1430, initial_ssthresh_bytes=settings['initial_ssthresh_bytes'])
        runner.validate(summary, settings, gain)
        for changes in (dict(nonfinal_short_packets=1), dict(last_new_payload_bytes=1430),
                        dict(data_payload_packet_counts={'1430': 1, '17': 1, '715': 2}),
                        dict(feedback_count=2), dict(feedback_max_ms=102),
                        dict(gain_q16=1), dict(gain_policy='native_aimd'),
                        dict(ca_update='byte-floor'), dict(application_backlog_bytes=1), dict(loss_ppm=1), dict(tick_scale=7), dict(rate_mbps=1000), dict(transport='serialized_link')):
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                runner.validate(dict(summary, **changes), settings, gain)
        with self.assertRaises(RuntimeError):
            runner.validate_settings(dict(settings, loss_ppm=1000))
        native = dict(id='native', value=None, expression='native AIMD gain', policy='native_aimd')
        runner.validate(dict(summary, gain_q16=None, gain_policy='native_aimd', g=None), settings, native)

    def test_microsecond_tick_audit_preserves_packet_timing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'events.csv'
            fields, original, summary = self.native_trace()
            fields = [name.replace('planned_q32_ms', 'planned_q32_tick').replace('next_slot_q32_ms', 'next_slot_q32_tick') for name in fields]
            fields += ['t_tick', 'echo_send_tick']
            rows = []
            for raw in original:
                row = dict(raw)
                row['t_tick'] = row['t_ms'] * 1000 + 371
                row['echo_send_tick'] = row['echo_send_ms'] * 1000 + 371
                row['t_ms'] = row['t_tick'] / 1000
                row['echo_send_ms'] = row['echo_send_tick'] / 1000
                for key in ('planned_q32', 'next_slot_q32'):
                    value = row.pop(key + '_ms')
                    row[key + '_tick'] = int(value) * 1000 + (371 << 32) if value != '' else ''
                rows.append(row)
            summary.update(tick_scale=1000, first_data_tx_tick=100371, data_acked_tick=300371,
                           first_data_tx_ms=100.371, data_acked_ms=300.371)
            self.write_trace(path, fields, rows)
            result = audit.transport_events(path, summary, 1430)
            self.assertEqual(result['tick_scale'], 1000)
            self.assertEqual(result['first_window_growth_elapsed_ms'], 100)
            rows[1]['t_tick'] += 1
            rows[1]['t_ms'] = rows[1]['t_tick'] / 1000
            self.write_trace(path, fields, rows)
            with self.assertRaisesRegex(RuntimeError, 'Forward DATA'):
                audit.transport_events(path, summary, 1430)

    def test_microsecond_endpoints_and_histogram_resolution(self):
        slots = [1] + [0] * 98 + [1]
        metrics = analysis.packet_metrics(slots, 100.123, 199.999, 100, 100)
        self.assertAlmostEqual(metrics['span_ms'], 99.876)
        self.assertAlmostEqual(metrics['gap_mean_ms'], 99.876)

    def test_ca_audit_uses_the_second_ack_and_integer_rounding(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'events.csv'
            fields, rows, summary = self.native_trace()
            fields.remove('cwnd_cnt')
            for row in rows:
                row.pop('cwnd_cnt')
            summary.pop('ca_update')
            self.write_trace(path, fields, rows)
            result = audit.transport_events(path, summary, 1430)
            self.assertEqual(result['ca_update'], 'byte-floor')
            self.assertEqual(result['max_integer_update_error_bytes'], 0)
            with gzip.open(path.with_name('ack_updates.csv.gz'), 'rt') as source:
                updates = list(csv.DictReader(source))
            self.assertEqual(int(updates[1]['observed_cwnd_bytes']), 3575)
            rows[-1]['cwnd_bytes'] = 4290
            self.write_trace(path, fields, rows)
            with self.assertRaises(RuntimeError):
                audit.transport_events(path, summary, 1430)

    def test_ca_remainder_and_zero_increment_are_audited(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'events.csv'
            fields, rows, summary = self.native_trace(sizes=(1430,) * 8 + (1,), ca_update='byte-remainder')
            self.write_trace(path, fields, rows)
            result = audit.transport_events(path, summary, 1430)
            self.assertTrue(result['remainder_per_ack_verified'])
            self.assertEqual(rows[-1]['cwnd_bytes'], rows[-2]['cwnd_bytes'])
            self.assertEqual(rows[-1]['cwnd_cnt'], 3716)
            rows[-1]['cwnd_cnt'] += 1
            self.write_trace(path, fields, rows)
            with self.assertRaisesRegex(RuntimeError, 'remainder'):
                audit.transport_events(path, summary, 1430)
            rows[-1]['cwnd_cnt'] -= 1
            rows[-2]['cwnd_cnt'] += 1
            self.write_trace(path, fields, rows)
            with self.assertRaisesRegex(RuntimeError, 'remainder'):
                audit.transport_events(path, summary, 1430)

    def test_phase_gain_and_short_tail_audits(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'events.csv'
            for mode in ('always_slow_start', 'always_congestion_avoidance'):
                for policy in ('fixed_q16', 'native_aimd'):
                    for sizes in ((1430, 1430), (1430, 17)):
                        with self.subTest(mode=mode, policy=policy, sizes=sizes):
                            fields, rows, summary = self.native_trace(mode, policy, sizes)
                            self.write_trace(path, fields, rows)
                            result = audit.transport_events(path, summary, 1430)
                            self.assertTrue(result['next_slot_rule_verified'])
                            self.assertTrue(result['receiver_ack_generation_verified'])
                            self.assertEqual(result['assigned_packets'], 2)
                            with gzip.open(path.with_name('ack_updates.csv.gz'), 'rt') as source:
                                updates = list(csv.DictReader(source))
                            expected = 4290 if mode == 'always_slow_start' else 3575
                            if sizes[-1] == 1430:
                                self.assertEqual(int(updates[-1]['observed_cwnd_bytes']), expected)
                            if mode == 'always_slow_start':
                                self.assertEqual(int(updates[-1]['paper_ack_reference_bytes']), 1430 + sum(sizes))
                                self.assertEqual(float(updates[-1]['paper_error_bytes']), 0)

    def test_hand_calculated_metrics(self):
        slots = [0] * 100
        slots[0], slots[50], slots[99] = 2, 1, 1
        result = analysis.packet_metrics(slots, 400, 499, 100, 100)
        self.assertEqual(result['span_ms'], 99)
        self.assertEqual(result['hhi'], .375)
        self.assertEqual(result['burst_fraction'], .5)
        self.assertAlmostEqual(result['zero_gap_fraction'], 1 / 3)
        self.assertEqual(result['gap_mean_ms'], 33)
        self.assertAlmostEqual(result['gap_cv'], (4901 / 3 - 1089) ** .5 / 33)
        slots = [4] + [0] * 99
        result = analysis.packet_metrics(slots, 400, 400, 100, 100)
        self.assertEqual(result['hhi'], 1)
        self.assertEqual(result['zero_gap_fraction'], 1)
        self.assertIsNone(result['gap_cv'])
        self.assertFalse(analysis.packet_metrics([0] * 100, None, None, 100, 100)['dispersion_valid'])
        self.assertFalse(analysis.packet_metrics([1] + [0] * 99, 400, 400, 100, 100)['dispersion_valid'])
        with self.assertRaises(ValueError):
            analysis.packet_metrics([0] * 99, None, None, 100, 100)

    def test_past_samples(self):
        import numpy as np
        values, ages = analysis.past_samples(np.array([10, 20]), np.array([100, 200]), np.array([9, 10, 19, 20, 21]))
        self.assertTrue(np.isnan(values[0]) and np.isnan(values[-1]))
        self.assertEqual(list(values[1:4]), [100, 100, 200])
        self.assertEqual(list(ages[1:4]), [0, 9, 0])

    def test_campaign_and_invalid_inputs(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        root.mkdir(parents=True, exist_ok=True)
        manifest = dict(g_hundredths=[100, 200], repeats=2, rtt_ms=100, rate_mbps=0,
                        bytes=7, mss_bytes=1430, loss_ppm=0,
                        backend='scp_virtual_fixed_delay', mode='always_slow_start', seed=1)
        (root / 'experiment.json').write_text(json.dumps(manifest), encoding='utf-8')
        for g in manifest['g_hundredths']:
            for repeat in (1, 2):
                folder = root / 'runs' / f'g{g}-r{repeat}'
                folder.mkdir(parents=True, exist_ok=True)
                compressed = g == 200 and repeat == 2
                suffix = '.gz' if compressed else ''
                case = dict(manifest, case_id=folder.name, g_hundredths=g, repeat=repeat,
                            stream_fd=1, state_log='state.jsonl' + suffix,
                            packet_rtt_file='packet_rtt.csv' + suffix)
                (folder / 'config.json').write_text(json.dumps(case), encoding='utf-8')
                summary = dict(status='complete', received_bytes=7, checksum_ok=True, dropped=0,
                               first_data_tx_ms=400, data_acked_ms=650, last_observed_ms=650,
                               last_data_tx_ms=610 if g == 100 else 630)
                (folder / 'summary.json').write_text(json.dumps(summary), encoding='utf-8')
                opener = gzip.open if compressed else open
                with opener(folder / case['state_log'], 'wt', encoding='utf-8') as output:
                    for t, multiplier in [(399, 1), (400, 1), (500, g / 50), (600, g / 25), (650, g / 25)]:
                        output.write(json.dumps(dict(type='ss', stream_fd=1, t=t, cwnd=int(1430 * multiplier))) + '\n')
                tx = [400, 400, 450, 499, 500, 599, 610] if g == 100 else [400, 400, 400, 400, 575, 610, 630]
                with opener(folder / case['packet_rtt_file'], 'wt', encoding='utf-8', newline='') as output:
                    fields = ['rtt_index', 'scope', 'packet_count', 'payload_bytes', 'first_tx_ms', 'last_tx_ms', 'first_seq', 'last_seq', 'slot_counts']
                    writer = csv.DictWriter(output, fieldnames=fields)
                    writer.writeheader()
                    for k in range(3):
                        for scope in ('all', 'new', 'retrans'):
                            packets = [(seq, t) for seq, t in enumerate(tx) if (t - 400) // 100 == k and scope != 'retrans']
                            slots = [0] * 100
                            for seq, t in packets:
                                slots[(t - 400) % 100] += 1
                            writer.writerow(dict(rtt_index=k, scope=scope, packet_count=len(packets), payload_bytes=len(packets),
                                first_tx_ms=packets[0][1] if packets else '', last_tx_ms=packets[-1][1] if packets else '',
                                first_seq=packets[0][0] if packets else '', last_seq=packets[-1][0] if packets else '', slot_counts=json.dumps(slots)))
        out = root / 'analysis'
        analysis.analyze(root, out, False)
        with (out / 'cwnd_by_rtt.csv').open(encoding='utf-8-sig') as source:
            wide = list(csv.DictReader(source))
        self.assertEqual(float(wide[1]['g_1.00']), 2860)
        self.assertEqual(float(wide[1]['g_2.00']), 5720)
        self.assertEqual(len(wide), 3)
        with (out / 'packet_dispersion_summary.csv').open(encoding='utf-8-sig') as source:
            rows = list(csv.DictReader(source))
        first = next(row for row in rows if row['g'] == '1.0' and row['rtt_index'] == '0' and row['scope'] == 'all')
        self.assertEqual(float(first['hhi_median']), .375)
        self.assertTrue(all(row['hhi_median'] == '' for row in rows if row['rtt_index'] == '2'))
        self.assertTrue(all(row['valid_dispersion_runs'] == '0' for row in rows
                            if row['rtt_index'] == '2' or row['scope'] == 'retrans'))
        with (out / 'packet_dispersion_by_rtt.csv').open(encoding='utf-8-sig') as source:
            bins = list(csv.DictReader(source))
        self.assertTrue(all(row['analysis_eligible'] == 'False' for row in bins
                            if row['contains_final_send'] == 'True' or row['packet_count'] == '0'))
        self.assertTrue((out / 'cwnd_all_g.png').stat().st_size > 10000)
        summary_path = root / 'runs/g100-r1/summary.json'
        summary = json.loads(summary_path.read_text())
        summary['checksum_ok'] = False
        summary_path.write_text(json.dumps(summary))
        with self.assertRaisesRegex(ValueError, 'integrity'):
            analysis.analyze(root, out, False)
        summary['checksum_ok'] = True
        summary_path.write_text(json.dumps(summary))
        manifest['repeats'] = 3
        (root / 'experiment.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'Incomplete'):
            analysis.analyze(root, out, False)
        analysis.analyze(root, root / 'partial-analysis', True)
        critical = 1.3862943611198906
        manifest.pop('g_hundredths')
        manifest.update(repeats=2, gains=[dict(id='critical', value=critical), dict(id='g2', value=2)])
        (root / 'experiment.json').write_text(json.dumps(manifest))
        for path in (root / 'runs').glob('*/config.json'):
            case = json.loads(path.read_text())
            old = case.pop('g_hundredths')
            case.update(gain_id='critical' if old == 100 else 'g2', g=critical if old == 100 else 2)
            path.write_text(json.dumps(case))
        analysis.analyze(root, root / 'critical-analysis', False)
        with (root / 'critical-analysis/cwnd_by_rtt.csv').open(encoding='utf-8-sig') as source:
            wide = list(csv.DictReader(source))
        self.assertEqual(float(wide[1][f'g_{critical:.17g}']), 2860)
        self.assertNotIn('g_1.39', wide[1])

    def test_full_final_send_bin_is_excluded_from_dispersion(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'packets.csv'
            fields = ['rtt_index', 'scope', 'packet_count', 'payload_bytes', 'first_tx_ms',
                      'last_tx_ms', 'first_seq', 'last_seq', 'slot_counts']
            rows = []
            for k in range(3):
                for scope in ('all', 'new', 'retrans'):
                    present = k < 2 and scope != 'retrans'
                    slots = [0] * 100
                    if present:
                        slots[0] = slots[50] = 1
                    rows.append(dict(rtt_index=k, scope=scope, packet_count=sum(slots),
                                     payload_bytes=sum(slots), first_tx_ms=100 + k * 100 if present else '',
                                     last_tx_ms=150 + k * 100 if present else '', first_seq=1 if present else '',
                                     last_seq=2 if present else '', slot_counts=json.dumps(slots)))
            self.write_trace(path, fields, rows)
            case = dict(case_id='sample', g=1, repeat=1, rtt_ms=100)
            summary = dict(status='complete', first_data_tx_ms=100, data_acked_ms=350, last_data_tx_ms=250)
            bins = analysis.load_packets(path, case, summary)
            final = next(row for row in bins if row['rtt_index'] == 1 and row['scope'] == 'all')
            self.assertTrue(final['full_rtt'] and final['dispersion_valid'] and final['contains_final_send'])
            self.assertFalse(final['analysis_eligible'])
            aggregated = analysis.summarize_packets(bins)
            self.assertTrue(all(row['valid_dispersion_runs'] == 0 and row['span_rtt_median'] is None
                                for row in aggregated if row['rtt_index'] > 0 or row['scope'] == 'retrans'))


if __name__ == '__main__':
    unittest.main()
