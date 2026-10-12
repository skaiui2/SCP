#!/usr/bin/env python3
"""Archive full curves separately from compact inputs needed for aggregation."""
import argparse
import json
from pathlib import Path
import tarfile


def package(root, name, output):
    output.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output / f'{name}-full.tar.gz', 'w:gz') as archive:
        for path in sorted(root.rglob('*')):
            if path.is_file() and path.name != 'runner.lock':
                archive.add(path, arcname=f'{name}/{path.relative_to(root).as_posix()}', recursive=False)
    inputs = [root / filename for filename in ('experiment.json', 'frozen.json', 'shard.json')]
    for config in sorted((root / 'runs').glob('*/config.json')):
        inputs.extend([config, config.parent / 'summary.json', config.parent / 'artifact_integrity.json'])
        case = json.loads(config.read_text(encoding='utf-8'))
        inputs.extend(config.parent / case[field] for field in ('state_log', 'packet_rtt_file'))
    with tarfile.open(output / f'{name}-inputs.tar.gz', 'w:gz') as archive:
        for path in inputs:
            if path.exists():
                archive.add(path, arcname=f'{name}/{path.relative_to(root).as_posix()}', recursive=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('--name', required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    package(args.root.resolve(), args.name, args.out.resolve())
