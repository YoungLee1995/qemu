#!/usr/bin/env python3
"""Build a tiny initramfs and test Linux -> experimental EP -> real simu."""
import gzip
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time

parser = argparse.ArgumentParser()
parser.add_argument('--fault', choices=['disconnect', 'timeout', 'wrong-id'])
parser.add_argument('--topology8', action='store_true')
options = parser.parse_args()
root = Path(__file__).resolve().parents[3]
base = root / 'experiments/qemu_stage1'
kernel = base / 'assets/vmlinuz-lts'
expected_kernel = '9d6eff6e7bcd5f5fba623f7a44c565ca6914da2b91110a8dc72a3a63cb088693'
if hashlib.sha256(kernel.read_bytes()).hexdigest() != expected_kernel:
    raise RuntimeError('kernel missing/incomplete/changed; run prepare_assets.py')
work = base / f'run-{time.time_ns()}'
work.mkdir(parents=True)
fs = work / 'rootfs'
shutil.copytree(base / 'rootfs', fs)
for name in ('proc', 'sys', 'dev', 'tmp', 'bin'):
    (fs / name).mkdir(exist_ok=True)
(fs / 'init').write_text('''#!/bin/busybox sh
/bin/busybox mount -t proc proc /proc
/bin/busybox mount -t sysfs sysfs /sys
/bin/busybox mount -t devtmpfs devtmpfs /dev
echo STAGE1_LINUX_BOOTED
/guest-test
result=$?
echo GUEST_RESULT=$result
/bin/busybox poweroff -f
''')
(fs / 'init').chmod(0o755)
if options.fault:
    (fs / 'init').write_text((fs / 'init').read_text().replace('/guest-test\n',
        f'/guest-test {options.fault}\n'))
if options.topology8:
    (fs / 'init').write_text((fs / 'init').read_text().replace('/guest-test\n',
        '/guest-test topology8\n'))
subprocess.run(['gcc', '-static', '-O2', '-Wall', '-Wextra',
                str(root / 'qemu/zc_simulator/stage1/guest-test.c'),
                '-o', str(fs / 'guest-test')], check=True)
shutil.copy2(root / 'simulator/riscv-tests/packet_runtime_test/tensor-args.bin', fs)
baseline = sorted((root / 'experiments/single_die_packet').glob('run-*/moe/data/result_l1_spike.bin'))[-1]
shutil.copy2(baseline, fs / 'legacy-result.bin')


def cpio_entry(name, data, mode, inode):
    encoded = name.encode() + b'\0'
    fields = [inode, mode, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(encoded), 0]
    header = b'070701' + b''.join(f'{v:08x}'.encode() for v in fields)
    a = header + encoded
    a += b'\0' * (-len(a) % 4)
    a += data + b'\0' * (-len(data) % 4)
    return a


archive = bytearray()
for i, p in enumerate(sorted(fs.rglob('*')), 1):
    if p.is_symlink():
        continue
    archive += cpio_entry(str(p.relative_to(fs)), b'' if p.is_dir() else p.read_bytes(),
                          p.stat().st_mode, i)
archive += cpio_entry('TRAILER!!!', b'', 0, 999999)
initramfs = work / 'initramfs.cpio.gz'
initramfs.write_bytes(gzip.compress(archive, mtime=0))
bridge_cmd = ['python3', str(root / 'qemu/zc_simulator/stage1/simu_bridge.py'),
              '--work', str(work / 'simu')]
if options.fault:
    bridge_cmd = ['python3', str(root / 'qemu/zc_simulator/stage1/fault_bridge.py'),
                  '--work', str(work / 'simu'), '--fault', options.fault]
with (work / 'bridge.log').open('w') as blog:
    bridge = subprocess.Popen(bridge_cmd, stdout=blog, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 20
        sock = work / 'simu/simu.sock'
        while not sock.exists():
            if bridge.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError('bridge failed to listen')
            time.sleep(0.05)
        cmd = [str(root / 'qemu/build/qemu-system-x86_64'), '-machine', 'q35',
               '-accel', 'tcg', '-m', '512M', '-smp', '1', '-nodefaults',
               '-display', 'none', '-serial', 'stdio', '-monitor', 'none',
               '-qmp', f'unix:{work}/qmp.sock,server=on,wait=off',
               '-no-reboot', '-kernel', str(base / 'assets/vmlinuz-lts'),
               '-initrd', str(initramfs),
               '-append', 'console=ttyS0 rdinit=/init intel_iommu=on iommu.strict=1 panic=-1',
               '-device', 'intel-iommu,intremap=on,caching-mode=on',
               '-device', 'pcie-root-port,id=rp,chassis=1',
               '-device', 'x3130-upstream,id=sw,bus=rp',
               '-device', 'xio3130-downstream,id=dp,bus=sw,chassis=2',
               '-chardev', f'socket,id=sim,path={sock}',
               '-device', 'zc-pcie-sim,id=ep,bus=dp,chardev=sim']
        if options.topology8:
            for i in range(1, 8):
                cmd += ['-device', f'xio3130-downstream,id=dp{i},bus=sw,chassis={i+2},addr={i}',
                        '-chardev', f'null,id=stub{i}',
                        '-device', f'zc-pcie-sim,id=ep{i},bus=dp{i},chardev=stub{i}']
        (work / 'command.json').write_text(json.dumps(cmd, indent=2))
        paths = [root / 'qemu/build/qemu-system-x86_64', base / 'assets/vmlinuz-lts',
                 fs / 'bin/busybox', initramfs, baseline]
        paths += [root / 'qemu/hw/misc/zc-pcie-sim.c',
                  *sorted(Path(__file__).parent.glob('*.py')),
                  Path(__file__).parent / 'guest-test.c']
        (work / 'manifest.json').write_text(json.dumps({str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                                                      for p in paths}, indent=2))
        with (work / 'linux.log').open('w') as log:
            p = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, timeout=180)
        text = (work / 'linux.log').read_text(errors='replace')
        result = {'qemu_exit': p.returncode, 'guest_pass': 'STAGE1_PASS ' in text,
                  'guest_exit_zero': 'GUEST_RESULT=0' in text,
                  'iommu_enabled': 'DMAR: Intel(R) Virtualization Technology for Directed I/O' in text}
        (work / 'results.json').write_text(json.dumps(result, indent=2))
        print(work, result, flush=True)
        if p.returncode or not all(result[k] for k in ('guest_pass', 'guest_exit_zero', 'iommu_enabled')):
            raise RuntimeError('Linux integration failed; inspect linux.log')
    finally:
        if bridge.poll() is None:
            bridge.terminate()
        bridge.wait(timeout=10)
