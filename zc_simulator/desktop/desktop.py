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
from reload_controller import ReloadController

HERE = STATE
sys.path.insert(0, str(REPO / 'zc_simulator/p5'))
from linux_bundle import BundlePlatform, validate_bundle


def readiness_command(state):
    """Check live guest services, rather than just the forwarded TCP port."""
    host = state['bind']
    if host == '0.0.0.0':
        host = '127.0.0.1'
    check = ('test -e /var/lib/t100-desktop-ready && '
             'test -x /opt/t100/guest-cluster && '
             'systemctl is-active --quiet xrdp && '
             f'test "$(find /dev -maxdepth 1 -type c -name \'test_dma_*\' | wc -l)" -eq {state["cards"]}')
    for card in range(state['cards']):
        check += f' && test -c /dev/test_dma_{card}'
    return ['ssh', '-i', str(HERE / 'ssh_key'), '-p', str(state['ssh_port']),
            '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
            '-o', 'StrictHostKeyChecking=accept-new',
            '-o', 'UserKnownHostsFile=' + str(HERE / 'known_hosts'),
            '-o', 'ConnectTimeout=5', '-o', 'ConnectionAttempts=1',
            't100@' + host, check]


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
    with (HERE / 'desktop.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            ap.exit(1, f'桌面数据目录正在使用：{HERE}\n'
                    '已有桌面或初始化进程持有锁，不要重复启动。\n'
                    '检查：python3 zc_simulator/desktop/control.py status\n'
                    '连接：python3 zc_simulator/desktop/terminal.py\n'
                    '如需重启，先用 control.py stop 正常关机，等待原启动进程退出。\n'
                    '若正在运行 prepare.py，请等待初始化完成。不要删除 desktop.lock。\n')
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
        supervisor_log = (work / 'supervisor.log').open('a', buffering=1)

        def detach_console(signum=None, frame=None):
            # A closed PTY may raise EIO on future writes even with SIGHUP
            # ignored. Redirect the actual descriptors, including stderr.
            os.dup2(supervisor_log.fileno(), 1)
            os.dup2(supervisor_log.fileno(), 2)

        def announce(message, **kwargs):
            try:
                print(message, **kwargs)
            except OSError:
                detach_console()
                print(message, **kwargs)

        def ignore_suspend(signum, frame):
            try:
                os.write(2, '\nCtrl+Z 已忽略，虚拟机继续运行；关机请执行 control.py stop。\n'.encode())
            except OSError:
                detach_console()

        def stop(signum, frame):
            nonlocal stopping
            stopping = True
            if p.qemu is not None and p.qemu.poll() is None:
                p.qemu.terminate()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        # Ignored dispositions survive exec: every backend (including its later
        # Spike children) and QEMU must ignore terminal-generated SIGTSTP too.
        # Install the supervisor's explanatory handler only after those forks.
        signal.signal(signal.SIGTSTP, signal.SIG_IGN)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        sockets = tempfile.TemporaryDirectory(prefix="zc-desktop-")
        probe = None
        reload_controller = None
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
            signal.signal(signal.SIGTSTP, ignore_suspend)
            signal.signal(signal.SIGHUP, detach_console)
            state = dict(supervisor_pid=os.getpid(), qemu_pid=p.qemu.pid,
                         work=str(work), ssh_port=args.ssh_port, rdp_port=args.rdp_port,
                         cards=args.cards, disk=str(disk), bind=args.bind, qmp=str(qmp))
            (HERE / 'active.json').write_text(json.dumps(state, indent=2))
            reload_controller = ReloadController(p, state)
            announce(json.dumps(state, indent=2), flush=True)
            announce('Ctrl+Z 已忽略；关闭终端后虚拟机继续运行。正常关机请执行 control.py stop。', flush=True)
            announce('正在等待 Ubuntu SSH、设备驱动和 xrdp 就绪；首次启动需要联网安装软件。', flush=True)
            announce(f'启动日志：{work / "serial.log"}', flush=True)
            reload_warning = False
            ready = False
            next_probe = 0
            next_notice = time.monotonic() + 30
            probe_started = 0
            while p.qemu.poll() is None:
                if any(proc.poll() is not None for proc in p.services.values()):
                    raise RuntimeError('a backend service exited; see run logs')
                if not reload_warning and not reload_controller.worker.is_alive():
                    reload_warning = True
                    announce('换镜像控制器已退出，请检查 supervisor.log；当前 VM 继续运行。', flush=True)
                if p.fabric_process.poll() is not None:
                    raise RuntimeError('fabric service exited; see run logs')
                now = time.monotonic()
                if not ready:
                    if probe is not None:
                        result = probe.poll()
                        if result is None and now - probe_started > 15:
                            probe.kill()
                            probe.wait()
                            result = probe.returncode
                        if result is not None:
                            probe = None
                            next_probe = now + 5
                            if result == 0:
                                ready = True
                                state['ready'] = True
                                (HERE / 'active.json').write_text(json.dumps(state, indent=2))
                                announce(f'\nDESKTOP_READY：Ubuntu 已就绪（SSH、{args.cards} 个设备节点、xrdp）。\n'
                                      '请在另一个宿主终端执行：\n'
                                      '  python3 zc_simulator/desktop/terminal.py\n'
                                      '可关闭当前终端，虚拟机继续运行。', flush=True)
                    if not ready and probe is None and now >= next_probe:
                        with (work / 'readiness.log').open('a') as log:
                            probe = subprocess.Popen(readiness_command(state), stdout=log, stderr=log,
                                                     start_new_session=True)
                        probe_started = now
                    if not ready and now >= next_notice:
                        announce('仍在等待 Ubuntu 就绪；可查看 serial.log 和 readiness.log。', flush=True)
                        next_notice = now + 30
                time.sleep(1)
            if p.qemu.returncode and not stopping:
                raise RuntimeError(f'QEMU exited {p.qemu.returncode}; see {work / "qemu.log"}')
        finally:
            if reload_controller is not None:
                reload_controller.close()
            if probe is not None and probe.poll() is None:
                probe.kill()
                probe.wait()
            p.close()
            sockets.cleanup()
            # active.json is useful for postmortem; mark processes as stopped.
            state_path = HERE / 'active.json'
            if state_path.exists():
                state = json.loads(state_path.read_text())
                state['stopped'] = True
                state['ready'] = False
                state_path.write_text(json.dumps(state, indent=2))
            supervisor_log.close()


if __name__ == '__main__':
    main()
