#!/usr/bin/env python3
"""Run the standalone QEMU/P5 delivery checks using the in-repository bundle."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--skip-build', action='store_true')
    args = ap.parse_args()
    work = HERE / 'evidence' / f'all-{time.time_ns()}'
    work.mkdir(parents=True)
    result = dict(passed=False, scope='standalone-p5-delivery', steps={})
    try:
        if not args.skip_build:
            subprocess.run(['ninja', '-C', str(REPO / 'build'), 'qemu-system-x86_64'], check=True)
        steps = [('protocol', 'test_fabric.py', []),
                 ('calibration', 'run_calibration.py', []),
                 ('tasks', 'run_tasks.py', []),
                 ('irq-errors', 'run_irq_errors.py', []),
                 ('faults', 'run_faults.py', []),
                 ('communication2', 'run_communication.py', ['--cards', '2']),
                 ('communication8', 'run_communication.py', ['--cards', '8']),
                 ('linux2', 'run_linux.py', ['--cards', '2']),
                 ('linux8', 'run_linux.py', ['--cards', '8'])]
        for name, script, flags in steps:
            log = work / f'{name}.log'
            with log.open('w') as output:
                code = subprocess.run([sys.executable, str(HERE / script)] + flags,
                                      cwd=REPO, stdout=output, stderr=subprocess.STDOUT,
                                      timeout=1200).returncode
            result['steps'][name] = dict(exit_code=code, log=str(log))
            print(name, 'PASS' if code == 0 else 'FAIL', flush=True)
            if code:
                raise RuntimeError(f'{name} failed; see {log}')
        result['passed'] = True
    finally:
        (work / 'results.json').write_text(json.dumps(result, indent=2))
        print('Evidence:', work, flush=True)


if __name__ == '__main__':
    main()
