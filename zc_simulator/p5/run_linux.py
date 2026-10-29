#!/usr/bin/env python3
"""Original host-demo driver, eight Linux-assigned full BAR sets and tasks."""
import json
from pathlib import Path
import subprocess
import time
from linux_bundle import BundlePlatform, select_bundle
from cluster_platform import digest
HERE=Path(__file__).resolve().parent
QEMU=HERE.parents[1]/'build/qemu-system-x86_64'


def main():
    import argparse
    ap=argparse.ArgumentParser();ap.add_argument('--cards',type=int,choices=(2,8),default=8)
    ap.add_argument('--runtime',choices=('auto','source','prebuilt'),default='auto',
                    help='auto: build when simulator sources exist, otherwise use simu/current')
    ap.add_argument('--prepare-only',action='store_true',help='Build/validate the runtime bundle and exit')
    options=ap.parse_args()
    work=HERE/'evidence'/f'linux-{options.cards}-{time.time_ns()}';work.mkdir(parents=True)
    p=None;checks={};passed=False
    def check(name,v):
        checks[name]=bool(v);print(name,'PASS' if v else 'FAIL',flush=True)
        if not v:raise RuntimeError(name)
    try:
        bundle=select_bundle(work,options.runtime)
        if options.prepare_only:
            passed=True
            return
        p=BundlePlatform(work,options.cards,bundle)
        p.spec['peer_base']=0x100000000;p.spec['peer_size']=0xff00000000
        (work/'platform.json').write_text(json.dumps(p.spec,indent=2));p.prepare()
        initrd=bundle/f'guest/initramfs-{options.cards}.cpio.gz'
        kernel=bundle/'guest/vmlinuz-lts'
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
        check('guest_all_tasks',f'P5_BASE_PASS cards={options.cards} ' in text and 'P5_C_LIBRARY_PASS' in text and 'P4_GUEST_EXIT=0' in text and text.count('P5_ASYNC_ROUND')==3)
        check('eight_probe',text.count('P4_PROBE card=')==options.cards)
        check('all24_full_bars',text.count('P4_BAR card=')==options.cards*3)
        check('iommu_enabled','DMAR: Intel(R) Virtualization Technology for Directed I/O' in text)
        for i in range(options.cards):check(f'card{i}_one_persistent_spike',len(p.spawns(i))==1)
        passed=True
    finally:
        if p is not None:p.close()
        (work/'results.json').write_text(json.dumps(dict(passed=passed,cards=options.cards,
            prepare_only=options.prepare_only,checks=checks),indent=2))
        print('Evidence:',work,flush=True)

if __name__=='__main__':main()
