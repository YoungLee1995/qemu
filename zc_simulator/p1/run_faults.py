#!/usr/bin/env python3
"""P1 shared-FD rejection, isolation, boundary and shared ECLIC regressions."""
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import time
from run_p1 import Backing, HERE, ROOT, SIM, SPIKE, FIRMWARE, ISA, QEMU, connect, stop


def main():
    work=HERE/'evidence'/f'faults-{time.time_ns()}'
    work.mkdir(parents=True)
    checks={};passed=False;a=Backing();b=Backing();proc=None
    def check(name,value):
        checks[name]=bool(value)
        if not value:raise RuntimeError(name)
        print(name,'PASS',flush=True)
    def invoke(name,cmd,fds,env=None):
        with (work/(name+'.log')).open('w') as log:
            return subprocess.run(cmd,cwd=work,pass_fds=fds,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=30).returncode
    try:
        a.initialize()
        timed_out=False
        try:
            a.start(work/'ready-timeout',ready_timeout=0)
        except RuntimeError as e:
            timed_out='software ready failed' in str(e)
        check('ready_timeout_reaps_backend',timed_out and not Path(f'/proc/{a.last_pid}').exists())
        # Present mailbox-sized FD where DDR0 is required; reject before use.
        bad=f'zc-pcie-p1,ddr0-fd={a.fds[2]},ddr1-fd={a.fds[1]},mailbox-fd={a.fds[2]}'
        code=invoke('qemu-size',[str(QEMU),'-machine','q35','-display','none','-nodefaults','-device',bad],a.fds)
        check('qemu_wrong_fd_size',code!=0 and 'must have size' in (work/'qemu-size.log').read_text())
        cmd=[str(SPIKE),f'--isa={ISA}',f'--p1-ddr0-fd={a.fds[2]}',f'--p1-ddr1-fd={a.fds[1]}',str(FIRMWARE)]
        code=invoke('spike-size',cmd,a.fds)
        check('spike_wrong_fd_size',code!=0 and 'incorrect size' in (work/'spike-size.log').read_text())
        code=invoke('spike-pair',[str(SPIKE),f'--p1-ddr0-fd={a.fds[0]}',str(FIRMWARE)],a.fds)
        check('spike_requires_both_regions',code!=0 and 'requires both FDs' in (work/'spike-pair.log').read_text())
        a.initialize();b.initialize()
        struct.pack_into('<I',a.maps[0],0x201ffffc,0x13572468)
        struct.pack_into('<I',b.maps[0],0x201ffffc,0x24681357)
        # Launch two functions backed by distinct sparse files, no CPU firmware.
        with (work/'qemu-isolation.log').open('w') as log:
            cmd=[str(QEMU),'-machine','q35','-accel','qtest','-display','none','-nodefaults',
                 '-qtest',f'unix:{work}/qt.sock,server=on,wait=off','-qtest-log','/dev/null',
                 '-device',a.device()+',addr=2','-device',b.device()+',addr=3']
            proc=subprocess.Popen(cmd,pass_fds=a.fds+b.fds,stdout=log,stderr=subprocess.STDOUT)
            qt=connect(work/'qt.sock',proc)
            def q(command):
                qt.write(command.encode()+b'\n');r=qt.readline().decode().split()
                if not r or r[0]!='OK':raise RuntimeError(r)
                return int(r[1],16) if len(r)>1 else 0
            def cfg(slot,off,v):
                q(f'outl 0xcf8 {0x80000000+(slot<<11)+off:#x}');q(f'outl 0xcfc {v:#x}')
            for slot,bar0,bar2 in [(2,0xe0000000,0xd0000000),(3,0xe0010000,0xd0400000)]:
                cfg(slot,0x10,bar0);cfg(slot,0x18,bar2);cfg(slot,4,6)
            check('card_a_last_word',q('readl 0xd01ffffc')==0x13572468)
            check('card_b_isolated',q('readl 0xd05ffffc')==0x24681357)
            guard=bytes(a.maps[0][0x20200000:0x20200040])
            q('writel 0xd0200000 0xdeadbeef')
            check('bar2_outside_aperture_rejected',q('readl 0xe000000c')==3 and bytes(a.maps[0][0x20200000:0x20200040])==guard)
            q('writel 0xe000000c 0')
            check('bar2_unsupported_read',q('readl 0xd0200000')==0xffffffff and q('readl 0xe000000c')==3)
            check('other_card_error_isolated',q('readl 0xe001000c')==0)
            qt.close();stop(proc)
        # Re-run real interrupt handlers against the new shared mailbox storage.
        tests=ROOT/'simulator/riscv-tests/runtime_test'
        for name in ['case_mcu0_npu3_pingpong','case_mcu0_mcu1_npu0_irq']:
            case=work/name;shutil.copytree(tests/name,case)
            if name.endswith('pingpong'):
                subprocess.run(['perl','gen_runtime_mailbox_npu3_pingpong.pl'],cwd=case,check=True,stdout=subprocess.DEVNULL)
                output=case/'runtime_mailbox_mcu0_npu3_pingpong_cfg_out.bin'
                words=struct.unpack('<II',(case/'runtime_mailbox_mcu0_npu3_pingpong_input.bin').read_bytes()[:8])
                expected=struct.pack('<4I',words[1],words[0],words[1],words[0])
            else:
                subprocess.run(['perl','gen_input.pl'],cwd=case,check=True,stdout=subprocess.DEVNULL)
                for core in ['mcu0','mcu1','npu0']:
                    prefix='/data/nuclei/gcc/bin/riscv64-unknown-elf-'
                    subprocess.run([prefix+'as','-march=rv64imafdc','-o',core+'.o',core+'.S'],cwd=case,check=True)
                    subprocess.run([prefix+'ld','-Ttext=0','-o',core+'.elf',core+'.o'],cwd=case,check=True)
                    subprocess.run(['/data/nuclei/gcc/bin/riscv64-unknown-elf-objcopy','-O','binary',core+'.elf',core+'.bin'],cwd=case,check=True)
                output=case/'dual_mcu_irq_out.bin';expected=struct.pack('<5I',0x07060504,0x03020100,0x0f0e0d0c,0x0b0a0908,2)
            output.unlink(missing_ok=True);a.maps[2][:]=bytes(4096)
            with (case/'shared-spike.log').open('w') as log:
                code=subprocess.run([str(SPIKE),f'--isa={ISA}','--runtime-func',str(FIRMWARE),'-cfg',str(case/'firmware.cfg')],cwd=case,
                    env=dict(os.environ,ZC_P1_MAILBOX_FD=str(a.fds[2])),pass_fds=[a.fds[2]],stdout=log,stderr=subprocess.STDOUT,timeout=60).returncode
            check(name+'_shared_irq',code==0 and output.exists() and output.read_bytes()==expected)
        passed=True
    finally:
        stop(proc);a.close();b.close()
        (work/'results.json').write_text(json.dumps({'pass':passed,'checks':checks},indent=2))
        print('Evidence:',work,flush=True)

if __name__=='__main__':main()
