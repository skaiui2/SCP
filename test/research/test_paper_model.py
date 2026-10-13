import unittest
import json
import tempfile
from pathlib import Path
import numpy as np
from paper_model import simulate, closed_form, run_case, merge, CRITICAL


class ModelTests(unittest.TestCase):
    def test_initial_packets_and_ack_before_sample(self):
        sent, windows, final = simulate(1, 2)
        np.testing.assert_allclose(sent, [0,1,1.5,2],rtol=0,atol=1e-14)
        np.testing.assert_array_equal(windows,[1,2,2,3])
        self.assertEqual(final,3)

    def test_closed_form_against_event_simulator(self):
        for g in [1,1.1,CRITICAL-.05,CRITICAL,CRITICAL+.05,2,4,8]:
            sent, _, _=simulate(g,8)
            predicted,_=closed_form(g,len(sent))
            np.testing.assert_allclose(sent,predicted,rtol=0,atol=1e-11)

    def test_critical_and_high_gain_boundaries(self):
        for g in [CRITICAL,2,4,8]:
            sent, _, _=simulate(g,8)
            actual=1+np.searchsorted(sent+1,np.arange(1,9),side='right')
            np.testing.assert_array_equal(actual,1+2**np.arange(8))

    def test_same_window_boundaries_different_send_spans(self):
        widths=[]
        for g in [2,4,8]:
            sent, _, _=simulate(g,8)
            group=sent[(sent>=7)&(sent<8)]
            widths.append(group[-1]-group[0])
        self.assertGreater(widths[0],widths[1])
        self.assertGreater(widths[1],widths[2])

    def test_invalid_gain_rejected(self):
        for g in [0,.5,float('nan'),float('inf')]:
            with self.assertRaises(ValueError):simulate(g,4)

    def test_complete_outputs_and_missing_repeats(self):
        gains=[dict(id='critical',value=CRITICAL,expression='2*ln(2)'),dict(id='g2',value=2,expression='2')]
        settings=dict(model='paper_append_scheduler_fixed_feedback',rtt_ms=100,
            duration_rtts=4,packet_payload_bytes=1430,sample_interval_ms=1,repeats=2,gains=gains)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for gain in gains:
                for repeat in (1,2):run_case(root/'runs'/f'{gain["id"]}-r{repeat:02d}',gain,repeat,settings)
            merge(root,root/'report',settings)
            audit=json.loads((root/'report/provenance.json').read_text())
            self.assertEqual(audit['complete_cases'],4)
            self.assertTrue(audit['repeated_traces_identical'])
            (root/'runs/g2-r02/summary.json').unlink()
            with self.assertRaisesRegex(ValueError,'Missing'):merge(root,root/'partial',settings)


if __name__=='__main__':unittest.main()
