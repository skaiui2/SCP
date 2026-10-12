#!/usr/bin/env python3
"""Disposable slow-start g campaign. Run preflight, then full with the same frozen binary."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
import gzip
import hashlib
import importlib.util
import json
import os
import platform
from pathlib import Path
import shutil
from statistics import median
import subprocess
import sys
import threading
import time

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parents[1]
BASELINE = '3f5b10923a0854f23678a5d7da427fb6aecc9052'
TAG = 'research-slowstart-g-100ms-20261012'
GIB = 2 ** 30
LOCK = threading.Lock()
PLOT_LOCK = threading.Semaphore(1)
spec = importlib.util.spec_from_file_location('pacing_analysis', HERE / 'analyze_pacing_research.py')
analysis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analysis)


def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def git(*arguments, cwd=SOURCE):
    return subprocess.check_output(['git', *arguments], cwd=cwd, text=True).strip()


def compress_verified(path):
    target = path.with_suffix(path.suffix + '.gz')
    temporary = target.with_suffix('.gz.tmp')
    original = digest(path)
    with path.open('rb') as source, temporary.open('wb') as raw:
        with gzip.GzipFile(filename='', mode='wb', fileobj=raw, compresslevel=6, mtime=0) as zipped:
            shutil.copyfileobj(source, zipped, 1024 * 1024)
    h = hashlib.sha256()
    with gzip.open(temporary, 'rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            h.update(chunk)
    if h.hexdigest() != original:
        raise RuntimeError(f'Compression verification failed: {path}')
    os.replace(temporary, target)
    path.unlink()
    return dict(uncompressed_sha256=original, compressed_sha256=digest(target), bytes=target.stat().st_size)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--exe', type=Path, required=True)
    parser.add_argument('--phase', choices=['preflight', 'full', 'shard'], required=True)
    parser.add_argument('--normal-repo', type=Path)
    parser.add_argument('--normal-build', type=Path)
    parser.add_argument('--ctest', type=Path)
    parser.add_argument('--jobs', type=int, default=2)
    parser.add_argument('--ci', action='store_true', help='Normal baseline checks are separate CI jobs')
    parser.add_argument('--shard-index', type=int)
    parser.add_argument('--shard-count', type=int, default=16)
    parser.add_argument('--seed-root', type=Path, help='Reuse matching preflight cases in assigned shards')
    args = parser.parse_args()
    if not 1 <= args.jobs <= 2:
        parser.error('Use one or two transfer workers on this machine')
    if not args.ci and not all((args.normal_repo, args.normal_build, args.ctest)):
        parser.error('Local execution needs --normal-repo, --normal-build and --ctest')
    if args.phase == 'shard' and (not args.ci or args.shard_index is None or
                                  not 0 <= args.shard_index < args.shard_count):
        parser.error('CI shards need a valid --shard-index and --shard-count')
    root, exe = args.root.resolve(), args.exe.resolve()
    root.mkdir(parents=True, exist_ok=True)
    if git('status', '--porcelain'):
        raise RuntimeError('Freeze a clean experimental commit before running')
    manifest = json.loads((HERE / 'experiment.json').read_text(encoding='utf-8'))
    commit, binary_sha = git('rev-parse', 'HEAD'), digest(exe)
    frozen = dict(commit=commit, binary_sha256=binary_sha, manifest_sha256=digest(HERE / 'experiment.json'),
                  python=sys.version, baseline=BASELINE, platform=platform.platform(),
                  workflow_run=os.environ.get('GITHUB_RUN_ID'), job=os.environ.get('GITHUB_JOB'))
    frozen_path = root / 'frozen.json'
    if frozen_path.exists():
        old = json.loads(frozen_path.read_text(encoding='utf-8'))
        if any(old[field] != frozen[field] for field in ('commit', 'binary_sha256', 'manifest_sha256')):
            raise RuntimeError('Frozen source/binary/config changed; use a separate dataset')
    else:
        atomic_json(frozen_path, frozen)
        shutil.copy2(HERE / 'experiment.json', root / 'experiment.json')
        shutil.copy2(exe, root / exe.name)
        shutil.copy2(HERE / 'SCP-pacing-experiment-plan.md', root / 'plan.md')
        shutil.copy2(HERE / 'analyze_pacing_research.py', root / 'analyze_pacing_research.py')
        with (root / 'build-provenance.txt').open('w', encoding='utf-8') as output:
            output.write(git('show', '--stat', '--oneline', 'HEAD') + '\n')
        if (exe.parent / 'CMakeCache.txt').exists():
            shutil.copy2(exe.parent / 'CMakeCache.txt', root / 'CMakeCache.txt')
        original_test = args.normal_build / 'Testing/Temporary/LastTest.log' if args.normal_build else None
        if original_test and original_test.exists():
            shutil.copy2(original_test, root / 'baseline-ctest.log')
    running_lock = root / 'runner.lock'
    if running_lock.exists():
        old_pid = json.loads(running_lock.read_text())['pid']
        # Do not discard a live Windows process lock during restart.
        if os.name == 'nt':
            probe = subprocess.run(['powershell', '-NoProfile', '-Command',
                                    f'if (Get-Process -Id {int(old_pid)} -ErrorAction SilentlyContinue) {{ exit 0 }} else {{ exit 1 }}'], capture_output=True)
            alive = probe.returncode == 0
        else:
            try:
                os.kill(old_pid, 0)
                alive = True
            except ProcessLookupError:
                alive = False
        if alive:
            raise RuntimeError(f'Another runner is alive: PID {old_pid}')
        running_lock.unlink()
    with running_lock.open('x') as output:
        json.dump(dict(pid=os.getpid(), phase=args.phase), output)
    results = {}
    for path in (root / 'runs').glob('*/summary.json'):
        results[path.parent.name] = json.loads(path.read_text(encoding='utf-8'))
    started = time.time()
    progress = dict(phase=args.phase, pid=os.getpid(), planned=manifest['expected_runs'], state='running')

    def update_progress():
        progress.update(updated_at=time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                        recorded=len(results), complete=sum(row['status'] == 'complete' for row in results.values()),
                        failed=sum(row['status'] != 'complete' for row in results.values()),
                        plotted=sum((root / 'runs' / case / 'artifact_integrity.json').exists() for case in results),
                        elapsed_seconds=round(time.time() - started, 2))
        atomic_json(root / 'progress.json', progress)

    def run_case(gain, repeat):
        case_id = f'g{gain:04d}-r{repeat:02d}'
        folder = root / 'runs' / case_id
        summary_path = folder / 'summary.json'
        integrity_path = folder / 'artifact_integrity.json'
        if summary_path.exists() and integrity_path.exists():
            result = json.loads(summary_path.read_text(encoding='utf-8'))
            if result['status'] != 'infra_error':
                saved_case = json.loads((folder / 'config.json').read_text(encoding='utf-8'))
                integrity = json.loads(integrity_path.read_text(encoding='utf-8'))
                if saved_case['commit'] != commit or saved_case['binary_sha256'] != binary_sha:
                    raise RuntimeError(f'Cached case belongs to a different experiment: {case_id}')
                for field in ('state_log', 'packet_rtt_file'):
                    if field in integrity and digest(folder / saved_case[field]) != integrity[field]['compressed_sha256']:
                        raise RuntimeError(f'Cached artifact checksum mismatch: {case_id}/{field}')
                return case_id, result
        folder.mkdir(parents=True, exist_ok=True)
        attempts = folder / 'attempts'
        attempts.mkdir(exist_ok=True)
        attempt_number = max([int(p.name) for p in attempts.iterdir() if p.is_dir()] or [0]) + 1
        attempt = attempts / f'{attempt_number:03d}'
        attempt.mkdir()
        prefix = attempt.relative_to(folder).as_posix()
        case = dict(manifest, case_id=case_id, g_hundredths=gain, repeat=repeat,
                    stream_fd=1, state_log=prefix + '/run.log.gz', packet_rtt_file=prefix + '/packet_rtt.csv.gz',
                    commit=commit, binary_sha256=binary_sha, attempt=attempt_number,
                    platform=platform.platform(), workflow_run=os.environ.get('GITHUB_RUN_ID'),
                    job=os.environ.get('GITHUB_JOB'))
        # The per-case gain is a scalar; remove campaign-only lists/counts.
        case.pop('expected_runs'); case.pop('repeats')
        atomic_json(folder / 'config.json', case)
        free = shutil.disk_usage(root).free
        if free < manifest['minimum_free_disk_gib'] * GIB:
            raise RuntimeError(f'Disk reserve reached before {case_id}: {free/GIB:.2f} GiB free')
        command = [str(root / exe.name), '--bytes', str(manifest['bytes']),
                   '--mbps', str(manifest['rate_mbps']), '--delay-ms', str(manifest['rtt_ms']//2),
                   '--loss-ppm', str(manifest['loss_ppm']), '--seed', str(manifest['seed']),
                   '--timeout-ms', str(manifest['virtual_timeout_ms']), '--g-hundredths', str(gain)]
        wall_start = time.perf_counter()
        with (attempt / 'run.log').open('wb') as output:
            try:
                process = subprocess.run(command, cwd=attempt, stdout=output, stderr=subprocess.STDOUT,
                                         timeout=manifest['wall_timeout_seconds'])
                return_code = process.returncode
                result = json.loads((attempt / 'research_summary.json').read_text(encoding='utf-8'))
            except subprocess.TimeoutExpired:
                return_code = None
                result = dict(status='wall_timeout', first_data_tx_ms=None, last_observed_ms=None,
                              dropped=0, received_bytes=0, checksum_ok=False, close_kind='killed_at_wall_timeout')
            except (OSError, ValueError) as error:
                return_code = None
                result = dict(status='infra_error', first_data_tx_ms=None, last_observed_ms=None,
                              dropped=0, received_bytes=0, checksum_ok=False, reason=str(error))
        result.update(return_code=return_code, transfer_wall_seconds=time.perf_counter()-wall_start,
                      attempt=attempt_number, command=command)
        if result['status'] == 'complete' and (return_code != 0 or result['received_bytes'] != manifest['bytes']
                or result['new_bytes'] != manifest['bytes'] or not result['checksum_ok']
                or result['dropped'] or result['observer_errors'] or result['close_kind'] != 'both_closed'):
            raise RuntimeError(f'Contradictory complete result: {case_id}')
        if result.get('first_data_tx_ms') is not None:
            analysis.load_packets(attempt / 'packet_rtt.csv', case, result)
            times, windows = analysis.load_states(attempt / 'run.log', 1)
            end = result.get('data_acked_ms') or result['last_observed_ms']
            if times[-1] < end:
                raise RuntimeError(f'Missing final protocol state: {case_id}')
            result['final_state_time_ms'] = int(times[-1])
        atomic_json(attempt / 'selected_summary.json', result)
        atomic_json(summary_path, result)
        with LOCK:
            results[case_id] = result
            progress['last_case'] = case_id
            update_progress()
        plot_start = time.perf_counter()
        with PLOT_LOCK:
            with (attempt / 'plot.log').open('wb') as output:
                plotted = subprocess.run([sys.executable, str(SOURCE / 'test/plot.py'), str(attempt / 'run.log'),
                                           '--out', str(attempt / 'curves'), '--max-points', '10000'],
                                          stdout=output, stderr=subprocess.STDOUT, timeout=600)
            if plotted.returncode:
                raise RuntimeError(f'Original per-case plot failed: {case_id}; see plot.log')
        result['plot_wall_seconds'] = time.perf_counter()-plot_start
        integrity = dict(state_log=compress_verified(attempt / 'run.log'))
        if (attempt / 'packet_rtt.csv').exists():
            integrity['packet_rtt_file'] = compress_verified(attempt / 'packet_rtt.csv')
        integrity['total_artifact_bytes'] = sum(p.stat().st_size for p in folder.rglob('*') if p.is_file())
        atomic_json(summary_path, result)
        atomic_json(integrity_path, integrity)
        with LOCK:
            results[case_id] = result
            update_progress()
        return case_id, result

    def restore_check():
        if args.ci:
            return
        normal_head = git('rev-parse', 'HEAD', cwd=args.normal_repo)
        normal_dirty = git('status', '--porcelain', cwd=args.normal_repo)
        with (root / f'normal-ctest-{args.phase}.log').open('wb') as output:
            check = subprocess.run([str(args.ctest), '--output-on-failure'], cwd=args.normal_build,
                                   stdout=output, stderr=subprocess.STDOUT, timeout=600)
        atomic_json(root / f'normal-check-{args.phase}.json', dict(head=normal_head,
                    baseline=BASELINE, matches_baseline=normal_head == BASELINE,
                    dirty=normal_dirty, ctest_return_code=check.returncode,
                    explanation='Normal checkout was never modified; no reset or destructive restore required.'))
        if check.returncode:
            raise RuntimeError('Normal functional tests failed; see normal-ctest log')

    try:
        update_progress()
        if args.phase == 'preflight':
            todo = [(g, r) for r in (1, 2) for g in (10, 100, 200, 400, 3200)]
        elif args.phase == 'shard':
            all_cases = [(g, r) for r in range(1, manifest['repeats']+1) for g in manifest['g_hundredths']]
            todo = [case for index, case in enumerate(all_cases) if index % args.shard_count == args.shard_index]
            if args.seed_root:
                seed_root = args.seed_root.resolve()
                seed_frozen = json.loads((seed_root / 'frozen.json').read_text(encoding='utf-8'))
                if any(seed_frozen[field] != frozen[field] for field in ('commit', 'binary_sha256', 'manifest_sha256')):
                    raise RuntimeError('Preflight source/binary/config does not match this shard')
                for gain, repeat in todo:
                    name = f'g{gain:04d}-r{repeat:02d}'
                    seed_case = seed_root / 'runs' / name
                    if seed_case.exists() and not (root / 'runs' / name).exists():
                        shutil.copytree(seed_case, root / 'runs' / name)
                        results[name] = json.loads((seed_case / 'summary.json').read_text(encoding='utf-8'))
            progress.update(shard_index=args.shard_index, shard_count=args.shard_count, assigned=len(todo))
            atomic_json(root / 'shard.json', dict(shard_index=args.shard_index, shard_count=args.shard_count,
                expected_cases=[f'g{g:04d}-r{r:02d}' for g, r in todo], commit=commit,
                binary_sha256=binary_sha, manifest_sha256=frozen['manifest_sha256']))
        else:
            preflight = json.loads((root / 'preflight.json').read_text(encoding='utf-8'))
            if not preflight['pass']:
                raise RuntimeError('Preflight did not pass')
            todo = [(g, r) for r in range(1, manifest['repeats']+1) for g in manifest['g_hundredths']]
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = [pool.submit(run_case, g, r) for g, r in todo]
            try:
                for future in as_completed(futures):
                    case_id, result = future.result()
                    print(f'{case_id}: {result["status"]}; {result["transfer_wall_seconds"]:.2f}s transfer', flush=True)
            except BaseException:
                for future in futures:
                    future.cancel()
                raise
        if args.phase == 'preflight':
            selected = [results[f'g{g:04d}-r{r:02d}'] for g, r in todo]
            artifacts = [json.loads((root / 'runs' / f'g{g:04d}-r{r:02d}' / 'artifact_integrity.json').read_text()) for g, r in todo]
            size_estimate = max(row['total_artifact_bytes'] for row in artifacts) * manifest['expected_runs'] / GIB
            projected_wall = sum(row['transfer_wall_seconds'] + row['plot_wall_seconds'] for row in selected) / len(selected) * manifest['expected_runs'] / args.jobs
            passed = all(row['status'] == 'complete' for row in selected) and size_estimate <= manifest['data_budget_gib']
            preflight = dict(pass_=passed, estimated_total_gib=size_estimate,
                             estimated_wall_hours=projected_wall/3600,
                             note='Estimate from preflight; plotting wait time included; not a guarantee.', cases=[f'g{g:04d}-r{r:02d}' for g, r in todo])
            preflight['pass'] = preflight.pop('pass_')
            atomic_json(root / 'preflight.json', preflight)
            analysis.analyze(root, root / 'preflight-analysis', True)
            if not passed:
                raise RuntimeError('Preflight failed integrity or capacity checks; keep evidence and stop')
            progress['state'] = 'preflight_complete'
        elif args.phase == 'shard':
            if any(results[f'g{g:04d}-r{r:02d}']['status'] != 'complete' for g, r in todo):
                raise RuntimeError('Shard contains unsuccessful transfers; keep evidence and mark CI failed')
            progress['state'] = 'shard_complete'
        else:
            progress['state'] = 'summarizing'
            update_progress()
            analysis.analyze(root, root / 'analysis', False)
            write_report(root, manifest, results)
            restore_check()
            # Only immutable experiment artifacts enter the data index. Runner
            # logs, PID files and tag/push messages may still change afterwards.
            control = {'runner.lock', 'progress.json', 'data-index.json', 'campaign.log',
                       'campaign.err.log', 'background-pid.txt', 'tag-message.txt'}
            inventory = {str(p.relative_to(root)):digest(p) for p in root.rglob('*')
                         if p.is_file() and p.name not in control}
            atomic_json(root / 'data-index.json', dict(files=inventory, commit=commit, binary_sha256=binary_sha))
            index_sha = digest(root / 'data-index.json')
            tag_body = root / 'tag-message.txt'
            tag_body.write_text(f'SCP always-slow-start g sweep, virtual 1 GiB / 1000 Mbps / 100 ms / 0 loss.\n'
                                f'{manifest["expected_runs"]} recorded runs. Dataset: {root}\n'
                                f'Data index SHA256: {index_sha}\n'
                                f'Experimental commit: {commit}\n'
                                f'Complete: {sum(r["status"] == "complete" for r in results.values())}; '
                                f'other results: {sum(r["status"] != "complete" for r in results.values())}.\n', encoding='utf-8')
            if git('tag', '--list', TAG):
                if git('rev-list', '-n', '1', TAG) != commit:
                    raise RuntimeError('Tag exists for a different commit; never overwrite it')
            else:
                subprocess.run(['git', 'tag', '-a', TAG, commit, '-F', str(tag_body)], cwd=SOURCE, check=True)
            subprocess.run(['git', 'push', 'origin', git('branch', '--show-current'), f'refs/tags/{TAG}'], cwd=SOURCE, check=True)
            progress.update(state='complete', tag=TAG, data_index_sha256=index_sha)
    except BaseException as error:
        progress.update(state='stopped', reason=f'{type(error).__name__}: {error}')
        raise
    finally:
        try:
            if args.phase == 'preflight' or progress['state'] != 'complete':
                restore_check()
        finally:
            with LOCK:
                update_progress()
            running_lock.unlink(missing_ok=True)


def write_report(root, manifest, results):
    groups = {}
    for case_id, row in results.items():
        gain = int(case_id.split('-')[0][1:]) / 100
        groups.setdefault(gain, []).append((case_id, row))
    table = []
    for gain, group in sorted(groups.items()):
        completed = [row for name, row in group if row['status'] == 'complete']
        durations = [row['data_acked_ms']-row['first_data_tx_ms'] for row in completed]
        hashes = [json.loads((root / 'runs' / name / 'artifact_integrity.json').read_text())['state_log']['uncompressed_sha256'] for name, row in group]
        table.append(dict(g=gain, runs=len(group), complete=len(completed), failed=len(group)-len(completed),
                          data_duration_ms_median=median(durations) if durations else None,
                          goodput_mbps_median=median(manifest['bytes']*8/d/1000 for d in durations) if durations else None,
                          max_cwnd_median=median(row['max_cwnd'] for row in completed) if completed else None,
                          retrans_packets_median=median(row['retrans_packets'] for row in completed) if completed else None,
                          unique_state_log_hashes=len(set(hashes))))
    analysis.write_csv(root / 'analysis/g_summary.csv', table)
    valid = [row for row in table if row['complete']]
    fastest = max(valid, key=lambda row:row['goodput_mbps_median']) if valid else None
    slowest = min(valid, key=lambda row:row['goodput_mbps_median']) if valid else None
    lines = ['# SCP pacing g 扫描结果', '',
             f'环境：虚拟网络；1 GiB；双向 1000 Mbps；基础 RTT 100 ms；配置丢包 0%；{len(manifest["g_hundredths"])} 个 g，每个 {manifest["repeats"]} 次。', '',
             f'记录 {len(results)} 次；完整校验成功 {sum(row["complete"] for row in table)} 次。', '',
             '## 传输和重复性', '',
             f'最快中位数：{fastest}；最慢中位数：{slowest}。', '',
             f'状态日志在同一 g 下完全一致的组数：{sum(row["unique_state_log_hashes"] == 1 for row in table)}/{len(table)}。', '',
             '详细数值见 analysis/g_summary.csv；它只对校验完整的传输计算 goodput，失败数明确保留。', '',
             '## 研究图和表', '',
             '![全部 g 的 cwnd](analysis/cwnd_all_g.png)', '',
             '![全部 g 的逐 RTT 分散度](analysis/packet_dispersion_all_g.png)', '',
             'cwnd_by_rtt.csv 逐 RTT 比较全部 g；cwnd_by_rtt_long.csv 保留各次重复和采样年龄。', '',
             'packet_dispersion_by_rtt.csv 保留首末包、字节序号、跨度、包数、毫秒突发比例、HHI、间隔 CV。', '',
             '## 解读范围', '',
             '发送分散度来自协议 transport-send 回调、1 ms 精度。基础 RTT 分格不等于测得的 SRTT，曲线重合需结合整数毫秒 pacing 量化和 64 MiB 窗口触顶解读。', '',
             '零丢包虚拟网络重复用于检查复现，不是独立随机网络抽样。完整 RTT 且至少两个包才进入分散度热图，尾格原始统计仍保留。', '',
             '正常 checkout 保持基线；normal-check-full.json 与 normal-ctest-full.log 记录结束后的功能测试。']
    (root / 'report.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
