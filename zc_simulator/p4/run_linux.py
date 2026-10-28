#!/usr/bin/env python3
"""Original host-demo driver, eight Linux-assigned full BAR sets and tasks."""
import gzip
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
from cluster_platform import Platform,HERE,ROOT,QEMU,digest
sys.path.insert(0,str(HERE.parent/'p1'))
from run_p1 import entry


def main():
    work=HERE/'evidence'/f'linux-{time.time_ns()}';p=Platform(work);checks={};passed=False
    def check(name,v):
        checks[name]=bool(v);print(name,'PASS' if v else 'FAIL',flush=True)
        if not v:raise RuntimeError(name)
    try:
        p.spec['peer_base']=0x100000000;p.spec['peer_size']=0xff00000000
        (work/'platform.json').write_text(json.dumps(p.spec,indent=2));p.prepare()
        module=HERE.parent/'p1/evidence/host-module'
        with (work/'host-build.log').open('w') as log:subprocess.run([sys.executable,str(HERE.parent/'p1/build_host_demo.py')],check=True,stdout=log,stderr=subprocess.STDOUT)
        fs=work/'rootfs';shutil.copytree(ROOT/'experiments/qemu_stage1/rootfs',fs)
        for name in ('dev','sys','proc','tmp'):(fs/name).mkdir(exist_ok=True)
        shutil.copy2(module/'ssv_test_driver.ko',fs)
        subprocess.run(['gcc','-static','-O2','-Wall','-Wextra',str(HERE/'guest-cluster.c'),'-o',str(fs/'guest-cluster')],check=True)
        (fs/'init').write_text('''#!/bin/busybox sh
/bin/busybox mount -t proc proc /proc
/bin/busybox mount -t sysfs sysfs /sys
/bin/busybox mount -t devtmpfs devtmpfs /dev
/bin/busybox insmod /ssv_test_driver.ko || exit 1
/guest-cluster
echo P4_GUEST_EXIT=$?
/bin/busybox poweroff -f
''');(fs/'init').chmod(0o755)
        archive=b''.join(entry(str(f.relative_to(fs)),b'' if f.is_dir() else f.read_bytes(),f.stat().st_mode,i)
            for i,f in enumerate(sorted(fs.rglob('*')),1) if not f.is_symlink())
        initrd=work/'initramfs.cpio.gz';initrd.write_bytes(gzip.compress(archive+entry('TRAILER!!!',b'',0,999999),mtime=0))
        kernel=ROOT/'experiments/qemu_stage1/assets/vmlinuz-lts'
        check('pinned_kernel',digest(kernel)=='9d6eff6e7bcd5f5fba623f7a44c565ca6914da2b91110a8dc72a3a63cb088693')
        args=[str(QEMU),'-machine','q35','-accel','tcg','-m','512M','-smp','4,sockets=1,cores=4,threads=1','-nodefaults','-display','none',
              '-serial','stdio','-monitor','none','-no-reboot','-icount','shift=3,align=off,sleep=off',
              '-kernel',str(kernel),'-initrd',str(initrd),'-append','console=ttyS0 rdinit=/init intel_iommu=on iommu.strict=1 pci=realloc=on panic=-1',
              '-global','q35-pcihost.pci-hole64-size=512G','-device','intel-iommu,intremap=on,caching-mode=on']+p.device_args()
        (work/'qemu-command.json').write_text(json.dumps(args,indent=2))
        with (work/'linux.log').open('w') as log:
            p.qemu=subprocess.Popen(args,stdout=log,stderr=subprocess.STDOUT)
            check('linux_exit',p.qemu.wait(timeout=900)==0)
        text=(work/'linux.log').read_text(errors='replace')
        check('no_kernel_panic','Kernel panic' not in text)
        check('guest_all_tasks','P4_LINUX_PASS cards=8 host_tasks=32 peer_tasks=224 directed_pairs=56 modes=4 file_ipc=8' in text and 'P4_GUEST_EXIT=0' in text)
        check('eight_probe',text.count('P4_PROBE card=')==8)
        check('all24_full_bars',text.count('P4_BAR card=')==24)
        check('iommu_enabled','DMAR: Intel(R) Virtualization Technology for Directed I/O' in text)
        for i in range(8):check(f'card{i}_one_persistent_spike',len(p.spawns(i))==1)
        passed=True
    finally:
        p.close();(work/'results.json').write_text(json.dumps(dict(passed=passed,checks=checks),indent=2))
        print('Evidence:',work,flush=True)

if __name__=='__main__':main()
