import csv
import gzip
import importlib.util
import json
from pathlib import Path
import unittest
import tempfile

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('analysis', ROOT / 'analyze_pacing_research.py')
analysis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analysis)


class AnalysisTests(unittest.TestCase):
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
        (root / 'experiment.json').write_text(json.dumps(dict(manifest, repeats=2)))


if __name__ == '__main__':
    unittest.main()
