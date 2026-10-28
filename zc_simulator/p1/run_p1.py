#!/usr/bin/env python3
"""Build and run internal P1 shared-DDR/mailbox Linux and lifecycle checks."""
import argparse
import gzip
import hashlib
import json
import mmap
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import time
import threading

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SIM = ROOT / 'simulator/riscv-isa-sim'
ISA = 'rv64imafdcv_zvl512b_zve32d_zfh_zvfh_zfbfmin_zvfbfmin_zba_zbb_zbs_zicond'
CONTROL = 0x100100000
FIRMWARE = ROOT / 'simulator/riscv-tests/firmware.riscv'
SPIKE = SIM / 'build/spike'
QEMU = ROOT / 'qemu/build/qemu-system-x86_64'


def digest(p):
    h=hashlib.sha256()
    with p.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''): h.update(block)
    return h.hexdigest()


def entry(name,data,mode,inode):
    n=name.encode()+b'\0'
    a=b'070701'+b''.join(f'{v:08x}'.encode() for v in
        [inode,mode,0,0,1,0,len(data),0,0,0,0,len(n),0])+n
    a+=b'\0'*(-len(a)%4)
    return a+data+b'\0'*(-len(data)%4)


class Backing:
    def __init__(self):
        self.fds=[];self.maps=[];self.watchers=[]
        for name,size in [('ddr0',1<<30),('ddr1',1<<38),('mailbox',4096)]:
            fd=os.memfd_create('zc-p1-'+name,os.MFD_CLOEXEC|os.MFD_ALLOW_SEALING)
            os.ftruncate(fd,size)
            import fcntl
            fcntl.fcntl(fd,fcntl.F_ADD_SEALS,fcntl.F_SEAL_GROW|fcntl.F_SEAL_SHRINK|fcntl.F_SEAL_SEAL)
            self.fds.append(fd)
            self.maps.append(mmap.mmap(fd,size))
        self.epoch=0
    def initialize(self):
        # Caller must have reaped old backend before reuse.
        if any(w.is_alive() for w in self.watchers):
            raise RuntimeError('cannot reuse memory while backend watcher is active')
        self.maps[2][:]=bytes(4096)
        self.maps[2][0x400:0x40c]=struct.pack('<III',self.epoch,self.epoch,0)
        self.maps[1][CONTROL:CONTROL+65536]=bytes(65536)
    def prepare(self,work):
        work.mkdir(parents=True,exist_ok=True)
        for name in ('mcu.bin','npu.bin'): shutil.copy2(HERE/'runtime'/name,work/name)
        shutil.copytree(ROOT/'moe_bf16_sint4_33x29x34_fix/data',work/'data',dirs_exist_ok=True)
        (work/'firmware.cfg').write_text('''runtime_func:1
pc_num:2
[1, 0x40000000, ./mcu.bin]
[16, 0x4000000000, ./npu.bin]
ddr_input_num:0
ddr_output_num:0
l1buf_input_num:4
[0x10000000, ./data/act_l1.bin]
[0x10008400, ./data/wgt_l1.bin]
[0x10008a00, ./data/wgt_scale_l1.bin]
[0x10009000, ./data/moe_index_l1.bin]
l1buf_output_num:0
''')
    def start(self,work,ready_timeout=20):
        self.prepare(work)
        log=(work/'spike.log').open('w')
        cmd=[str(SPIKE),f'--p1-ddr0-fd={self.fds[0]}',f'--p1-ddr1-fd={self.fds[1]}',
             f'--isa={ISA}','--runtime-func',str(FIRMWARE),'-cfg',str(work/'firmware.cfg')]
        (work/'command.json').write_text(json.dumps(cmd,indent=2))
        proc=subprocess.Popen(cmd,cwd=work,env=dict(os.environ,ZC_P1_MAILBOX_FD=str(self.fds[2])),
                              pass_fds=self.fds,stdout=log,stderr=subprocess.STDOUT)
        log.close()
        self.last_pid=proc.pid
        deadline=time.monotonic()+ready_timeout
        while struct.unpack_from('<II',self.maps[1],CONTROL+16)!=(1,1):
            if proc.poll() is not None or time.monotonic()>deadline:
                stop(proc);raise RuntimeError(f'software ready failed: {work}')
            time.sleep(.005)
        struct.pack_into('<II',self.maps[2],0x404,self.epoch,1)
        def watch():
            proc.wait()
            # The launcher owns lifetime: publish death before allowing reuse.
            struct.pack_into('<I',self.maps[2],0x408,0)
        watcher=threading.Thread(target=watch,daemon=True)
        self.watchers.append(watcher);watcher.start()
        return proc
    def device(self):
        return f'zc-pcie-p1,ddr0-fd={self.fds[0]},ddr1-fd={self.fds[1]},mailbox-fd={self.fds[2]}'
    def close(self):
        for watcher in self.watchers:watcher.join(timeout=5)
        for m in self.maps:m.close()
        for fd in self.fds:os.close(fd)


