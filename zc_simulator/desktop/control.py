#!/usr/bin/env python3
"""Connect to, inspect, or gracefully stop the local desktop experiment."""
import argparse
import json
import socket
from pathlib import Path
import subprocess
import sys

from paths import STATE

HERE = STATE


def powerdown(active):
    # Older launchers saved QMP only in the recorded QEMU argv.
    path = active.get('qmp')
    if not path:
        command = json.loads((Path(active['work']) / 'qemu-command.json').read_text())
        endpoint = command[command.index('-qmp') + 1]
        if not endpoint.startswith('unix:'):
            raise ValueError('unsupported QMP endpoint')
        path = endpoint[5:].split(',')[0]
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(10)
        conn.connect(path)
        with conn.makefile('rwb') as stream:
            greeting = json.loads(stream.readline())
            if 'QMP' not in greeting:
                raise ValueError('invalid QMP greeting')
            for request_id, execute in enumerate(('qmp_capabilities', 'system_powerdown')):
                stream.write((json.dumps(dict(execute=execute, id=request_id)) + '\n').encode())
                stream.flush()
                while True:
                    reply = json.loads(stream.readline())
                    if reply.get('id') == request_id:
                        if 'error' in reply:
                            raise RuntimeError(reply['error'])
                        break
    print('已通过 QMP 发送 ACPI 正常关机请求（不依赖 SSH）。\n'
          '请等待 Ubuntu 关机、原 desktop.py 退出后再初始化或重启；本消息不表示关机已完成。', flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('action', choices=('ssh', 'status', 'stop'))
    ap.add_argument('command', nargs=argparse.REMAINDER)
    args = ap.parse_args()
    if not (HERE / 'active.json').is_file():
        ap.exit(1, '没有桌面启动记录，请先运行 desktop.py。\n')
    active = json.loads((HERE / 'active.json').read_text())
    if active.get('stopped'):
        ap.error('desktop VM has stopped; run desktop.py to start it')
    if args.action == 'stop':
        try:
            powerdown(active)
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            ap.exit(1, f'无法发送正常关机请求：{exc}\n请检查原启动终端和本次运行日志，尚未确认关机。\n')
        return
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
    else:
        cmd += args.command
    sys.exit(subprocess.call(cmd))


if __name__ == '__main__':
    main()
