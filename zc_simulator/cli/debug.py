#!/usr/bin/env python3
"""Attach to a live backend Spike debugger; Ctrl+] detaches without stopping it."""
import argparse
import json
import os
from pathlib import Path
import select
import socket
import sys
import termios
import tty


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backends', required=True, type=Path)
    parser.add_argument('--card', type=int, default=0)
    args = parser.parse_args()
    cards = json.loads(args.backends.read_text())['cards']
    if not 0 <= args.card < len(cards):
        parser.error('card outside backend plan')
    path = cards[args.card].get('debug_socket')
    if not path:
        parser.error('backend was not started with -d')
    if not sys.stdin.isatty():
        parser.error('interactive terminal required')
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        conn.connect(path)
    except OSError as exc:
        parser.exit(1, f'{exc}; start QEMU and wait for Spike spawn first\n')
    print('Connected to Spike. Ctrl+] detaches; Ctrl+C interrupts simulated execution.', flush=True)
    previous = termios.tcgetattr(0)
    try:
        tty.setraw(0)
        while True:
            for source in select.select([0, conn], [], [])[0]:
                if source == 0:
                    data = os.read(0, 4096)
                    if not data or b'\x1d' in data:
                        return
                    conn.sendall(data)
                else:
                    data = conn.recv(65536)
                    if not data:
                        return
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
    finally:
        termios.tcsetattr(0, termios.TCSADRAIN, previous)
        conn.close()


if __name__ == '__main__':
    main()
