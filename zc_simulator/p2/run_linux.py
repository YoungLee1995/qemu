#!/usr/bin/env python3
"""P2 Linux toolchain driver coherent DMA + persistent MCU/NPU integration."""
import gzip
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'p1'))
from run_p1 import ROOT,SIM,SPIKE,FIRMWARE,ISA,QEMU,entry,stop,digest
HERE=Path(__file__).resolve().parent


def main():
    work=HERE/'evidence'/f'linux-{time.time_ns()}';work.mkdir(parents=True)
    sock=Path('/tmp')/f'zc-p2-linux-{os.getpid()}.sock'
    service=qemu=None;passed=False;checks={}
    module=HERE.parent/'p1/evidence/host-module'
    def check(name,value):
        checks[name]=bool(value);print(name,'PASS' if value else 'FAIL',flush=True)
        if not value:raise RuntimeError(name)
    try:
        subprocess.run(['make','-C',str(HERE/'runtime')],check=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        module=HERE.parent/'p1/evidence/host-module'
        subprocess.run([sys.executable,str(HERE.parent/'p1/build_host_demo.py')],check=True,stdout=(work/'host-build.log').open('w'),stderr=subprocess.STDOUT)
        inputs=work/'inputs';inputs.mkdir()
        for n in ['mcu.bin','npu.bin']:shutil.copy2(HERE/'runtime'/n,inputs/n)
        fw=inputs/'firmware.cfg';fw.write_text('runtime_func:1\npc_num:2\n[1, 0x40000000, ./mcu.bin]\n[16, 0x4000000000, ./npu.bin]\nddr_input_num:0\nddr_output_num:0\nl1buf_input_num:0\nl1buf_output_num:0\n')
        files=[SPIKE,FIRMWARE,fw,inputs/'mcu.bin',inputs/'npu.bin',SIM/'smart/sm_system.cfg']
        cfg=work/'backend.json';cfg.write_text(json.dumps({'spike':str(SPIKE),'firmware':str(FIRMWARE),'isa':ISA,
            'cfg':str(fw),'cwd':str(inputs),'p2':True,'sha256':{str(p):digest(p) for p in files}},indent=2))
        fs=work/'rootfs';shutil.copytree(ROOT/'experiments/qemu_stage1/rootfs',fs)
        for n in ['dev','sys','proc','tmp']: (fs/n).mkdir(exist_ok=True)
        shutil.copy2(module/'ssv_test_driver.ko',fs)
        subprocess.run(['gcc','-static','-O2','-Wall','-Wextra',str(HERE/'guest-dma.c'),'-o',str(fs/'guest-dma')],check=True)
        (fs/'init').write_text('''#!/bin/busybox sh
/bin/busybox mount -t proc proc /proc
/bin/busybox mount -t sysfs sysfs /sys
/bin/busybox mount -t devtmpfs devtmpfs /dev
/bin/busybox insmod /ssv_test_driver.ko || exit 1
/guest-dma
echo P2_GUEST_EXIT=$?
/bin/busybox poweroff -f
''');(fs/'init').chmod(0o755)
        archive=b''.join(entry(str(p.relative_to(fs)),b'' if p.is_dir() else p.read_bytes(),p.stat().st_mode,i)
            for i,p in enumerate(sorted(fs.rglob('*')),1) if not p.is_symlink())
        initrd=work/'initramfs.cpio.gz';initrd.write_bytes(gzip.compress(archive+entry('TRAILER!!!',b'',0,999999),mtime=0))
        kernel=ROOT/'experiments/qemu_stage1/assets/vmlinuz-lts'
        check('pinned_kernel',digest(kernel)=='9d6eff6e7bcd5f5fba623f7a44c565ca6914da2b91110a8dc72a3a63cb088693')
        with (work/'service.log').open('w') as log:
            service=subprocess.Popen([sys.executable,str(ROOT/'simulator/tools/p1/service.py'),'--socket',str(sock),'--config',str(cfg),'--work',str(work/'service')],stdout=log,stderr=subprocess.STDOUT)
        end=time.monotonic()+5
        while not sock.exists() and service.poll() is None and time.monotonic()<end:time.sleep(.01)
        check('service_listening',sock.exists())
        cmd=[str(QEMU),'-machine','q35','-accel','tcg','-m','512M','-smp','1','-nodefaults','-display','none',
            '-serial','stdio','-monitor','none','-no-reboot','-icount','shift=3,align=off,sleep=off',
            '-kernel',str(kernel),'-initrd',str(initrd),'-append','console=ttyS0 rdinit=/init intel_iommu=on iommu.strict=1 panic=-1',
            '-global','q35-pcihost.pci-hole64-size=64G','-device','intel-iommu,intremap=on,caching-mode=on',
            '-device','pcie-root-port,id=rp,chassis=1','-device','x3130-upstream,id=sw,bus=rp',
            '-device','xio3130-downstream,id=dp,bus=sw,chassis=2','-device',
            f'zc-pcie-p1,p2=on,p2-trace=on,bus=dp,backend-socket={sock},config-sha256={digest(cfg)}']
        (work/'command.json').write_text(json.dumps(cmd,indent=2))
        with (work/'linux.log').open('w') as log:
            qemu=subprocess.Popen(cmd,stdout=log,stderr=subprocess.STDOUT)
            check('linux_exit',qemu.wait(timeout=180)==0)
        log=(work/'linux.log').read_text(errors='replace')
        check('no_panic','Kernel panic' not in log)
        check('four_modes_real_dma','P2_LINUX_PASS tasks=5 modes=4 coherent_iommu=1 msix=1' in log and 'P2_GUEST_EXIT=0' in log)
        check('file_ipc_crc_result_mcu_irq','P2_FILE_IPC_PASS coherent_iova=1 crc32=1 result_bytes=12345 mcu_irq=1' in log)
        events=list(map(json.loads,(work/'service/service.jsonl').read_text().splitlines()))
        check('one_persistent_spike',len([e for e in events if e['event']=='spawn'])==1)
        check('iommu_enabled','DMAR: Intel(R) Virtualization Technology for Directed I/O' in log)
        passed=True
    finally:
        stop(qemu);stop(service);sock.unlink(missing_ok=True)
        (work/'manifest.json').write_text(json.dumps({str(p):digest(p) for p in [HERE/'guest-dma.c',HERE/'run_linux.py',SPIKE,QEMU,module/'ssv_test_driver.ko'] if p.is_file()},indent=2))
        (work/'results.json').write_text(json.dumps({'pass':passed,'p2_full_exit':False,'checks':checks},indent=2))
        print('Evidence:',work,flush=True)

if __name__=='__main__':main()
