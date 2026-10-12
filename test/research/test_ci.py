import csv
import gzip
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import run_campaign
from merge_ci import merge, finalize, extract_archive
from package_ci import package
from run_campaign import digest

COMMIT = 'a' * 40
URL = 'https://example.invalid/actions/runs/test'


def fixture(root):
    manifest = dict(g_hundredths=[100, 200], repeats=2, expected_runs=4, bytes=7,
                    rtt_ms=100, rate_mbps=1000, loss_ppm=0, backend='virtual',
                    mode='always_slow_start', seed=1)
    cases = [(g, r) for r in (1, 2) for g in (100, 200)]
    for index in (0, 1):
        shard = root / f'shard-{index}'
        shard.mkdir(parents=True)
        (shard / 'experiment.json').write_text(json.dumps(manifest), encoding='utf-8')
        frozen = dict(commit=COMMIT, binary_sha256='frozen-binary',
                      manifest_sha256=digest(shard / 'experiment.json'))
        (shard / 'frozen.json').write_text(json.dumps(frozen), encoding='utf-8')
        names = []
        for position, (gain, repeat) in enumerate(cases):
            if position % 2 != index:
                continue
            name = f'g{gain:04d}-r{repeat:02d}'
            names.append(name)
            folder = shard / 'runs' / name
            folder.mkdir(parents=True)
            config = dict(manifest, case_id=name, g_hundredths=gain, repeat=repeat, stream_fd=1,
                          state_log='state.jsonl.gz', packet_rtt_file='packet.csv.gz',
                          commit=COMMIT, binary_sha256='frozen-binary')
            (folder / 'config.json').write_text(json.dumps(config), encoding='utf-8')
            summary = dict(status='complete', first_data_tx_ms=400, data_acked_ms=650,
                           last_observed_ms=650, received_bytes=7, checksum_ok=True, dropped=0,
                           max_cwnd=5720, retrans_packets=0)
            (folder / 'summary.json').write_text(json.dumps(summary), encoding='utf-8')
            with gzip.open(folder / 'state.jsonl.gz', 'wt', encoding='utf-8') as output:
                for t, window in [(399, 1430), (400, 1430), (500, 2860), (600, 5720), (650, 5720)]:
                    output.write(json.dumps(dict(type='ss', stream_fd=1, t=t, cwnd=window)) + '\n')
            with gzip.open(folder / 'packet.csv.gz', 'wt', encoding='utf-8', newline='') as output:
                fields = ['rtt_index', 'scope', 'packet_count', 'payload_bytes', 'first_tx_ms',
                          'last_tx_ms', 'first_seq', 'last_seq', 'slot_counts']
                writer = csv.DictWriter(output, fieldnames=fields)
                writer.writeheader()
                for k in range(3):
                    for scope in ('all', 'new', 'retrans'):
                        n = 7 if k == 0 and scope != 'retrans' else 0
                        writer.writerow(dict(rtt_index=k, scope=scope, packet_count=n, payload_bytes=n,
                            first_tx_ms=400 if n else '', last_tx_ms=400 if n else '',
                            first_seq=0 if n else '', last_seq=6 if n else '', slot_counts=json.dumps([n] + [0]*99)))
            integrity = {}
            for field in ('state_log', 'packet_rtt_file'):
                integrity[field] = dict(compressed_sha256=digest(folder / config[field]),
                                        uncompressed_sha256='same-test-trace')
            (folder / 'artifact_integrity.json').write_text(json.dumps(integrity), encoding='utf-8')
            curves = folder / 'curves'
            curves.mkdir()
            (curves / 'archive-routing-fixture.txt').write_text('Full archive only', encoding='utf-8')
        metadata = dict(frozen, shard_index=index, shard_count=2, expected_cases=names)
        (shard / 'shard.json').write_text(json.dumps(metadata), encoding='utf-8')


class CITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.inputs = self.root / 'inputs'
        fixture(self.inputs)

    def test_merge_and_stable_index(self):
        output = self.root / 'merged'
        result = merge(self.inputs, output, 2, COMMIT, URL)
        self.assertEqual(result['recorded'], 4)
        first_index = digest(output / 'data-index.json')
        finalize(output, COMMIT, URL)
        self.assertEqual(digest(output / 'data-index.json'), first_index)
        (output / 'normal-check-full.json').write_text('{"ctest_return_code":0}', encoding='utf-8')
        finalize(output, COMMIT, URL)
        index = json.loads((output / 'data-index.json').read_text(encoding='utf-8'))
        self.assertIn('normal-check-full.json', index['files'])
        self.assertNotIn('tag-message.txt', index['files'])
        for name, sha in index['files'].items():
            self.assertEqual(digest(output / name), sha)

    def test_corrupt_and_wrong_owner_rejected(self):
        shard = self.inputs / 'shard-0'
        packet = next(shard.glob('runs/*/packet.csv.gz'))
        packet.write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'corrupt'):
            merge(self.inputs, self.root / 'bad', 2, COMMIT, URL)
        path = shard / 'shard.json'
        metadata = json.loads(path.read_text(encoding='utf-8'))
        metadata['expected_cases'].append('g0200-r01')
        path.write_text(json.dumps(metadata), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'matrix'):
            merge(self.inputs, self.root / 'owner', 2, COMMIT, URL)

    def test_missing_shard_is_explicitly_partial(self):
        other = self.root / 'partial-inputs'
        other.mkdir()
        (self.inputs / 'shard-0').rename(other / 'shard-0')
        with self.assertRaisesRegex(ValueError, 'Missing'):
            merge(other, self.root / 'strict', 2, COMMIT, URL)
        output = self.root / 'partial-report'
        result = merge(other, output, 2, COMMIT, URL, allow_incomplete=True)
        self.assertEqual(result['recorded'], 2)
        audit = json.loads((output / 'analysis/analysis_audit.json').read_text(encoding='utf-8'))
        self.assertEqual(len(audit['missing_cases']), 2)

    def test_archive_routing_and_path_safety(self):
        archives = self.root / 'archives'
        package(self.inputs / 'shard-0', 'shard-0', archives)
        with tarfile.open(archives / 'shard-0-full.tar.gz') as full:
            self.assertTrue(any('curves/' in name for name in full.getnames()))
        with tarfile.open(archives / 'shard-0-inputs.tar.gz') as compact:
            self.assertFalse(any('curves/' in name for name in compact.getnames()))
        destination = self.root / 'extracted'
        destination.mkdir()
        extract_archive(archives / 'shard-0-inputs.tar.gz', destination)
        self.assertTrue((destination / 'shard-0/shard.json').exists())
        unsafe = archives / 'unsafe.tar.gz'
        with tarfile.open(unsafe, 'w:gz') as archive:
            member = tarfile.TarInfo('../escape')
            member.size = 1
            archive.addfile(member, io.BytesIO(b'x'))
        with self.assertRaisesRegex(ValueError, 'Unsafe'):
            extract_archive(unsafe, destination)

    def test_shard_reuses_seed_without_aggregating_or_pushing(self):
        seed = self.inputs / 'shard-0'
        source = self.root / 'source'
        source.mkdir()
        (source / 'experiment.json').write_bytes((seed / 'experiment.json').read_bytes())
        for name in ('SCP-pacing-experiment-plan.md', 'analyze_pacing_research.py'):
            (source / name).write_text('Controller fixture', encoding='utf-8')
        binary = self.root / 'scp_virtual'
        binary.write_bytes(b'cached-only-test')
        frozen = json.loads((seed / 'frozen.json').read_text(encoding='utf-8'))
        frozen['binary_sha256'] = digest(binary)
        (seed / 'frozen.json').write_text(json.dumps(frozen), encoding='utf-8')
        for folder in (seed / 'runs').iterdir():
            config = json.loads((folder / 'config.json').read_text(encoding='utf-8'))
            config['binary_sha256'] = digest(binary)
            (folder / 'config.json').write_text(json.dumps(config), encoding='utf-8')
            summary = json.loads((folder / 'summary.json').read_text(encoding='utf-8'))
            summary['transfer_wall_seconds'] = 0
            (folder / 'summary.json').write_text(json.dumps(summary), encoding='utf-8')
        output = self.root / 'runner-shard'
        argv = ['run_campaign.py', '--root', str(output), '--exe', str(binary),
                '--phase', 'shard', '--ci', '--shard-index', '0', '--shard-count', '2',
                '--seed-root', str(seed)]
        def fake_git(*arguments, **kwargs):
            return '' if arguments[0] == 'status' else COMMIT
        with patch.object(run_campaign, 'HERE', source), patch.object(run_campaign, 'git', fake_git), \
                patch('sys.argv', argv), \
                patch.object(run_campaign.platform, 'platform', return_value='cached-shard-test'), \
                patch.object(run_campaign.analysis, 'analyze', side_effect=AssertionError('Shard must not aggregate')), \
                patch.object(run_campaign.subprocess, 'run', side_effect=AssertionError('Cached shard must not execute or push')):
            run_campaign.main()
        progress = json.loads((output / 'progress.json').read_text(encoding='utf-8'))
        self.assertEqual(progress['state'], 'shard_complete')
        self.assertEqual(progress['complete'], 2)
        self.assertFalse((output / 'runner.lock').exists())
        self.assertFalse((output / 'tag-message.txt').exists())


if __name__ == '__main__':
    unittest.main()
