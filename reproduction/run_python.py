"""Run this branch's 22 gains x 3 using the reviewed Python packet model."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=4)
    args = parser.parse_args()
    if args.workers < 1:
        parser.error('workers must be positive')
    root = Path(__file__).resolve().parent
    settings = json.loads((root / 'data/experiment.json').read_text())
    phase = 'ss' if settings['mode'] == 'always_slow_start' else 'ca'
    gains = [row['value'] for row in settings['gains']]
    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=False)
    (args.out / 'source').mkdir()
    (args.out / 'logs').mkdir()
    model = args.out / 'source/pacing.py'
    shutil.copy2(root.parent / 'test/models/pacing.py', model)
    manifest = dict(backend='independent_python_packet_model', native_scp_execution=False,
                    model_sha256=hashlib.sha256(model.read_bytes()).hexdigest(),
                    modes=[phase], gains=gains, repeats=3, requested_bytes=1 << 30,
                    reservation_rule='max_now_next_slot_then_reserve_then_add_gap',
                    tick_ms=0.001, rtt_ms=100, python=sys.version)
    manifest_path = args.out / 'manifest.json'
    manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')

    def run(index, gain):
        folder = args.out / phase / f'g{index:02d}'
        command = [sys.executable, str(model), '--mode', phase, '--gain', repr(gain),
                   '--bytes', str(1 << 30), '--repeats', '3', '--out', str(folder)]
        with (args.out / 'logs' / f'g{index:02d}.log').open('w') as log:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
        return index, gain

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        jobs = [pool.submit(run, index, gain) for index, gain in enumerate(gains, 1)]
        for job in as_completed(jobs):
            index, gain = job.result()
            print(f'{phase} g{index:02d}: g={gain:.17g}; 3 runs complete', flush=True)
    manifest['completed_transfers'] = len(gains) * 3
    manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')


if __name__ == '__main__':
    main()
