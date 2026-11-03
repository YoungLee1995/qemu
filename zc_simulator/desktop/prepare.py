#!/usr/bin/env python3
"""Prepare only local desktop experiment artifacts, never the published bundle."""
import hashlib
import fcntl
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess

from paths import REPO, SOURCE, STATE

HERE = STATE


def main():
    HERE.mkdir(parents=True, exist_ok=True)
    lock = (HERE / 'desktop.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(
            f'桌面数据目录正在使用：{HERE}\n'
            '已有桌面或初始化进程持有锁，不能在运行期间重建系统盘/seed。\n'
            '查看已有桌面：python3 zc_simulator/desktop/control.py status\n'
            '直接连接：python3 zc_simulator/desktop/terminal.py\n'
            '确需重新初始化时，先运行 python3 zc_simulator/desktop/control.py stop，\n'
            '等待原 desktop.py 进程退出，再执行 prepare.py。不要删除 desktop.lock。')
    tools = HERE / 'tools/root/usr/bin'
    qemu_img = shutil.which('qemu-img') or str(tools / 'qemu-img')
    genisoimage = shutil.which('genisoimage') or str(tools / 'genisoimage')
    env = dict(os.environ)
    local_lib = HERE / 'tools/root/usr/lib/x86_64-linux-gnu'
    if local_lib.is_dir():
        env['LD_LIBRARY_PATH'] = str(local_lib) + ':' + env.get('LD_LIBRARY_PATH', '')
    base = HERE / 'ubuntu-24.04-base.qcow2'
    partial = base.with_suffix('.qcow2.part')
    expected = next(line.split()[0] for line in (HERE / 'SHA256SUMS').read_text().splitlines()
                    if line.split()[-1].lstrip('*') == 'noble-server-cloudimg-amd64.img')
    candidate = base if base.exists() else partial
    with candidate.open('rb') as f:
        h = hashlib.sha256()
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    if h.hexdigest() != expected:
        raise RuntimeError('Ubuntu cloud image digest mismatch or download incomplete')
    if not base.exists():
        partial.rename(base)
    disk = HERE / 'ubuntu-24.04-desktop.qcow2'
    if not disk.exists():
        subprocess.run([qemu_img, 'create', '-f', 'qcow2', '-F', 'qcow2',
                        '-b', base.name, str(disk), '32G'], env=env, check=True)
    key = HERE / 'ssh_key'
    if not key.exists():
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)], check=True)
    credentials = HERE / 'credentials.json'
    if credentials.exists():
        password = json.loads(credentials.read_text())['password']
    else:
        password = secrets.token_urlsafe(18)
        fd = os.open(credentials, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(dict(user='t100', password=password), f, indent=2)
    share = HERE / 'share'
    (share / 'driver').mkdir(parents=True, exist_ok=True)
    for name in ('driver/ssv_test_driver.c', 'driver/Makefile',
                 'my-task.c', 'desktop-info.sh', 'CUSTOM_TASK.md'):
        target = share / name
        if target.is_symlink():
            target.unlink()
        shutil.copy2(SOURCE / 'share' / name, target)
    shutil.copy2(REPO / 'zc_simulator/p4/guest-cluster.c', share / 'guest-cluster.c')
    for name in ('zc_host.h', 'zc_comm.h'):
        shutil.copy2(REPO / 'zc_simulator/tools/p5' / name, share / name)
    shutil.copy2(SOURCE / 'device_reload.py', share / 'device_reload.py')
    setup = '''#!/bin/bash
set -euo pipefail
exec > >(tee -a /var/log/t100-desktop-setup.log) 2>&1
export DEBIAN_FRONTEND=noninteractive
echo "START $(date -Is)"
mkdir -p /mnt/hostshare
mountpoint -q /mnt/hostshare || mount -t 9p -o trans=virtio,version=9p2000.L hostshare /mnt/hostshare
apt-get update
apt-get install -y --no-install-recommends xfce4 xfce4-terminal dbus-x11 xrdp xorgxrdp xterm pciutils build-essential "linux-headers-$(uname -r)" x11-utils
usermod -aG ssl-cert xrdp
printf 'startxfce4\n' > /home/t100/.xsession
chown t100:t100 /home/t100/.xsession
mkdir -p /opt/t100/driver
cp /mnt/hostshare/driver/* /opt/t100/driver/
make -C /lib/modules/$(uname -r)/build M=/opt/t100/driver modules
install -D -m 644 /opt/t100/driver/ssv_test_driver.ko /lib/modules/$(uname -r)/extra/ssv_test_driver.ko
depmod -a
echo ssv_test_driver > /etc/modules-load.d/t100.conf
modprobe ssv_test_driver
gcc -O2 -Wall /mnt/hostshare/guest-cluster.c -o /opt/t100/guest-cluster
mkdir -p /home/t100/Desktop
cat > /home/t100/Desktop/PCIe.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=PCIe devices
Exec=xfce4-terminal --hold -x sh -c "lspci -nn; echo; ls -l /dev/test_dma_*"
Terminal=false
Icon=utilities-terminal
EOF
cat > /home/t100/Desktop/P2P.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=Eight-card DMA and P2P test
Exec=xfce4-terminal --hold -x sudo /opt/t100/guest-cluster
Terminal=false
Icon=utilities-terminal
EOF
chmod +x /home/t100/Desktop/*.desktop
chown -R t100:t100 /home/t100/Desktop
systemctl enable --now xrdp
touch /var/lib/t100-desktop-ready
echo "T100_DESKTOP_READY $(date -Is)"
'''
    seed = HERE / 'seed'
    seed.mkdir(exist_ok=True)
    user_data = {
        'hostname': 't100-eight-card',
        'manage_etc_hosts': True,
        'ssh_pwauth': False,
        'users': [{'name': 't100', 'shell': '/bin/bash', 'groups': ['sudo'],
                   'sudo': ['ALL=(ALL) NOPASSWD:ALL'], 'lock_passwd': False,
                   'passwd': subprocess.check_output(['openssl', 'passwd', '-6', '-stdin'],
                                                     input=password + '\n', text=True).strip(),
                   'ssh_authorized_keys': [key.with_suffix('.pub').read_text().strip()]}],
        'write_files': [
            {'path': '/usr/local/sbin/t100-desktop-setup', 'permissions': '0755', 'content': setup},
            {'path': '/etc/default/grub.d/90-t100.cfg', 'content': 'GRUB_CMDLINE_LINUX_DEFAULT="console=ttyS0 intel_iommu=on iommu.strict=1 pci=realloc=on"\n'},
            {'path': '/etc/fstab', 'append': True, 'content': '\nhostshare /mnt/hostshare 9p trans=virtio,version=9p2000.L,nofail 0 0\n'},
        ],
        'runcmd': [
            ['update-grub'],
            ['sh', '-c', 'test -e /var/lib/t100-desktop-ready || systemctl is-active --quiet t100-desktop-setup || systemd-run --unit=t100-desktop-setup /usr/local/sbin/t100-desktop-setup'],
        ],
    }
    # JSON is valid YAML and cloud-init accepts it after the cloud-config header.
    user_path = seed / 'user-data'
    user_path.write_text('#cloud-config\n' + json.dumps(user_data, indent=2) + '\n')
    user_path.chmod(0o600)
    (seed / 'meta-data').write_text('instance-id: t100-desktop-v1\nlocal-hostname: t100-eight-card\n')
    dns = [line.split()[1] for line in Path('/etc/resolv.conf').read_text().splitlines()
           if line.startswith('nameserver ') and not line.split()[1].startswith('127.')]
    nic = dict(match={'name': 'en*'}, dhcp4=True)
    if dns:
        nic.update(nameservers={'addresses': dns}, **{'dhcp4-overrides': {'use-dns': False}})
    (seed / 'network-config').write_text(json.dumps(dict(version=2, ethernets={'ethernet': nic}), indent=2))
    subprocess.run([genisoimage, '-quiet', '-output', str(HERE / 'seed.iso'),
                    '-volid', 'cidata', '-joliet', '-rock',
                    str(user_path), str(seed / 'meta-data'), str(seed / 'network-config')], check=True)
    (HERE / 'seed.iso').chmod(0o600)
    print('Prepared persistent Ubuntu disk, cloud-init seed, credentials, and shared workspace.')


if __name__ == '__main__':
    main()
