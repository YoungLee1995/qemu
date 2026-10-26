#!/usr/bin/env python3
"""Run the experimental first-stage QEMU acceptance suite, sequentially."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

here = Path(__file__).resolve().parent
root = here.parents[2]
work = root / 'experiments/qemu_stage1' / f'suite-{time.time_ns()}'
work.mkdir(parents=True)
results = []
cases = [
    ('linux-simu', [sys.executable, str(here / 'run_linux.py')], root, 180),
    ('topology8', [sys.executable, str(here / 'run_linux.py'), '--topology8'], root, 180),
    ('disconnect', [sys.executable, str(here / 'run_linux.py'), '--fault', 'disconnect'], root, 180),
    ('wrong-id', [sys.executable, str(here / 'run_linux.py'), '--fault', 'wrong-id'], root, 180),
    ('timeout', [sys.executable, str(here / 'run_linux.py'), '--fault', 'timeout'], root, 180),
    ('reset-inflight', [sys.executable, str(here / 'reset_probe.py')], root, 60),
    ('intel-iommu', ['./tests/qtest/intel-iommu-test'], root / 'qemu/build', 60),
    ('intel-iommu-invalidation', ['./tests/qtest/iommu-intel-inv-test'], root / 'qemu/build', 60),
]
environment = dict(os.environ, QTEST_QEMU_BINARY=str(root / 'qemu/build/qemu-system-x86_64'))
try:
    for name, command, cwd, limit in cases:
        start = time.monotonic()
        with (work / f'{name}.log').open('w') as log:
            try:
                code = subprocess.run(command, cwd=cwd, env=environment,
                                      stdout=log, stderr=subprocess.STDOUT,
                                      timeout=limit).returncode
            except subprocess.TimeoutExpired:
                code = 124
        results.append({'name': name, 'exit_code': code,
                        'elapsed_seconds': round(time.monotonic()-start, 3),
                        'command': command, 'cwd': str(cwd)})
        print(name, 'PASS' if code == 0 else f'FAIL {code}', flush=True)
finally:
    passed = len(results) == len(cases) and all(r['exit_code'] == 0 for r in results)
    (work / 'results.json').write_text(json.dumps({'experimental_stage1_pass': passed,
                                                  'cases': results}, indent=2))
    print('Evidence:', work, flush=True)
if not passed:
    raise SystemExit(1)
