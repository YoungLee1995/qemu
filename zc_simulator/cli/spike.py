#!/usr/bin/env python3
"""Run independent Spike instances, or QEMU-compatible backend services."""
from pathlib import Path
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
    args = ap.parse_args()
    if args.mode == 'backend' and args.cores == 'npu':
        ap.error('QEMU backend requires an MCU implementing its ready/session ABI')
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
                    command = [config['spike'], '--isa=' + config['isa'], '--runtime-func',
                               config['firmware'], '-cfg', config['cfg']]
                else:
                    sock = Path(tmp) / f'card{i}.sock'
                    command = [sys.executable, str(REPO / 'zc_simulator/tools/p1/service.py'),
                               '--socket', str(sock), '--config', str(path), '--work', str(card),
                               '--card', str(i)]
                    cards.append(dict(socket=str(sock), config=str(path)))
                save(card / 'command.json', command)
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
