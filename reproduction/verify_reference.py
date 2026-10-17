"""Compare regenerated measurements with archived tables, independent of CSV row order."""
import argparse
import csv
import hashlib
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    here = Path(__file__).resolve().parent
    manifest = json.loads((here / 'manifest.json').read_text())
    for name, expected in manifest['reference_files'].items():
        reference = here / 'reference' / name
        if hashlib.sha256(reference.read_bytes()).hexdigest() != expected:
            raise ValueError('Reference file has changed: ' + name)
        generated = args.out / name
        if name.endswith('.csv'):
            def read(path):
                with path.open(encoding='utf-8-sig', newline='') as source:
                    reader = csv.DictReader(source)
                    fields = sorted(reader.fieldnames)
                    return fields, sorted(tuple(row[key] for key in fields) for row in reader)
            equal = read(reference) == read(generated)
        else:
            equal = json.loads(reference.read_text()) == json.loads(generated.read_text())
        if not equal:
            raise ValueError('Generated values differ from the archived experiment: ' + name)
        print('PASS ' + name)
    print('All six reference files match. PDF metadata/renderer bytes are not numerical evidence.')


if __name__ == '__main__':
    main()
