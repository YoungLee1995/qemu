#!/usr/bin/env python3
"""Hardware BAR sizing, 64-bit relocation, decode enable and bounds checks."""
import json
import subprocess
import time
from pathlib import Path
from run_p1 import Backing,HERE,QEMU,connect,stop


def main():
    work=HERE/'evidence'/f'bars-{time.time_ns()}';work.mkdir(parents=True)
    b=Backing();b.initialize();proc=None;checks={};passed=False
    def check(name,condition):
        checks[name]=bool(condition)
        if not condition:raise RuntimeError(name)
        print(name,'PASS',flush=True)
    try:
        sock=Path('/tmp')/f'zc-bars-{__import__("os").getpid()}.sock'
        with (work/'qemu.log').open('w') as log:
            proc=subprocess.Popen([str(QEMU),'-machine','q35','-accel','qtest','-display','none','-nodefaults',
                '-qtest',f'unix:{sock},server=on,wait=off','-qtest-log','/dev/null','-device',b.device()+',addr=2'],
                pass_fds=b.fds,stdout=log,stderr=subprocess.STDOUT)
            qt=connect(sock,proc)
            def q(cmd):
                qt.write(cmd.encode()+b'\n');r=qt.readline().decode().split()
                if not r or r[0]!='OK':raise RuntimeError(r)
                return int(r[1],16) if len(r)>1 else 0
            def write(off,v):
                q(f'outl 0xcf8 {0x80001000+off:#x}');q(f'outl 0xcfc {v:#x}')
            def read(off):
                q(f'outl 0xcf8 {0x80001000+off:#x}');return q('inl 0xcfc')
            for index,size,flags in [(0,1<<22,4),(2,1<<25,4),(4,1<<35,12)]:
                off=0x10+index*4
                write(off,0xffffffff);write(off+4,0xffffffff)
                low,high=read(off),read(off+4)
                mask=(high<<32)|(low&0xfffffff0)
                check(f'bar{index}_type',(low&15)==flags)
                check(f'bar{index}_size',((~mask)&((1<<64)-1))+1==size)
                write(off,0);write(off+4,0)
            write(0x10,0xe0000000);write(0x14,0)
            write(0x18,0xd0000000);write(0x1c,0)
            write(0x20,0);write(0x24,8);write(4,6)
            check('bar0_decode',q('readl 0xe0000000')==0x5a435031)
            check('bar4_high32',read(0x24)==8)
            # End of each actual aperture routes to defined unsupported behavior.
            for name,addr in [('bar2',0xd1fffffc),('bar4',0xffffffffc)]:
                write(0x10,0xe0000000);q('writel 0xe000000c 0')
                check(name+'_last_word_rejected',q(f'readl {addr:#x}')==0xffffffff and q('readl 0xe000000c')==3)
            # Memory disable must prevent even valid endpoint reads from decoding.
            write(4,0)
            check('memory_disable_blocks_decode',q('readl 0xe0000000')!=0x5a435031)
            write(4,6)
            check('memory_enable_restores_decode',q('readl 0xe0000000')==0x5a435031)
            # Relocate BAR0 above 4GiB to test high half, independent of bridges.
            write(0x10,0);write(0x14,2)
            check('bar0_64bit_relocation',q('readl 0x200000000')==0x5a435031)
            check('bar0_old_address_removed',q('readl 0xe0000000')!=0x5a435031)
            qt.close()
        passed=True
    finally:
        stop(proc);b.close()
        (work/'results.json').write_text(json.dumps({'pass':passed,'checks':checks},indent=2))
        print('Evidence:',work,flush=True)

if __name__=='__main__':main()
