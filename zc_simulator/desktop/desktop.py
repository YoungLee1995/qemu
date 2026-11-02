#!/usr/bin/env python3
"""Local desktop experiment; uses the published P5 backends unchanged."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

from paths import REPO, SOURCE, STATE

HERE = STATE
sys.path.insert(0, str(REPO / 'zc_simulator/p5'))
from linux_bundle import BundlePlatform, validate_bundle


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--cards', type=int, choices=(1, 2, 8), default=8)
    ap.add_argument('--ssh-port', type=int, default=2222)
    ap.add_argument('--rdp-port', type=int, default=13389)
    ap.add_argument('--bind', default='127.0.0.1',
                    help='Address for SSH/RDP forwards; use the container bridge IP for a host SSH tunnel')
    ap.add_argument('--memory', default='4G')
    args = ap.parse_args()
    HERE.mkdir(parents=True, exist_ok=True)
    # Avoid two VMs opening the same persistent disk or forwarding ports.
    with (HERE / 'desktop.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        disk = HERE / 'ubuntu-24.04-desktop.qcow2'
        seed = HERE / 'seed.iso'
        for path in (disk, seed):
            if not path.is_file():
                ap.error(f'missing {path}; run prepare.py first')
        bundle = (REPO / 'zc_simulator/simu/current').resolve()
        manifest = validate_bundle(bundle)
        work = HERE / 'runs' / f'cards-{args.cards}-{time.time_ns()}'
        work.mkdir(parents=True)
        p = BundlePlatform(work, args.cards, bundle)
        p.spec['peer_base'] = 0x100000000
        p.spec['peer_size'] = 0xff00000000
        (work / 'platform.json').write_text(json.dumps(p.spec, indent=2))
        (work / 'bundle.json').write_text(json.dumps(dict(path=str(bundle), manifest=manifest), indent=2))
        stopping = False

        def stop(signum, frame):
            nonlocal stopping
            stopping = True
            if p.qemu is not None and p.qemu.poll() is None:
                p.qemu.terminate()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        sockets = tempfile.TemporaryDirectory(prefix="zc-desktop-")
        try:
            p.prepare()
            qmp = Path(sockets.name) / 'qmp.sock'
            serial = Path(sockets.name) / 'serial.sock'
            command = [
                str(REPO / 'build/qemu-system-x86_64'),
                '-L', str(REPO / 'pc-bios'),
                '-machine', 'q35', '-accel', 'tcg', '-cpu', 'max',
                '-m', args.memory, '-smp', '4,sockets=1,cores=4,threads=1',
                '-nodefaults', '-display', 'none', '-monitor', 'none',
                '-no-reboot', '-boot', 'order=c',
                '-chardev', f'socket,id=serial0,path={serial},server=on,wait=off,logfile={work / "serial.log"}',
                '-serial', 'chardev:serial0',
                '-qmp', f'unix:{qmp},server=on,wait=off',
                '-global', 'q35-pcihost.pci-hole64-size=512G',
                '-device', 'intel-iommu,intremap=on,caching-mode=on',
                '-drive', f'file={disk},format=qcow2,if=none,id=osdisk',
                '-device', 'virtio-blk-pci,drive=osdisk,addr=4,iommu_platform=on,disable-legacy=on',
                '-drive', f'file={seed},format=raw,media=cdrom,readonly=on',
                '-netdev', f'user,id=net0,hostfwd=tcp:{args.bind}:{args.ssh_port}-:22,hostfwd=tcp:{args.bind}:{args.rdp_port}-:3389',
                '-device', 'virtio-net-pci,netdev=net0,addr=5,romfile=,iommu_platform=on,disable-legacy=on',
                '-fsdev', f'local,id=share,path={HERE / "share"},security_model=none',
                '-device', 'virtio-9p-pci,fsdev=share,mount_tag=hostshare,addr=6,iommu_platform=on,disable-legacy=on',
            ] + p.device_args()
            (work / 'qemu-command.json').write_text(json.dumps(command, indent=2))
            with (work / 'qemu.log').open('w') as log:
                p.qemu = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
            state = dict(supervisor_pid=os.getpid(), qemu_pid=p.qemu.pid,
                         work=str(work), ssh_port=args.ssh_port, rdp_port=args.rdp_port,
                         cards=args.cards, disk=str(disk), bind=args.bind)
            (HERE / 'active.json').write_text(json.dumps(state, indent=2))
            print(json.dumps(state, indent=2), flush=True)
            while p.qemu.poll() is None:
                if any(proc.poll() is not None for proc in p.services.values()):
                    raise RuntimeError('a backend service exited; see run logs')
                if p.fabric_process.poll() is not None:
                    raise RuntimeError('fabric service exited; see run logs')
                time.sleep(1)
            if p.qemu.returncode and not stopping:
                raise RuntimeError(f'QEMU exited {p.qemu.returncode}; see {work / "qemu.log"}')
        finally:
            p.close()
            sockets.cleanup()
            # active.json is useful for postmortem; mark processes as stopped.
            state_path = HERE / 'active.json'
            if state_path.exists():
                state = json.loads(state_path.read_text())
                state['stopped'] = True
                state_path.write_text(json.dumps(state, indent=2))


if __name__ == '__main__':
    main()
