#!/usr/bin/env python3
"""Start QEMU with separately launched Spike backend services."""
import argparse
import fcntl
import gzip
import json
from pathlib import Path
import signal
import subprocess

from common import REPO, save
from linux_bundle import BundlePlatform, unpack_initramfs, validate_bundle
from run_p1 import digest, entry


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--backends', required=True, type=Path, help='backends.json from cli/spike.py')
    ap.add_argument('--work', required=True, type=Path, help='New QEMU log directory')
    ap.add_argument('--guest', choices=('console', 'test', 'desktop'), default='console')
    ap.add_argument('--state', type=Path, default=REPO / 'zc_simulator/desktop/state')
    ap.add_argument('--memory', default='4G')
    ap.add_argument('--ssh-port', type=int, default=2222)
    ap.add_argument('--rdp-port', type=int, default=13389)
    ap.add_argument('--bind', default='127.0.0.1')
    args = ap.parse_args()
    plan = json.loads(args.backends.read_text())
    cards = plan['cards']
    if len(cards) not in (1, 2, 8):
        ap.error('supported card counts: 1, 2, 8')
    if args.guest == 'test' and (len(cards) == 1 or plan['cores'] != 'both'):
        ap.error('test guest requires 2/8 cards with MCU+NPU; use console for MCU-only')
    for card in cards:
        if not Path(card['socket']).is_socket():
            ap.error('start cli/spike.py --mode backend first')
    bundle = Path(plan['bundle'])
    validate_bundle(bundle)
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=False)
    p = BundlePlatform(work, len(cards), bundle)
    p.spec['peer_base'], p.spec['peer_size'] = 0x100000000, 0xff00000000
    p.cfg = Path(cards[0]['config'])
    p.sockets = [Path(c['socket']) for c in cards]
    save(work / 'platform.json', p.spec)
    lock = None
    try:
        p.start_fabric()
        device_args = p.device_args()
        for i, value in enumerate(device_args):
            if value.startswith('zc-pcie-p1,'):
                card_id = int(value.split('card-id=')[1].split(',')[0])
                device_args[i] = value.replace('config-sha256=' + digest(p.cfg),
                    'config-sha256=' + digest(Path(cards[card_id]['config'])))
        command = [str(REPO / 'build/qemu-system-x86_64'), '-L', str(REPO / 'pc-bios'),
                   '-machine', 'q35', '-accel', 'tcg', '-cpu', 'max', '-m', args.memory,
                   '-smp', '4,sockets=1,cores=4,threads=1', '-nodefaults', '-display', 'none',
                   '-serial', 'stdio', '-monitor', 'none', '-no-reboot',
                   '-global', 'q35-pcihost.pci-hole64-size=512G',
                   '-device', 'intel-iommu,intremap=on,caching-mode=on']
        if args.guest == 'desktop':
            state = args.state.resolve()
            lock = (state / 'desktop.lock').open('a')
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for name in ('ubuntu-24.04-desktop.qcow2', 'seed.iso'):
                if not (state / name).is_file():
                    ap.error('prepare the desktop state with desktop/prepare.py first')
            command += ['-drive', f'file={state}/ubuntu-24.04-desktop.qcow2,format=qcow2,if=none,id=osdisk',
                        '-device', 'virtio-blk-pci,drive=osdisk,addr=4,iommu_platform=on,disable-legacy=on',
                        '-drive', f'file={state}/seed.iso,format=raw,media=cdrom,readonly=on',
                        '-netdev', f'user,id=net0,hostfwd=tcp:{args.bind}:{args.ssh_port}-:22,hostfwd=tcp:{args.bind}:{args.rdp_port}-:3389',
                        '-device', 'virtio-net-pci,netdev=net0,addr=5,romfile=,iommu_platform=on,disable-legacy=on',
                        '-fsdev', f'local,id=share,path={state}/share,security_model=none',
                        '-device', 'virtio-9p-pci,fsdev=share,mount_tag=hostshare,addr=6,iommu_platform=on,disable-legacy=on']
        else:
            initrd = bundle / f'guest/initramfs-{2 if len(cards) == 1 else len(cards)}.cpio.gz'
            if args.guest == 'console':
                fs = work / 'rootfs'
                fs.mkdir()
                unpack_initramfs(initrd, fs)
                (fs / 'init').write_text('''#!/bin/busybox sh
/bin/busybox mount -t proc proc /proc
/bin/busybox mount -t sysfs sysfs /sys
/bin/busybox mount -t devtmpfs devtmpfs /dev
/bin/busybox insmod /ssv_test_driver.ko || exit 1
echo ZC_CONSOLE_READY
exec /bin/busybox sh
''')
                (fs / 'init').chmod(0o755)
                archive = b''.join(entry(str(f.relative_to(fs)), b'' if f.is_dir() else f.read_bytes(),
                                        f.stat().st_mode, i) for i, f in enumerate(sorted(fs.rglob('*')), 1)
                                   if not f.is_symlink())
                initrd = work / 'console.cpio.gz'
                initrd.write_bytes(gzip.compress(archive + entry('TRAILER!!!', b'', 0, 999999), mtime=0))
            command += ['-kernel', str(bundle / 'guest/vmlinuz-lts'), '-initrd', str(initrd),
                        '-append', 'console=ttyS0 rdinit=/init intel_iommu=on iommu.strict=1 pci=realloc=on panic=-1']
        command += device_args
        save(work / 'qemu-command.json', command)
        with (work / 'run-qemu.sh').open('w') as f:
            import shlex
            f.write('#!/bin/sh\nexec ' + shlex.join(command) + '\n')
        p.qemu = subprocess.Popen(command)
        def stop_vm(*_):
            if p.qemu.poll() is None:
                p.qemu.terminate()
        signal.signal(signal.SIGTERM, stop_vm)
        signal.signal(signal.SIGINT, stop_vm)
        code = p.qemu.wait()
        if code:
            raise SystemExit(code)
    finally:
        # Backend socket ownership belongs to terminal A, not this process.
        p.sockets = []
        p.close()
        if lock:
            lock.close()


if __name__ == '__main__':
    main()
