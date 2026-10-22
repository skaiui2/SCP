"""Launch the compiled SCP stack and analyze its own logs."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import analyze_pacing_research as analysis
import audit
import comparison

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
             ROOT / 'test/plot.py', ROOT / 'test/analyze_scp.py',
             ROOT / '.github/workflows/pacing-research.yml']
    for directory in ('lib', 'test/common', 'test/virtual', 'test/research'):
        files.extend(p for p in (ROOT / directory).rglob('*')
                     if p.is_file() and p.suffix in ('.c', '.h', '.py', '.json', '.cmake')
                     and '__pycache__' not in p.parts)
    shared = audit.shared_source(ROOT, json.loads((HERE / 'scp_fixed_experiment.json').read_text())['baseline_sha'])
    commands = json.loads((executable.parent / 'compile_commands.json').read_text())
    compiled = {Path(entry['file']).resolve() for entry in commands
                if 'scp_research.dir' in entry.get('command', entry.get('output', ''))}
    required = {ROOT / name for name in ('scp.c', 'test/virtual/vnet.c', 'test/common/transfer.c')}
    if not required <= compiled:
        raise RuntimeError('Build did not compile the shared SCP core/network/transfer into scp_research')
    source_files = {p.relative_to(ROOT).as_posix(): digest(p) for p in sorted(set(files))}
    diff = subprocess.check_output(['git', 'diff', '--binary', 'HEAD'], cwd=ROOT)
    (out / 'source.diff').write_bytes(diff)
    provenance = dict(
        commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        binary_sha256=digest(executable), source_files_sha256=source_files,
        source_identity_sha256=hashlib.sha256(json.dumps(source_files, sort_keys=True).encode()).hexdigest(),
        source_diff_sha256=hashlib.sha256(diff).hexdigest(), engine='scp_virtual',
        checked_baseline_files=shared,
        baseline_core_delta='none; shared core/network/transfer are identical to baseline')
    for name in ('compile_commands.json', 'CMakeCache.txt', 'build_commands.json', 'build.log'):
        path = executable.parent / name
        if path.exists():
            shutil.copy2(path, out / name)
    atomic_json(out / 'frozen.json', provenance)
    shutil.copy2(executable, out / executable.name)
    return provenance


def validate_settings(settings):
    modes = {'always_slow_start': 2147483647, 'always_congestion_avoidance': 1430}
    required = dict(rtt_ms=100, rate_mbps=0, loss_ppm=0, seed=1,
                    link_serialization=False, queue_delay=False,
                    backend='scp_virtual_fixed_delay', initial_cwnd_bytes=1430,
                    mss_bytes=1430, payload_policy='full_mss_except_final')
    if (settings.get('mode') not in modes or
            settings.get('initial_ssthresh_bytes') != modes[settings['mode']] or
            any(settings.get(field) != value for field, value in required.items())):
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
    policy = gain.get('policy', 'fixed_q16')
    gain_q16 = round(gain['value'] * 65536) if policy == 'fixed_q16' else None
    if (summary['mode'] != settings['mode'] or summary['gain_policy'] != policy or
            summary['gain_q16'] != gain_q16):
        raise RuntimeError('Actual C phase or gain policy differs from requested configuration')
    for field in ('cwnd_limit_bytes', 'send_window_bytes', 'recv_window_bytes',
                  'initial_cwnd_bytes', 'initial_ssthresh_bytes'):
        if summary[field] != settings[field]:
            raise RuntimeError(f'Compiled limit differs from configuration: {field}')


def run_case(out, executable, gain, repeat, settings, provenance):
    name = f'{gain["id"]}-r{repeat:02d}'
    folder = out / 'runs' / name
    folder.mkdir(parents=True, exist_ok=False)
    policy = gain.get('policy', 'fixed_q16')
    gain_q16 = round(gain['value'] * 65536) if policy == 'fixed_q16' else None
    effective = gain_q16 / 65536 if gain_q16 is not None else None
    command = [str(executable), '--bytes', str(settings['bytes']), '--mbps', '0',
               '--delay-ms', '50', '--loss-ppm', '0', '--seed', '1']
    config = dict(settings, case_id=name, gain_id=gain['id'], g=gain['value'],
                  g_expression=gain['expression'], gain_policy=policy, gain_q16=gain_q16,
                  g_effective=effective, repeat=repeat,
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
        summary.update(command=command, return_code=run.returncode, g=gain['value'],
                       g_effective=effective)
        atomic_json(folder / 'summary.json', summary)
        if run.returncode:
            raise RuntimeError(f'C executable exited with {run.returncode}')
        validate(summary, settings, gain)
        atomic_json(folder / 'execution_audit.json', audit.transport_events(
            folder / 'network_events.csv', summary, settings['mss_bytes']))
        if digest(executable) != provenance['binary_sha256']:
            raise RuntimeError('Executable changed during the sweep')
        origin = summary['first_data_tx_ms']
        initial = growth = last = None
        with (folder / 'run.log').open(encoding='utf-8') as source:
            for line in source:
                if line.startswith('{'):
                    row = json.loads(line)
                    if row.get('type') == 'ss' and row.get('stream_fd') == 1:
                        if row['srtt'] not in (0, 100):
                            raise RuntimeError('Observed core SRTT differs from 100 ms')
                        if row['t'] >= origin:
                            in_ss = row['cwnd'] < row['ssthresh']
                            if (row['ssthresh'] != settings['initial_ssthresh_bytes'] or
                                    in_ss != (settings['mode'] == 'always_slow_start')):
                                raise RuntimeError('Observed core left the configured congestion-control phase')
                        if row['t'] == origin:
                            initial = row
                        if initial and growth is None and row['cwnd'] > initial['cwnd']:
                            growth = row
                        last = row
        if not growth or growth['t'] - origin != 100 or growth['srtt'] != 100:
            raise RuntimeError('First core window update is not at +100 ms')
        summary.update(first_window_growth_ms=growth['t'], initial_cwnd_bytes=initial['cwnd'],
                       final_state_time_ms=last['t'],
                       cwnd_cap_reached=summary['max_cwnd'] >= settings['cwnd_limit_bytes'])
        atomic_json(folder / 'summary.json', summary)
        if policy == 'fixed_q16':
            analysis.load_packets(folder / 'packet_rtt.csv', config, summary)
        with (folder / 'plot.log').open('wb') as output:
            subprocess.run([sys.executable, str(ROOT / 'test/plot.py'), str(folder / 'run.log'),
                            '--out', str(folder / 'curves'), '--max-points', '10000'],
                           stdout=output, stderr=subprocess.STDOUT, check=True, timeout=600)
        integrity = {key: compress_verified(folder / filename) for key, filename in (
            ('state_log', 'run.log'), ('packet_rtt_file', 'packet_rtt.csv'),
            ('network_events', 'network_events.csv'))}
        atomic_json(folder / 'artifact_integrity.json', integrity)
        atomic_json(folder / 'derived_integrity.json', {name: digest(folder / name) for name in
                    ('ack_updates.csv.gz', 'execution_audit.json', 'summary.json', 'config.json')})
        print(f'{name}: C SCP PASS; {summary["received_bytes"]} bytes; feedback 100 ms', flush=True)
    except Exception as error:
        atomic_json(folder / 'failure.json', dict(error=str(error), command=command))
        raise


def merge(inputs, out, settings):
    out.mkdir(parents=True, exist_ok=True)
    expected = {(g['id'], r) for g in settings['gains'] for r in range(1, settings['repeats'] + 1)}
    gain_map = {g['id']: g for g in settings['gains']}
    seen, sources, binaries, summaries = set(), set(), {}, []
    for path in sorted(inputs.glob('**/runs/*/config.json')):
        config = json.loads(path.read_text())
        if config.get('gain_policy') == 'native_aimd':
            continue
        key = config['gain_id'], config['repeat']
        if key not in expected or key in seen:
            raise ValueError('Unplanned or duplicate C transfer')
        seen.add(key)
        sources.add((config['commit'], config['source_identity_sha256']))
        binaries.setdefault(config['gain_id'], set()).add(config['binary_sha256'])
        gain = gain_map[config['gain_id']]
        if (config['g'] != gain['value'] or config['g_expression'] != gain['expression'] or
                config.get('gain_policy') != 'fixed_q16' or
                config.get('gain_q16') != round(gain['value'] * 65536) or
                config.get('g_effective') != round(gain['value'] * 65536) / 65536):
            raise ValueError('Incorrect gain identity')
        for field in ('rtt_ms', 'bytes', 'loss_ppm', 'backend', 'mode', 'seed', 'rate_mbps',
                      'initial_cwnd_bytes', 'initial_ssthresh_bytes',
                      'link_serialization', 'queue_delay', 'mss_bytes', 'payload_policy'):
            if config[field] != settings[field]:
                raise ValueError(f'Mixed experimental condition: {field}')
        summary = json.loads((path.parent / 'summary.json').read_text())
        validate(summary, settings, gain)
        integrity = json.loads((path.parent / 'artifact_integrity.json').read_text())
        derived = json.loads((path.parent / 'derived_integrity.json').read_text())
        for name, expected_digest in derived.items():
            if digest(path.parent / name) != expected_digest:
                raise ValueError(f'Corrupt case evidence: {name}')
        for field, filename in (('state_log', config['state_log']),
                                ('packet_rtt_file', config['packet_rtt_file']),
                                ('network_events', 'network_events.csv.gz')):
            if digest(path.parent / filename) != integrity[field]['compressed_sha256']:
                raise ValueError(f'Corrupt protocol observation: {filename}')
        shutil.copytree(path.parent, out / 'runs' / config['case_id'])
        summaries.append(dict(summary, repeat=config['repeat']))
    if seen != expected or len(sources) != 1 or any(len(v) != 1 for v in binaries.values()):
        raise ValueError('Incomplete or mixed C SCP dataset')
    atomic_json(out / 'experiment.json', settings)
    analysis.analyze(out, out / 'analysis', False)
    comparison.compare(out, out / 'analysis')
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
                          binary_sha256_by_gain={g: next(iter(v)) for g, v in binaries.items()})), flush=True)


def build(folder, gain, cmake, settings):
    gain_q16 = round(gain['value'] * 65536) if gain.get('policy', 'fixed_q16') == 'fixed_q16' else 0
    folder.mkdir(parents=True, exist_ok=True)
    commands = [[cmake, '-S', str(ROOT / 'test'), '-B', str(folder),
                 '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON',
                 '-DSCP_RESEARCH=ON', '-DSCP_TEST_LOG_INTERVAL_MS=1',
                 f'-DSCP_RESEARCH_GAIN_Q16={gain_q16}',
                 f'-DSCP_RESEARCH_CA={int(settings["mode"] == "always_congestion_avoidance")}',
                 f'-DPython3_EXECUTABLE={sys.executable}'],
                [cmake, '--build', str(folder), '--target', 'scp_research', '--parallel', '2']]
    atomic_json(folder / 'build_commands.json', commands)
    with (folder / 'build.log').open('w') as log:
        for command in commands:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
    return folder / ('scp_research.exe' if os.name == 'nt' else 'scp_research')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--exe', type=Path)
    parser.add_argument('--build-dir', type=Path)
    parser.add_argument('--cmake', default='cmake')
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
    if not args.exe and not args.build_dir:
        parser.error('--build-dir or --exe is required')
    if args.bytes is not None:
        settings['bytes'] = args.bytes
    if args.repeats is not None:
        settings['repeats'] = args.repeats
    selected = [g for g in settings['gains'] if args.gain_id is None or g['id'] == args.gain_id]
    if args.gain_id == 'native':
        selected = [dict(id='native', value=None, expression='native AIMD gain', policy='native_aimd')]
    if not selected or settings['repeats'] < 1 or not 0 < settings['bytes'] <= 2**30:
        parser.error('Invalid gain, repetition count or payload size')
    settings['gains'] = selected
    settings['expected_runs'] = len(selected) * settings['repeats']
    if args.exe and len(selected) != 1:
        parser.error('A prebuilt executable must identify exactly one gain')
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    atomic_json(out / 'experiment.json', settings)
    failures = 0
    deterministic = []
    for gain in selected:
        provenance_dir = out / 'builds' / gain['id']
        provenance_dir.mkdir(parents=True, exist_ok=True)
        try:
            executable = args.exe.resolve() if args.exe else build(
                args.build_dir.resolve() / gain['id'], gain, args.cmake, settings)
        except Exception as error:
            folder = args.build_dir.resolve() / gain['id']
            for name in ('build.log', 'build_commands.json', 'CMakeCache.txt'):
                if (folder / name).exists():
                    shutil.copy2(folder / name, provenance_dir / name)
            atomic_json(provenance_dir / 'failure.json', dict(error=str(error)))
            raise
        provenance = freeze(provenance_dir, executable)
        fingerprints = []
        for repeat in range(1, settings['repeats'] + 1):
            try:
                run_case(out, executable, gain, repeat, settings, provenance)
                folder = out / 'runs' / f'{gain["id"]}-r{repeat:02d}'
                evidence = json.loads((folder / 'artifact_integrity.json').read_text())
                fingerprints.append(tuple(evidence[key]['uncompressed_sha256'] for key in
                                          ('state_log', 'packet_rtt_file', 'network_events')))
            except Exception as error:
                failures += 1
                print(f'{gain["id"]} r{repeat}: {error}', file=sys.stderr, flush=True)
        deterministic.append(dict(gain_id=gain['id'], repeats=len(fingerprints),
                                  identical=len(set(fingerprints)) == 1))
    atomic_json(out / 'deterministic_repeat_check.json', deterministic)
    if failures:
        raise SystemExit(1)
    if any(not row['identical'] for row in deterministic):
        raise RuntimeError('Deterministic repeats differ; inspect raw protocol observations')


if __name__ == '__main__':
    main()
