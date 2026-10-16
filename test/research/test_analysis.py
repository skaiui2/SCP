import csv
import gzip
import importlib.util
import json
from pathlib import Path
import unittest
import tempfile
import run_scp_fixed_feedback as runner
import audit

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('analysis', ROOT / 'analyze_pacing_research.py')
analysis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analysis)


class AnalysisTests(unittest.TestCase):
    def test_transport_audit_rejects_changed_feedback_and_window(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'events.csv'
            header = 'event,direction,t_ms,flags,seq,ack,payload_bytes,echo_send_ms,cwnd_bytes,planned_q32_ms\n'
            trace = ('send,0,100,1,10,0,1430,100,1430,429496729600\n'
                     'arrival,1,200,2,0,1440,0,100,1430,\n'
                     'input_returned,1,200,2,0,1440,0,100,2860,\n')
            summary = dict(data_acked_ms=200, received_bytes=1430, new_packets=1,
                           first_data_tx_ms=100, gain_reciprocal_q32=4294967296)
            path.write_text(header + trace)
            self.assertTrue(audit.transport_events(path, summary, 1430)['core_window_per_ack_verified'])
            for changed in (trace.replace('arrival,1,200', 'arrival,1,201'),
                            trace.replace('100,2860', '100,4000'),
                            trace.replace('429496729600', '433791696896')):
                path.write_text(header + changed)
                with self.assertRaises(RuntimeError):
                    audit.transport_events(path, summary, 1430)

    def test_native_summary_validation(self):
        settings = json.loads((ROOT / 'scp_fixed_experiment.json').read_text())
        settings['bytes'] = 2 * settings['mss_bytes'] + 17
        gain = next(g for g in settings['gains'] if g['id'] == 'critical')
        summary = dict(engine='scp_virtual', status='complete', g=gain['value'],
                       gain_reciprocal_q32=round(2**32 / gain['value']),
                       transport='fixed_delay', rate_mbps=0, delay_ms=50, loss_ppm=0, seed=1,
                       received_bytes=settings['bytes'], accepted_bytes=settings['bytes'],
                       new_bytes=settings['bytes'], checksum_ok=True, checksum_a='same', checksum_b='same',
                       mss_bytes=1430, data_payload_packet_counts={'1430': 2, '17': 1},
                       new_packets=3, nonfinal_short_packets=0, last_new_payload_bytes=17,
                       feedback_count=3, feedback_min_ms=100, feedback_max_ms=100,
                       observer_errors=0, dropped=0, retrans_packets=0,
                       **{field: settings[field] for field in
                          ('cwnd_limit_bytes', 'send_window_bytes', 'recv_window_bytes')})
        runner.validate(summary, settings, gain)
        for changes in (dict(nonfinal_short_packets=1), dict(last_new_payload_bytes=1430),
                        dict(data_payload_packet_counts={'1430': 1, '17': 1, '715': 2}),
                        dict(feedback_count=2), dict(feedback_max_ms=102),
                        dict(gain_reciprocal_q32=1), dict(loss_ppm=1), dict(rate_mbps=1000), dict(transport='serialized_link')):
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                runner.validate(dict(summary, **changes), settings, gain)
        with self.assertRaises(RuntimeError):
            runner.validate_settings(dict(settings, loss_ppm=1000))

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
        manifest = dict(g_hundredths=[100, 200], repeats=2, rtt_ms=100, rate_mbps=1000,
                        bytes=7, loss_ppm=0, backend='virtual', mode='always_slow_start', seed=1)
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
                               first_data_tx_ms=400, data_acked_ms=650, last_observed_ms=650)
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


if __name__ == '__main__':
    unittest.main()
