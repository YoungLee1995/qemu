#!/usr/bin/env python3
"""Build the repository host demo inside the running desktop VM."""
import argparse
import shutil
import subprocess
import sys

from paths import REPO, SOURCE, STATE


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--interactive', action='store_true', help='Open the host demo command prompt')
    args = ap.parse_args()
    source = REPO / 'zc_simulator/host-demo/ssv_test_app.c'
    if not source.is_file():
        ap.error(f'missing host demo source: {source}')
    share = STATE / 'share/pcie-demo-check'
    share.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, share / source.name)
    # The original application allocates one 2 MiB HugeTLB page per endpoint,
    # including for list-devices. Restore the guest setting on exit.
    (share / 'run-host-demo').write_text('''#!/bin/bash
set -eu
exec 9>/run/lock/t100-host-demo.lock
flock -n 9
old=$(cat /proc/sys/vm/nr_hugepages)
trap 'sysctl -w vm.nr_hugepages="$old" >/dev/null' EXIT
if [ "$old" -lt 16 ]; then
    sysctl -w vm.nr_hugepages=16 >/dev/null
fi
/opt/t100/ssv_test_app.bin "$@"
''')
    remote = '''set -eu
mkdir -p /tmp/pcie-demo-check
gcc -O2 -Wall -Wextra -o /tmp/pcie-demo-check/ssv_test_app.bin /mnt/hostshare/pcie-demo-check/ssv_test_app.c
sudo install -m 755 /tmp/pcie-demo-check/ssv_test_app.bin /opt/t100/ssv_test_app.bin
sudo install -m 755 /mnt/hostshare/pcie-demo-check/run-host-demo /opt/t100/run-host-demo
sudo install -m 755 /tmp/pcie-demo-check/ssv_test_app.bin /mnt/hostshare/pcie-demo-check/ssv_test_app.bin
'''
    control = [sys.executable, str(SOURCE / 'control.py'), 'ssh']
    subprocess.run(control + [remote], check=True)
    command = 'sudo /opt/t100/run-host-demo'
    if args.interactive:
        return subprocess.call(control + ['-tt', command])
    result = subprocess.run(control + ['timeout 90 ' + command + ' list-devices'],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    (share / 'host-run.log').write_text(result.stdout)
    print(result.stdout, end='')
    print(f'Log: {share / "host-run.log"}')
    return result.returncode


if __name__ == '__main__':
    sys.exit(main())
