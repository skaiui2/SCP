"""Verify and expand the committed native RTT tables; no experiment is run."""
import argparse
import gzip
import hashlib
import json
import lzma
from pathlib import Path
import shutil


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    source = root / 'data'
    for name, expected in json.loads((root / 'checksums.json').read_text()).items():
        actual = hashlib.sha256((source / name).read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError('Committed data checksum mismatch: ' + name)
    args.out.mkdir(parents=True, exist_ok=False)
    files = 0
    for path in sorted(source.rglob('*')):
        if not path.is_file():
            continue
        target = args.out / path.relative_to(source)
        compressed = path.suffix in ('.gz', '.xz')
        if compressed:
            target = target.with_suffix('')
        target.parent.mkdir(parents=True, exist_ok=True)
        if compressed:
            opener = gzip.open if path.suffix == '.gz' else lzma.open
            with opener(path, 'rb') as inp, target.open('wb') as out:
                shutil.copyfileobj(inp, out)
        else:
            shutil.copy2(path, target)
        files += 1
    print(json.dumps(dict(output=str(args.out), verified_and_expanded_files=files)))


if __name__ == '__main__':
    main()
