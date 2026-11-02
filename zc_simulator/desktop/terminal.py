#!/usr/bin/env python3
"""Open an interactive terminal in the running Ubuntu demo."""
import json
import os
from pathlib import Path
from paths import STATE
h = STATE
s = json.loads((h / 'active.json').read_text())
if s.get('stopped'):
    raise SystemExit('Ubuntu demo is stopped; start desktop.py first')
print('Connecting to the eight-card Ubuntu demo. Type exit to disconnect.', flush=True)
os.execvp('ssh', ['ssh', '-tt', '-i', str(h / 'ssh_key'), '-p', str(s['ssh_port']),
    '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=accept-new',
    '-o', 'UserKnownHostsFile=' + str(h / 'known_hosts'),
    '-o', 'ConnectTimeout=15', 't100@' + s.get('bind', '127.0.0.1')])
