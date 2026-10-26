import json
import subprocess
import sys
import tempfile
from pathlib import Path

from report import write_report


executable = Path(sys.argv[1]).resolve()
with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    command = [str(executable), '--bytes', '65553', '--mbps', '100',
               '--delay-ms', '5', '--loss-ppm', '0', '--queue-packets', '100', '--seed', '7']
    first = None
    for repeat in range(2):
        result = subprocess.run(command, cwd=root, stdout=subprocess.DEVNULL, check=True)
        row = json.loads((root / 'summary.json').read_text())
        assert all(row[key] for key in ('success', 'checksum_ok', 'sequence_ok', 'cleanup_ok'))
        assert row['timeouts'] == row['retrans_packets'] == row['active_timers'] == row['core_live_allocations'] == 0
        assert row['srtt_max_ms'] >= row['srtt_avg_ms'] >= 10
        assert abs(row['bufferbloat_ratio'] - row['srtt_avg_ms'] / 10) < 0.000002
        assert row['startup_rtts_to_80pct'] > 0
        if first is not None: assert row == first
        first = row
    result = subprocess.run(command + ['--timeout-ms', '1'], cwd=root, stdout=subprocess.DEVNULL)
    row = json.loads((root / 'summary.json').read_text())
    assert result.returncode == 1 and not row['success'] and not row['checksum_ok']
    assert row['cleanup_ok'] and row['active_timers'] == row['core_live_allocations'] == 0
    row.update(bytes=65553, scenario='intentional-timeout', goodput_mbps=0,
               repro_command=subprocess.list2cmdline(command + ['--timeout-ms', '1']))
    assert write_report(root, [row]) == 0
    assert 'Repro: ' + row['repro_command'] in (root / 'report.md').read_text()
print('PASS: observed metrics, deterministic replay, failed checksum/timeout, cleanup and exact repro')
