#!/usr/bin/env python3
"""Open an interactive terminal in the running Ubuntu demo."""
import json
import os
from pathlib import Path
from paths import STATE
h = STATE
if not (h / 'active.json').is_file():
    raise SystemExit(
        f'尚无 Ubuntu 桌面启动记录：{h / "active.json"}\n'
        'run_linux.py 是运行后自动关机的测试，不会启动常驻 Ubuntu。\n'
        '首次使用请按 zc_simulator/克隆后构建与运行简易指南.md 的桌面准备流程下载镜像并运行 prepare.py。\n'
        '然后在终端 A 运行 python3 zc_simulator/desktop/desktop.py --cards 8，\n'
        '保持运行，再在终端 B 运行本命令。所有终端须使用相同的 ZC_DESKTOP_STATE 设置。')
s = json.loads((h / 'active.json').read_text())
if s.get('stopped'):
    raise SystemExit('Ubuntu demo is stopped; start desktop.py first')
print('Connecting to the eight-card Ubuntu demo. Type exit to disconnect.', flush=True)
os.execvp('ssh', ['ssh', '-tt', '-i', str(h / 'ssh_key'), '-p', str(s['ssh_port']),
    '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=accept-new',
    '-o', 'UserKnownHostsFile=' + str(h / 'known_hosts'),
    '-o', 'ConnectTimeout=15', 't100@' + s.get('bind', '127.0.0.1')])
