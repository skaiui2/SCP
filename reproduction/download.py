"""Download and hash-check the archived native SCP evidence (standard library only)."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import stat
import urllib.request
import zipfile


def digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=Path('reproduction/data'))
    parser.add_argument('--archive-dir', type=Path, help='Use already downloaded ZIPs; do not copy or download')
    args = parser.parse_args()
    manifest = json.loads(Path(__file__).with_name('manifest.json').read_text())
    args.out.mkdir(parents=True, exist_ok=True)
    for asset in manifest['assets']:
        archive = (args.archive_dir or args.out) / asset['name']
        if not archive.exists():
            if args.archive_dir:
                raise FileNotFoundError(archive)
            print('Downloading ' + asset['name'], flush=True)
            partial = archive.with_suffix('.part')
            request = urllib.request.Request(asset['url'], headers={'User-Agent': 'SCP-reproduction'})
            with urllib.request.urlopen(request, timeout=120) as source, partial.open('wb') as target:
                shutil.copyfileobj(source, target, 1024 * 1024)
            if partial.stat().st_size != asset['bytes'] or digest(partial) != asset['sha256']:
                raise ValueError('Downloaded archive failed SHA-256: ' + str(partial))
            partial.replace(archive)
        if archive.stat().st_size != asset['bytes'] or digest(archive) != asset['sha256']:
            raise ValueError('Archive failed SHA-256: ' + str(archive))
        folder = args.out / archive.stem
        if folder.exists() and any(folder.iterdir()):
            raise FileExistsError(f'{folder} is not empty; use a fresh output directory')
        folder.mkdir(exist_ok=True)
        with zipfile.ZipFile(archive) as source:
            for member in source.infolist():
                target = (folder / member.filename).resolve()
                if not target.is_relative_to(folder.resolve()) or stat.S_ISLNK(member.external_attr >> 16):
                    raise ValueError('Unsafe ZIP entry: ' + member.filename)
            source.extractall(folder)
        print('Verified and extracted ' + asset['name'], flush=True)
    print('Dataset: ' + str(args.out / 'scp-fixed-results'))


if __name__ == '__main__':
    main()
