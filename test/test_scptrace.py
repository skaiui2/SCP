"""Hand-written log fixtures check the offline exporter, not the protocol stack."""
import csv
import gzip
import json
from pathlib import Path
import tempfile
import unittest

import scptrace


class ExportTests(unittest.TestCase):
    def fixture(self, run, initial=10, scale=1000, base=10, compressed=False):
        mss, q32, gain = 1430, 1 << 32, 98304
        window = initial * mss
        initial_slot = (100 + initial) * scale * q32
        updated = window + mss // initial
        start = 200 * scale * q32
        gap = mss * 100 * scale * ((65536 << 32) // gain) // updated
        last_plan = start + gap
        next_slot = last_plan + 17 * 100 * scale * ((65536 << 32) // gain) // updated
        last_send = last_plan >> 32
        rows = []

        def record(now, event, direction, flags, seq=0, ack=0, size=0, echo=0,
                   plan='', slot=initial_slot, cwnd=window):
            rows.append(dict(event=event, direction=direction, t_ms=now/scale,
                t_tick=now, fd=2 if direction == 0 else 1, flags=flags,
                seq=seq & scptrace.MASK, ack=ack & scptrace.MASK, payload_bytes=size,
                echo_send_ms=echo/scale, echo_send_tick=echo, cwnd_bytes=cwnd,
                cwnd_cnt=0, flight_bytes=0, planned_q32_tick=plan,
                gain_q16=gain, next_slot_q32_tick=slot))

        record(0, 'send', 0, 8, seq=base-1, slot=0)
        for number in range(initial):
            sent = (100 + number) * scale
            seq = base + number * mss
            record(sent, 'send', 0, 1, seq=seq, size=mss, echo=sent, plan=sent*q32)
            record(sent+50*scale, 'arrival', 0, 1, seq=seq, size=mss, echo=sent)
            record(sent+50*scale, 'send', 1, 2, ack=seq+mss, echo=sent)
            record(sent+50*scale, 'input_returned', 0, 1, seq=seq, size=mss, echo=sent)
            now = sent + 100 * scale
            record(now, 'arrival', 1, 2, ack=seq+mss, echo=sent,
                   slot=initial_slot if not number else next_slot, cwnd=window if not number else updated)
            if not number:
                record(now, 'send', 0, 1, seq=base+initial*mss, size=mss,
                       echo=now, plan=start, slot=next_slot, cwnd=updated)
            record(now, 'input_returned', 1, 2, ack=seq+mss, echo=sent, slot=next_slot, cwnd=updated)
        record(last_send, 'send', 0, 1, seq=base+(initial+1)*mss, size=17,
               echo=last_send, plan=last_plan, slot=next_slot, cwnd=updated)
        for sent, size, seq in ((200*scale, mss, base+initial*mss),
                                (last_send, 17, base+(initial+1)*mss)):
            record(sent+50*scale, 'arrival', 0, 1, seq=seq, size=size, echo=sent, slot=next_slot, cwnd=updated)
            record(sent+50*scale, 'send', 1, 2, ack=seq+size, echo=sent, slot=next_slot, cwnd=updated)
            record(sent+50*scale, 'input_returned', 0, 1, seq=seq, size=size, echo=sent, slot=next_slot, cwnd=updated)
            record(sent+100*scale, 'arrival', 1, 2, ack=seq+size, echo=sent, slot=next_slot, cwnd=updated)
            record(sent+100*scale, 'input_returned', 1, 2, ack=seq+size, echo=sent, slot=next_slot, cwnd=updated)
        rows.sort(key=lambda row: row['t_tick'])
        config = dict(case_id='fixture-r01', mss_bytes=mss, g=1.5, g_effective=1.5,
                      repeat=1, binary_sha256='fixture', tick_scale=scale,
                      initial_cwnd_bytes=window, state_log='run.log.gz', commit='fixture')
        summary = dict(status='complete', checksum_ok=True, retrans_packets=0, dropped=0,
                       received_bytes=(initial+1)*mss+17, new_packets=initial+2,
                       first_data_tx_ms=100, first_data_tx_tick=100*scale,
                       data_acked_ms=(last_send+100*scale)/scale,
                       data_acked_tick=last_send+100*scale, tick_scale=scale,
                       initial_cwnd_bytes=window, mode='always_congestion_avoidance', gain_policy='fixed_q16')
        for name, value in (('config', config), ('summary', summary), ('execution_audit',
                            dict(next_slot_rule_verified=True, receiver_ack_generation_verified=True))):
            (run / f'{name}.json').write_text(json.dumps(value), encoding='utf-8')
        suffix = '.gz' if compressed else ''
        opener = gzip.open if compressed else open
        with opener(run / ('network_events.csv'+suffix), 'wt', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        with opener(run / ('run.log'+suffix), 'wt', encoding='utf-8') as stream:
            stream.write('Protocol log preamble\n')
            stream.write(json.dumps(dict(type='ss', stream_fd=2))+'\n')
            stream.write(json.dumps(dict(type='other'))+'\n')
            for elapsed, increase, flight in ((0, 0, window), (1, 1, window), (1, 2, 17), (220, 3, 0)):
                stream.write(json.dumps(dict(type='ss', stream_fd=1, t=100+elapsed,
                    t_tick=(100+elapsed)*scale, tick_scale=scale,
                    cwnd=window+increase, flight=flight, ssthresh=window))+'\n')
        return rows, summary

    def commands(self, path):
        color = None
        result = []
        lines = iter(path.read_text(encoding='ascii').splitlines())
        for line in lines:
            if line in ('title', 'xlabel', 'ylabel'):
                next(lines)
            elif line in ('blue', 'green', 'red', 'purple', 'gray20'):
                color = line
            elif line.startswith(('line ', 'dot ', 'ltext ')):
                parts = line.split()
                result.append((color, parts, next(lines) if parts[0] == 'ltext' else ''))
        return result

    def test_complete_trains_ticks_wrap_and_state_order(self):
        for initial in (1, 10):
            for scale in (1, 1000):
                with self.subTest(initial=initial, scale=scale), tempfile.TemporaryDirectory() as directory:
                    run = Path(directory)
                    rows, summary = self.fixture(run, initial, scale, base=0xfffffff0)
                    result = scptrace.export_case(run)
                    self.assertEqual(result, dict(data_packets=initial+2, ack_packets=initial+2,
                                                  state_samples=4, control_packets=1))
                    tsg = self.commands(run/'xpl/a2b_tsg.xpl')
                    data = [parts for color, parts, _ in tsg if color == 'blue' and parts[0] == 'line']
                    self.assertEqual(len(data), initial+2)
                    self.assertEqual([int(parts[2]) for parts in data], [1430*n for n in range(initial+2)])
                    self.assertEqual(int(data[-1][4]), summary['received_bytes'])
                    observed = [row for row in rows if row['direction'] == 0 and row['event'] == 'send' and row['flags'] == 1]
                    self.assertEqual([float(parts[1]) for parts in data],
                                     [(row['t_tick']-100*scale)/scale/1000 for row in observed])
                    self.assertNotEqual(float(data[-1][1]),
                        (observed[-1]['planned_q32_tick']-100*scale*scptrace.Q32)/(scptrace.Q32*scale*1000))
                    self.assertTrue(any(text.startswith('G0 ') for _, _, text in tsg))
                    self.assertFalse(any(text.startswith('G-1 ') for _, _, text in tsg))
                    ack_ends = [float(parts[-1]) for color, parts, _ in tsg if color == 'green' and
                                (parts[0] == 'dot' or parts[1] == parts[3])]
                    self.assertEqual(ack_ends, [1430*n for n in range(1, initial+2)]+[summary['received_bytes']])
                    window = [parts for color, parts, _ in self.commands(run/'xpl/a2b_window.xpl') if color == 'blue']
                    self.assertEqual([float(parts[-1]) for parts in window], [1430*initial+n for n in range(4)])
                    self.assertEqual(window[2][1], window[2][3])
                    for path in (run/'xpl').glob('*.xpl'):
                        self.assertTrue(path.read_text().startswith('double double\n'))
                        self.assertTrue(path.read_text().endswith('go\n'))

    def test_gzip_and_plain_outputs_match(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, compressed in (('plain', False), ('gzip', True)):
                run = root/name
                run.mkdir()
                self.fixture(run, compressed=compressed)
                scptrace.export_case(run)
            for name in ('a2b_tsg.xpl', 'a2b_window.xpl'):
                self.assertEqual((root/'plain/xpl'/name).read_bytes(), (root/'gzip/xpl'/name).read_bytes())

    def test_incomplete_input_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            _, summary = self.fixture(run)
            summary['new_packets'] += 1
            (run/'summary.json').write_text(json.dumps(summary))
            with self.assertRaises(ValueError):
                scptrace.export_case(run)
            self.assertFalse(list(run.glob('xpl/*.xpl')))

    def test_legacy_clock_and_native_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            self.fixture(run, scale=1)
            path = run/'network_events.csv'
            with path.open(newline='') as stream:
                rows = list(csv.DictReader(stream))
            for row in rows:
                row.pop('t_tick')
                row.pop('echo_send_tick')
                row['planned_q32_ms'] = row.pop('planned_q32_tick')
                row['next_slot_q32_ms'] = row.pop('next_slot_q32_tick')
            with path.open('w', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            config = json.loads((run/'config.json').read_text())
            config['g_effective'] = None
            (run/'config.json').write_text(json.dumps(config))
            summary = json.loads((run/'summary.json').read_text())
            summary['gain_policy'] = 'native_aimd'
            (run/'summary.json').write_text(json.dumps(summary))
            self.assertEqual(scptrace.export_case(run)['data_packets'], 12)
            self.assertIn('native_aimd', (run/'xpl/a2b_tsg.xpl').read_text())

    def test_failed_window_export_does_not_publish_partial_files(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            self.fixture(run)
            (run/'run.log').write_text('No sender state samples\n')
            with self.assertRaisesRegex(ValueError, 'No sender state'):
                scptrace.export_case(run)
            self.assertFalse(list((run/'xpl').iterdir()))


if __name__ == '__main__':
    unittest.main()
