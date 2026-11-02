#!/usr/bin/env python3
"""Connect to, inspect, or gracefully stop the local desktop experiment."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

from paths import STATE

HERE = STATE


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('action', choices=('ssh', 'status', 'stop'))
    ap.add_argument('command', nargs=argparse.REMAINDER)
    args = ap.parse_args()
    active = json.loads((HERE / 'active.json').read_text())
    if active.get('stopped'):
        ap.error('desktop VM has stopped; run desktop.py to start it')
    cmd = ['ssh', '-i', str(HERE / 'ssh_key'), '-p', str(active['ssh_port']),
           '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=accept-new',
           '-o', 'UserKnownHostsFile=' + str(HERE / 'known_hosts'),
           '-o', 'ConnectTimeout=30', 't100@' + active.get('bind', '127.0.0.1')]
    if args.action == 'status':
        cmd += ['uname -r; lspci -nn -d 16c3:abcd; '
                'ls -l /dev/test_dma_* 2>/dev/null; '
                'systemctl is-active xrdp; '
                'test -e /var/lib/t100-desktop-ready && echo T100_DESKTOP_READY; '
                'sudo tail -12 /var/log/t100-desktop-setup.log 2>/dev/null']
    elif args.action == 'stop':
        cmd += ['sudo poweroff']
    else:
        cmd += args.command
    sys.exit(subprocess.call(cmd))


if __name__ == '__main__':
    main()
