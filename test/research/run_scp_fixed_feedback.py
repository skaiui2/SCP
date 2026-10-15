"""Launch the compiled SCP stack and analyze its own logs."""
import argparse
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys

import analyze_pacing_research as analysis

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def compress_verified(path):
    target = path.with_suffix(path.suffix + '.gz')
    original = digest(path)
    with path.open('rb') as source, target.open('wb') as output:
        with gzip.GzipFile(filename='', mode='wb', fileobj=output, mtime=0) as archive:
            shutil.copyfileobj(source, archive)
    check = hashlib.sha256()
    with gzip.open(target, 'rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            check.update(block)
    if check.hexdigest() != original:
        raise RuntimeError(f'Compression verification failed: {path}')
    path.unlink()
    return dict(compressed_sha256=digest(target), uncompressed_sha256=original)


def freeze(out, executable):
    files = [ROOT / 'scp.c', ROOT / 'scp.h', ROOT / 'test/CMakeLists.txt',
             ROOT / 'test/plot.py', ROOT / 'test/analyze_scp.py']
    for directory in ('lib', 'test/common', 'test/virtual', 'test/research'):
        files.extend(p for p in (ROOT / directory).rglob('*')
                     if p.is_file() and p.suffix in ('.c', '.h', '.py', '.json')
                     and '__pycache__' not in p.parts)
    source_files = {p.relative_to(ROOT).as_posix(): digest(p) for p in sorted(set(files))}
    diff = subprocess.check_output(['git', 'diff', '--binary', 'HEAD'], cwd=ROOT)
    (out / 'source.diff').write_bytes(diff)
    provenance = dict(
        commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        binary_sha256=digest(executable), source_files_sha256=source_files,
        source_identity_sha256=hashlib.sha256(json.dumps(source_files, sort_keys=True).encode()).hexdigest(),
        source_diff_sha256=hashlib.sha256(diff).hexdigest(), engine='scp_virtual')
    for name in ('compile_commands.json', 'CMakeCache.txt'):
        path = executable.parent / name
        if path.exists():
            shutil.copy2(path, out / name)
    atomic_json(out / 'frozen.json', provenance)
    return provenance


def validate_settings(settings):
    required = dict(rtt_ms=100, rate_mbps=0, loss_ppm=0, seed=1,
                    link_serialization=False, queue_delay=False,
                    backend='scp_virtual_fixed_delay', mode='always_slow_start',
                    mss_bytes=1430, payload_policy='full_mss_except_final')
    if any(settings.get(field) != value for field, value in required.items()):
        raise RuntimeError('Research requires fixed 100 ms feedback, zero loss and full MSS packets')


def validate(summary, settings, gain):
    validate_settings(settings)
    if summary.get('engine') != 'scp_virtual' or summary['status'] != 'complete':
        raise RuntimeError('The compiled SCP transfer did not complete')
    required = dict(transport='fixed_delay', rate_mbps=0, delay_ms=50, loss_ppm=0, seed=1)
    if any(summary.get(field) != value for field, value in required.items()):
        raise RuntimeError('Actual C transport differs from the research conditions')
    for field in ('received_bytes', 'accepted_bytes', 'new_bytes'):
        if summary[field] != settings['bytes']:
            raise RuntimeError(f'Payload count differs: {field}')
    if not summary['checksum_ok'] or summary['checksum_a'] != summary['checksum_b']:
        raise RuntimeError('Payload checksum mismatch')
    mss = settings['mss_bytes']
    full, tail = divmod(settings['bytes'], mss)
    expected_sizes = {str(mss): full} if full else {}
    if tail:
        expected_sizes[str(tail)] = 1
    if (summary['mss_bytes'] != mss or summary['data_payload_packet_counts'] != expected_sizes or
            summary['new_packets'] != full + bool(tail) or summary['nonfinal_short_packets'] or
            summary['last_new_payload_bytes'] != (tail or mss)):
        raise RuntimeError('Observed packet sizes differ from full MSS packets plus one final tail')
    if summary['feedback_count'] != summary['new_packets']:
        raise RuntimeError('Not every forward DATA packet has an observed feedback ACK')
    if summary['feedback_count'] == 0 or summary['feedback_min_ms'] != 100 or summary['feedback_max_ms'] != 100:
        raise RuntimeError('Observed DATA-to-ACK feedback differs from 100 ms')
    if summary['observer_errors'] or summary['dropped'] or summary['retrans_packets']:
        raise RuntimeError('Observer error, loss or retransmission in the zero-loss case')
    if not math.isclose(summary['g'], gain['value'], rel_tol=0, abs_tol=1e-14):
        raise RuntimeError('Actual C gain differs from requested gain')
    for field in ('cwnd_limit_bytes', 'send_window_bytes', 'recv_window_bytes'):
        if summary[field] != settings[field]:
            raise RuntimeError(f'Compiled limit differs from configuration: {field}')


def run_case(out, executable, gain, repeat, settings, provenance):
    name = f'{gain["id"]}-r{repeat:02d}'
    folder = out / 'runs' / name
    folder.mkdir(parents=True, exist_ok=False)
    argument = '2ln2' if gain['id'] == 'critical' else repr(gain['value'])
    command = [str(executable), '--bytes', str(settings['bytes']), '--mbps', '0',
               '--delay-ms', '50', '--loss-ppm', '0', '--seed', '1', '--g', argument]
    config = dict(settings, case_id=name, gain_id=gain['id'], g=gain['value'],
                  g_expression=gain['expression'], gain_argument=argument, repeat=repeat,
                  stream_fd=1, state_log='run.log.gz', packet_rtt_file='packet_rtt.csv.gz',
                  command=command, commit=provenance['commit'], engine='scp_virtual',
                  binary_sha256=provenance['binary_sha256'],
                  source_identity_sha256=provenance['source_identity_sha256'])
    config.pop('gains')
    atomic_json(folder / 'config.json', config)
    try:
        with (folder / 'run.log').open('wb') as output:
            run = subprocess.run(command, cwd=folder, stdout=output,
                                 stderr=subprocess.STDOUT, timeout=1200)
        summary = json.loads((folder / 'research_summary.json').read_text())
        summary.update(command=command, return_code=run.returncode)
        atomic_json(folder / 'summary.json', summary)
        if run.returncode:
            raise RuntimeError(f'C executable exited with {run.returncode}')
        validate(summary, settings, gain)
        if digest(executable) != provenance['binary_sha256']:
            raise RuntimeError('Executable changed during the sweep')
        states = []
        with (folder / 'run.log').open(encoding='utf-8') as source:
            for line in source:
                if line.startswith('{'):
                    row = json.loads(line)
                    if row.get('type') == 'ss' and row.get('stream_fd') == 1:
                        states.append(row)
        origin = summary['first_data_tx_ms']
        initial = next(row for row in states if row['t'] == origin)
        growth = next(row for row in states if row['t'] >= origin and row['cwnd'] > initial['cwnd'])
        if growth['t'] - origin != 100 or growth['srtt'] != 100:
            raise RuntimeError('First core window update is not at +100 ms')
        if any(row['srtt'] not in (0, 100) for row in states):
            raise RuntimeError('Observed core SRTT differs from 100 ms')
        summary.update(first_window_growth_ms=growth['t'], initial_cwnd_bytes=initial['cwnd'],
                       final_state_time_ms=states[-1]['t'],
                       cwnd_cap_reached=summary['max_cwnd'] >= settings['cwnd_limit_bytes'])
        atomic_json(folder / 'summary.json', summary)
        analysis.load_packets(folder / 'packet_rtt.csv', config, summary)
        with (folder / 'plot.log').open('wb') as output:
            subprocess.run([sys.executable, str(ROOT / 'test/plot.py'), str(folder / 'run.log'),
                            '--out', str(folder / 'curves'), '--max-points', '10000'],
                           stdout=output, stderr=subprocess.STDOUT, check=True, timeout=600)
        integrity = {key: compress_verified(folder / filename) for key, filename in (
            ('state_log', 'run.log'), ('packet_rtt_file', 'packet_rtt.csv'),
            ('network_events', 'network_events.csv'))}
        atomic_json(folder / 'artifact_integrity.json', integrity)
        print(f'{name}: C SCP PASS; {summary["received_bytes"]} bytes; feedback 100 ms', flush=True)
    except Exception as error:
        atomic_json(folder / 'failure.json', dict(error=str(error), command=command))
        raise


def merge(inputs, out, settings):
    out.mkdir(parents=True, exist_ok=True)
    expected = {(g['id'], r) for g in settings['gains'] for r in range(1, settings['repeats'] + 1)}
    gain_map = {g['id']: g for g in settings['gains']}
    seen, sources, binaries, summaries = set(), set(), set(), []
    for path in sorted(inputs.glob('**/runs/*/config.json')):
        config = json.loads(path.read_text())
        key = config['gain_id'], config['repeat']
        if key not in expected or key in seen:
            raise ValueError('Unplanned or duplicate C transfer')
        seen.add(key)
        sources.add((config['commit'], config['source_identity_sha256']))
        binaries.add(config['binary_sha256'])
        gain = gain_map[config['gain_id']]
        if config['g'] != gain['value'] or config['g_expression'] != gain['expression']:
            raise ValueError('Incorrect gain identity')
        for field in ('rtt_ms', 'bytes', 'loss_ppm', 'backend', 'mode', 'seed', 'rate_mbps',
                      'link_serialization', 'queue_delay', 'mss_bytes', 'payload_policy'):
            if config[field] != settings[field]:
                raise ValueError(f'Mixed experimental condition: {field}')
        summary = json.loads((path.parent / 'summary.json').read_text())
        validate(summary, settings, gain)
        integrity = json.loads((path.parent / 'artifact_integrity.json').read_text())
        for field, filename in (('state_log', config['state_log']),
                                ('packet_rtt_file', config['packet_rtt_file']),
                                ('network_events', 'network_events.csv.gz')):
            if digest(path.parent / filename) != integrity[field]['compressed_sha256']:
                raise ValueError(f'Corrupt protocol observation: {filename}')
        shutil.copytree(path.parent, out / 'runs' / config['case_id'])
        summaries.append(dict(summary, repeat=config['repeat']))
    if seen != expected or len(sources) != 1 or len(binaries) != 1:
        raise ValueError('Incomplete or mixed C SCP dataset')
    atomic_json(out / 'experiment.json', settings)
    analysis.analyze(out, out / 'analysis', False)
    fields = ['g', 'repeat', 'engine', 'received_bytes', 'checksum_ok', 'feedback_count',
              'feedback_min_ms', 'feedback_max_ms', 'max_cwnd', 'cwnd_cap_reached',
              'data_acked_ms', 'first_data_tx_ms', 'retrans_packets']
    analysis.write_csv(out / 'analysis/transfer_summary.csv',
                       [{field: row.get(field) for field in fields} for row in summaries], fields)
    sizes = [dict(g=row['g'], repeat=row['repeat'], payload_bytes=int(size), packet_count=count)
             for row in summaries for size, count in row['data_payload_packet_counts'].items()]
    analysis.write_csv(out / 'analysis/packet_payload_sizes.csv', sizes,
                       ['g', 'repeat', 'payload_bytes', 'packet_count'])
    print(json.dumps(dict(engine='scp_virtual', complete=len(seen), commit=next(iter(sources))[0],
                          binary_sha256=next(iter(binaries)))), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--exe', type=Path)
    parser.add_argument('--gain-id')
    parser.add_argument('--repeats', type=int)
    parser.add_argument('--bytes', type=int)
    parser.add_argument('--merge', type=Path)
    args = parser.parse_args()
    settings = json.loads((HERE / 'scp_fixed_experiment.json').read_text())
    validate_settings(settings)
    if args.merge:
        merge(args.merge, args.out.resolve(), settings)
        return
    if not args.exe:
        parser.error('--exe is required')
    if args.bytes is not None:
        settings['bytes'] = args.bytes
    if args.repeats is not None:
        settings['repeats'] = args.repeats
    selected = [g for g in settings['gains'] if args.gain_id is None or g['id'] == args.gain_id]
    if not selected or settings['repeats'] < 1 or not 0 < settings['bytes'] <= 2**30:
        parser.error('Invalid gain, repetition count or payload size')
    settings['gains'] = selected
    settings['expected_runs'] = len(selected) * settings['repeats']
    executable, out = args.exe.resolve(), args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    provenance = freeze(out, executable)
    atomic_json(out / 'experiment.json', settings)
    failures = 0
    for gain in selected:
        for repeat in range(1, settings['repeats'] + 1):
            try:
                run_case(out, executable, gain, repeat, settings, provenance)
            except Exception as error:
                failures += 1
                print(f'{gain["id"]} r{repeat}: {error}', file=sys.stderr, flush=True)
    if failures:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