def stop(p):
    if p and p.poll() is None:
        p.terminate()
        try:p.wait(timeout=5)
        except subprocess.TimeoutExpired:p.kill();p.wait()


def connect(path,proc):
    s=socket.socket(socket.AF_UNIX);s.settimeout(10)
    deadline=time.monotonic()+10
    while True:
        try:s.connect(str(path));return s.makefile('rwb',buffering=0)
        except (FileNotFoundError,ConnectionRefusedError):
            if proc.poll() is not None or time.monotonic()>deadline:raise
            time.sleep(.02)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skip-build',action='store_true')
    parser.add_argument('--host-demo',action='store_true',help='Build/load adapted toolchain demo with pinned guest headers')
    args=parser.parse_args()
    work=HERE/'evidence'/f'run-{time.time_ns()}'
    work.mkdir(parents=True)
    result={'p1_full_exit':False,'profile':'shared-fd-mailbox-experiment','checks':{}}
    def run(name,cmd,cwd=ROOT,timeout=300):
        with (work/f'{name}.log').open('w') as log:
            subprocess.run(list(map(str,cmd)),cwd=cwd,check=True,stdout=log,stderr=subprocess.STDOUT,timeout=timeout)
    def check(name,value):
        result['checks'][name]=bool(value)
        if not value:raise RuntimeError(name)
        print(name,'PASS',flush=True)
    backing=None;backend=None;qemu=None
    try:
        if not args.skip_build:
            run('build-spike',['make','-C',SIM/'build','-j16','spike'],timeout=900)
            run('build-qemu',['ninja','-C',ROOT/'qemu/build','-j16','qemu-system-x86_64'],timeout=900)
            run('build-firmware',['make','-C',ROOT/'simulator/riscv-tests','firmware.riscv'])
        if args.host_demo:
            run('build-host-demo',[str(Path(os.sys.executable)),HERE/'build_host_demo.py'],timeout=900)
        run('build-runtime',['make','-C',HERE/'runtime','-B'])
        fs=work/'rootfs'
        shutil.copytree(ROOT/'experiments/qemu_stage1/rootfs',fs)
        for name in ('proc','sys','dev','tmp','bin'): (fs/name).mkdir(exist_ok=True)
        run('build-guest',['gcc','-static','-O2','-Wall','-Wextra',HERE/'guest-test.c','-o',fs/'guest-test'])
        shutil.copy2(ROOT/'simulator/riscv-tests/packet_runtime_test/tensor-args.bin',fs)
        # Content-addressed baseline, never silently choose a different output.
        candidates=sorted((ROOT/'experiments/single_die_packet').glob('run-*/moe/data/result_l1_spike.bin'))
        reference=next(p for p in candidates if digest(p)=='37121ee2cd6afaed02f18587ed683d6fb5d5c4a6cd3b078f682a77891fe7e015')
        shutil.copy2(reference,fs/'legacy-result.bin')
        (fs/'init').write_text('''#!/bin/busybox sh
/bin/busybox mount -t proc proc /proc
/bin/busybox mount -t sysfs sysfs /sys
/bin/busybox mount -t devtmpfs devtmpfs /dev
/guest-test
echo GUEST_RESULT=$?
/bin/busybox poweroff -f
''');(fs/'init').chmod(0o755)
        if args.host_demo:
            module=HERE/'evidence/host-module'
            shutil.copy2(module/'ssv_test_driver.ko',fs)
            shutil.copy2(module/'ssv_test_app',fs)
            run('build-host-probe',['gcc','-static','-O2','-Wall','-Wextra',HERE/'host-probe.c','-o',fs/'host-probe'])
            init=(fs/'init').read_text().replace('/guest-test\n',
                '/bin/busybox insmod /ssv_test_driver.ko || exit 1\n'
                '/host-probe || exit 1\n'
                '/ssv_test_app list-devices || exit 1\n'
                '/guest-test\n')
            (fs/'init').write_text(init)
        archive=b''.join(entry(str(p.relative_to(fs)),b'' if p.is_dir() else p.read_bytes(),p.stat().st_mode,i)
                         for i,p in enumerate(sorted(fs.rglob('*')),1) if not p.is_symlink())
        initrd=work/'initramfs.cpio.gz'
        initrd.write_bytes(gzip.compress(archive+entry('TRAILER!!!',b'',0,999999),mtime=0))
        kernel=ROOT/'experiments/qemu_stage1/assets/vmlinuz-lts'
        check('pinned_guest_kernel',digest(kernel)=='9d6eff6e7bcd5f5fba623f7a44c565ca6914da2b91110a8dc72a3a63cb088693')
        backing=Backing();backing.initialize();backend=backing.start(work/'linux-simu')
        cmd=[str(QEMU),'-machine','q35','-accel','tcg','-m','512M','-smp','1','-nodefaults',
             '-display','none','-serial','stdio','-monitor','none','-no-reboot',
             '-icount','shift=3,align=off,sleep=off',
             '-kernel',str(kernel),'-initrd',str(initrd),
             '-append','console=ttyS0 rdinit=/init intel_iommu=on iommu.strict=1 panic=-1',
             '-device','intel-iommu,intremap=on,caching-mode=on',
             '-device','pcie-root-port,id=rp,chassis=1','-device','x3130-upstream,id=sw,bus=rp',
             '-device','xio3130-downstream,id=dp,bus=sw,chassis=2',
             '-device',backing.device()+',bus=dp']
        (work/'linux-command.json').write_text(json.dumps(cmd,indent=2))
        with (work/'linux.log').open('w') as log:
            qemu=subprocess.Popen(cmd,pass_fds=backing.fds,stdout=log,stderr=subprocess.STDOUT)
            check('linux_qemu_exit',qemu.wait(timeout=180)==0)
        text=(work/'linux.log').read_text(errors='replace')
        check('linux_boot_no_panic','Kernel panic' not in text)
        check('linux_tasks', 'P1_PASS tensor=3 rejected=5' in text and 'GUEST_RESULT=0' in text)
        if args.host_demo:
            check('host_demo_probe_mmap_msix','HOST_PROBE_PASS' in text)
            check('host_demo_app','Discovered 1 PCIe endpoint(s)' in text and 'minor=0 chip_index=0 [active]' in text)
        check('linux_iommu','DMAR: Intel(R) Virtualization Technology for Directed I/O' in text)
        check('software_stop',backend.wait(timeout=30)==0)
        check('shared_ddr0_host_write',struct.unpack_from('<I',backing.maps[0],0x201ff000)[0]==0x1234abcd)
        check('shared_ddr1_output',bytes(backing.maps[1][CONTROL+0x1000:CONTROL+0x1000+33792])==reference.read_bytes())
        # Dedicated qtest verifies W1C, MSI-X PBA and reset/restart fencing.
        backing.watchers[-1].join(timeout=5)
        backing.epoch=0;backing.initialize();backend=backing.start(work/'reset-simu')
        cmd=[str(QEMU),'-machine','q35','-accel','qtest','-display','none','-nodefaults',
             '-qtest',f'unix:{work}/qt.sock,server=on,wait=off','-qtest-log','/dev/null',
             '-qmp',f'unix:{work}/qm.sock,server=on,wait=off','-device',backing.device()+',addr=2']
        with (work/'reset-qemu.log').open('w') as log:
            qemu=subprocess.Popen(cmd,pass_fds=backing.fds,stdout=log,stderr=subprocess.STDOUT)
            qt=connect(work/'qt.sock',qemu);qm=connect(work/'qm.sock',qemu);qm.readline()
            def qtest(command):
                qt.write(command.encode()+b'\n');reply=qt.readline().decode().split()
                assert reply[0]=='OK',reply
                return reply[1:]
            def qmp(command):
                qm.write(json.dumps({'execute':command}).encode()+b'\n')
                while True:
                    item=json.loads(qm.readline())
                    if 'return' in item:return item['return']
                    if 'error' in item:raise RuntimeError(item)
            def cfg(offset,value,width='outl'):
                qtest(f'outl 0xcf8 {0x80001000+offset:#x}');qtest(f'{width} 0xcfc {value:#x}')
            def mapped():
                cfg(0x10,0xe0000000);cfg(0x20,0);cfg(0x24,1);cfg(4,6,'outw')
            qmp('qmp_capabilities');mapped()
            def read(addr):return int(qtest(f'readl {addr:#x}')[0],16)
            def write(addr,v):qtest(f'writel {addr:#x} {v:#x}')
            deadline=time.monotonic()+5
            while not read(0xe0000008) and time.monotonic()<deadline:time.sleep(.01)
            check('qtest_software_ready',read(0xe0000008)==1)
            # All vectors start masked. notify must latch the PBA while masked.
            write(0xe0000020,31)
            check('msix_masked_pending',read(0xe0003000)&(1<<31))
            # Invalid file trigger must produce explicit error, not fake success.
            write(0x100000000+0x18103190,0x5a5aa5a5)
            check('unsupported_dma_rejected',read(0xe000000c)==3)
            qmp('system_reset')
            check('reset_backend_stopped',backend.wait(timeout=10)==75)
            before=bytes(backing.maps[1][CONTROL:CONTROL+65536]);time.sleep(.02)
            check('old_writes_terminated',bytes(backing.maps[1][CONTROL:CONTROL+65536])==before)
            mapped();check('reset_not_ready',read(0xe0000008)==0)
            backing.watchers[-1].join(timeout=5)
            backing.epoch=struct.unpack_from('<I',backing.maps[2],0x400)[0]
            backing.initialize();backend=backing.start(work/'restart-simu')
            deadline=time.monotonic()+5
            while not read(0xe0000008) and time.monotonic()<deadline:time.sleep(.01)
            check('reset_software_ready_again',read(0xe0000008)==1)
            check('reset_msix_pba_cleared',read(0xe0003000)==0)
            # Unexpected backend death must withdraw ready and reject new input.
            backend.kill();backend.wait(timeout=5);backing.watchers[-1].join(timeout=5)
            deadline=time.monotonic()+5
            while read(0xe0000008) and time.monotonic()<deadline:time.sleep(.01)
            check('backend_death_not_ready',read(0xe0000008)==0)
            before=bytes(backing.maps[1][CONTROL+0x100:CONTROL+0x104])
            write(0x110000100,0xabcdef01)
            check('backend_death_blocks_input',bytes(backing.maps[1][CONTROL+0x100:CONTROL+0x104])==before and read(0xe000000c)==3)
            qmp('quit');check('qtest_exit',qemu.wait(timeout=10)==0)
            qt.close();qm.close()
        result['p1_shared_connection_pass']=True
        paths=[QEMU,SPIKE,FIRMWARE,kernel,initrd,reference,HERE/'guest-test.c',ROOT/'qemu/hw/misc/zc-pcie-p1.c']
        paths+=list((HERE/'runtime').glob('*'))
        paths+=[HERE/'run_p1.py',HERE/'run_faults.py',HERE/'host-probe.c',HERE/'host-demo.patch',HERE/'build_host_demo.py']
        paths+=[SIM/p for p in ['riscv/devices.cc','riscv/devices.h','riscv/mbox.cc','smart/sm_main.cc','spike_main/spike.cc']]
        paths+=[ROOT/'simulator/host-demo'/p for p in ['ssv_test_driver.c','ssv_test_app.c']]
        if args.host_demo:
            paths+=[module/p for p in ['ssv_test_driver.ko','ssv_test_app','manifest.json']]
        (work/'manifest.json').write_text(json.dumps({str(p):digest(p) for p in paths if p.is_file()},indent=2))
    except Exception as e:
        result['error']=f'{type(e).__name__}: {e}';raise
    finally:
        stop(qemu);stop(backend)
        if backing:backing.close()
        (work/'results.json').write_text(json.dumps(result,indent=2))
        print('Evidence:',work,flush=True)

if __name__=='__main__':main()
