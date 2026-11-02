#!/usr/bin/env python3
"""Serve the Ubuntu command line on a loopback browser terminal."""
import argparse
import os
from pathlib import Path
import shutil
import sys
from paths import SOURCE, STATE

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--port', type=int, default=7681)
a = p.parse_args()
ttyd = shutil.which('ttyd') or str(STATE / 'tools/local/ttyd')
if not Path(ttyd).is_file():
    p.error('Install ttyd or place it at ' + str(STATE / 'tools/local/ttyd'))
os.execv(ttyd, [ttyd, '--writable', '--interface', '127.0.0.1', '--port', str(a.port),
    '-t', 'titleFixed=Ubuntu 八卡 Demo 终端', sys.executable, str(SOURCE / 'terminal.py')])
