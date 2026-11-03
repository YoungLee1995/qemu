#!/usr/bin/env python3
"""Run independent Spike instances, or QEMU-compatible backend services."""
from pathlib import Path
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import threading

from common import REPO, inputs, parser, save


def main():
    ap = parser(__doc__)
    ap.add_argument('--mode', choices=('standalone', 'backend'), default='standalone')
    ap.add_argument('--cards', type=int, choices=(1, 2, 8), default=1,
                    help='One single-die model process per card/instance')
    ap.add_argument('-d', '--debug', action='store_true',
                    help='Interactive Spike debugger in this terminal (standalone, one card)')
    args = ap.parse_args()
    if args.debug and (args.mode != 'standalone' or args.cards != 1):
        ap.error('-d requires --mode standalone --cards 1; backend interactive debugging is not supported')
    if args.mode == 'backend' and args.cores == 'npu':
        ap.error('QEMU backend currently supports --cores both or mcu; use standalone for npu-only')
    if args.cores == 'mcu' and args.npu_bin:
        ap.error('--npu-bin requires --cores both or npu')
    if args.cores == 'npu' and args.mcu_bin:
        ap.error('--mcu-bin requires --cores both or mcu')
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=False)
    done = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: done.set())
    processes = []
    with tempfile.TemporaryDirectory(prefix='zc-manual-') as tmp:
        try:
            cards = []
            for i in range(args.cards):
                card = work / f'card{i}'
                config = inputs(args, card)
                path = card / 'backend.json'
                save(path, config)
                if args.mode == 'standalone':
                    command = [config['spike']] + (['-d'] if args.debug else []) + [
                               '--isa=' + config['isa'], '--runtime-func',
                               config['firmware'], '-cfg', config['cfg']]
                    script = card / 'run-spike.sh'
                    script.write_text('#!/bin/sh\ncd ' + shlex.quote(str(card)) +
                                      '\nexec ' + shlex.join(command) + '\n')
                    script.chmod(0o755)
                else:
                    sock = Path(tmp) / f'card{i}.sock'
                    command = [sys.executable, str(REPO / 'zc_simulator/tools/p1/service.py'),
                               '--socket', str(sock), '--config', str(path), '--work', str(card),
                               '--card', str(i)]
                    cards.append(dict(socket=str(sock), config=str(path)))
                save(card / 'command.json', command)
                if args.debug:
                    print('Spike command: ' + shlex.join(command), flush=True)
                    print(f'Rerun: {card / "run-spike.sh"}', flush=True)
                    # Replace the launcher: the debugger owns this terminal,
                    # including Ctrl+C, rather than the supervisor stop handler.
                    for sig in (signal.SIGINT, signal.SIGTERM):
                        signal.signal(sig, signal.SIG_DFL)
                    os.chdir(card)
                    os.execv(command[0], command)
                with (card / 'console.log').open('w') as log:
                    processes.append(subprocess.Popen(command, cwd=card, stdout=log,
                                                      stderr=subprocess.STDOUT))
            if args.mode == 'backend':
                import time
                for card, proc in zip(cards, processes):
                    end = time.monotonic() + 10
                    while not Path(card['socket']).exists():
                        if proc.poll() is not None or time.monotonic() > end:
                            raise RuntimeError('backend failed; see card*/console.log')
                        done.wait(.02)
                save(work / 'backends.json', dict(schema=1, bundle=str(args.bundle.resolve()),
                                                 cores=args.cores, cards=cards))
                print(f'BACKENDS_READY {work / "backends.json"}', flush=True)
            else:
                print(f'Spike processes: {[p.pid for p in processes]}; logs: {work}', flush=True)
            while not done.wait(.1):
                codes = [p.poll() for p in processes]
                if any(c is not None and c != 0 for c in codes):
                    raise RuntimeError(f'Spike/service exited {codes}; see {work}/card*/console.log')
                if all(c is not None for c in codes):
                    break
        finally:
            for proc in processes:
                if proc.poll() is None:
                    proc.terminate()
            for proc in processes:
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()


if __name__ == '__main__':
    main()
