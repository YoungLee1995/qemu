#!/usr/bin/env python3
"""Exercise P2 Host service/HDMA via real endpoint DMA and Intel VT-d."""
import hashlib
import json
import mmap
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'p1'))
from run_p1 import Backing, QEMU, connect, stop, CONTROL

HERE=Path(__file__).resolve().parent


def main():
    work=HERE/'evidence'/f'hdma-{time.time_ns()}';work.mkdir(parents=True)
    b=Backing();b.initialize();proc=None;checks={};passed=False
    sock=Path('/tmp')/f'zc-p2-{os.getpid()}.sock'
    def check(name,value):
        checks[name]=bool(value)
        print(name,'PASS' if value else 'FAIL',flush=True)
        if not value:raise RuntimeError(name)
    try:
        # Backend readiness fixture, not a substitute for the real Spike tests.
        struct.pack_into('<II',b.maps[1],CONTROL+16,1,1)
        struct.pack_into('<I',b.maps[2],0x408,1)
        with (work/'qemu.log').open('w') as log:
            proc=subprocess.Popen([str(QEMU),'-machine','q35','-m','512M','-accel','qtest',
                '-display','none','-nodefaults','-device','intel-iommu,caching-mode=on',
                '-qtest',f'unix:{sock},server=on,wait=off','-qtest-log','/dev/null',
                '-device',b.device()+',p2=on,p2-trace=on,addr=2',
                '-device',b.device()+',p2=on,p2-trace=on,addr=3'],pass_fds=b.fds,stdout=log,stderr=subprocess.STDOUT)
            qt=connect(sock,proc)
            def q(command):
                qt.write(command.encode()+b'\n');line=qt.readline().decode().split()
                if not line or line[0]!='OK':raise RuntimeError(line)
                return int(line[1],16) if len(line)>1 else 0
            def wr(a,v):q(f'writel {a:#x} {v:#x}')
            def wq(a,v):q(f'writeq {a:#x} {v:#x}')
            def rd(a):return q(f'readl {a:#x}')
            def config(off,val,devfn=16):
                wrport=0x80000000|(devfn<<8)|off
                q(f'outl 0xcf8 {wrport:#x}');q(f'outl 0xcfc {val:#x}')
            for off,val in [(0x10,0xe0000000),(0x14,0),(4,6)]:config(off,val)
            for off,val in [(0x10,0xe0400000),(0x14,0),(4,6)]:config(off,val,24)
            def wait(a,v,limit=5):
                end=time.monotonic()+limit
                while time.monotonic()<end:
                    if rd(a)==v:return True
                    time.sleep(.001)
                return False
            check('ready',wait(0xe0000008,1))
            # Legacy VT-d four-level translation. IOVA 0x800000 -> GPA0x12000000.
            vtd=0xfed90000; root=0x10000000;ctx=root+0x1000
            l4=root+0x10000;l3=l4+0x1000;l2=l3+0x1000;l1=l2+0x1000
            iova=0x800000;pa=0x12000000
            wq(root,ctx|1);wq(ctx+16*16,l4|1);wq(ctx+16*16+8,2)
            wq(l4,l3|3);wq(l3,l2|3);wq(l2+4*8,l1|3)
            def page(index,perm=3):wq(l1+index*8,(pa+index*4096)|perm)
            for i in range(8):page(i)
            wq(vtd+0x20,root);wr(vtd+0x18,1<<30)
            check('vtd_root_set',bool(rd(vtd+0x1c)&(1<<30)))
            wr(vtd+0x18,1<<31)
            check('vtd_translation_enabled',bool(rd(vtd+0x1c)&(1<<31)))
            def invalidate():
                wq(vtd+0x28,(1<<63)|(1<<61))
                wq(vtd+0xf8,(1<<63)|(1<<60))
            def memread(a,n):
                qt.write(f'read {a:#x} {n:#x}\n'.encode());r=qt.readline().decode().split()
                if r[0]!='OK':raise RuntimeError(r)
                return bytes.fromhex(r[1].removeprefix('0x'))
            def memwrite(a,data):q(f'write {a:#x} {len(data):#x} 0x{data.hex()}')
            h=0xe0380000
            def start(src,dst,n,read=False,control=0x202,base=h,linked=0):
                a=base+(0x100 if read else 0)
                wr(a,1);wr(a+0x8c,0xffffffff);wr(a+0x34,control)
                wr(a+0x1c,n);wq(a+0x20,src);wq(a+0x28,dst)
                if linked:wq(a+0x10,linked);wr(a+0x18,2)
                wr(a+4,1);return a
            def done(a,expected=1):
                return wait(a+0x80,3) and rd(a+0x84)==expected
            payload=bytes((i*37+19)&255 for i in range(12345))
            local=0x20010000;off=0x10000
            b.maps[0][off:off+len(payload)]=payload
            a=start(local,iova+123,len(payload))
            check('write_cross_page',done(a) and memread(pa+123,len(payload))==payload)
            check('iova_not_physical',memread(iova+123,64)==bytes(64))
            memwrite(pa+123,bytes(x^255 for x in payload))
            a=start(iova+123,local,len(payload),read=True)
            check('read_cross_page',done(a) and b.maps[0][off:off+len(payload)]==bytes(x^255 for x in payload))
            wr(a+0x8c,1);check('interrupt_w1c',rd(a+0x84)==0)
            page(0,1);invalidate();memwrite(pa,bytes(64))
            a=start(local,iova,64)
            check('readonly_write_aborts',done(a,4) and memread(pa,64)==bytes(64))
            check('actual_vtd_fault',bool(rd(vtd+0x34)))
            a=start(iova,local,64,read=True)
            check('readonly_read_allowed',done(a))
            page(0,0);invalidate();a=start(iova,local,64,read=True)
            check('revoked_mapping_aborts',done(a,4))
            page(0);invalidate()
            a=start(iova+0x10000,local,64,read=True)
            check('unmapped_aborts',done(a,4))
            a=start(local,iova,64,base=0xe0780000)
            check('requester_domain_isolation',done(a,4))
            config(4,2);a=start(local,iova,64)
            check('bus_master_off_aborts',done(a,4));config(4,6)
            a=start(0x1fffffff,iova,64)
            check('invalid_local_aborts',done(a,4))
            a=start(local,(1<<64)-32,64)
            check('address_overflow_aborts',done(a,4))
            # 64-bit Host IOVA high half must survive channel programming.
            wq(l3+4*8,l2|3);invalidate()
            a=start(local,iova+(1<<32),64)
            check('host_iova_above_4g',done(a) and memread(pa,64)==b.maps[0][off:off+64])
            page(1,0);invalidate();memwrite(pa,bytes(8192))
            a=start(local,iova,8192)
            check('partial_write_aborts_at_fault_page',done(a,4) and memread(pa,4096)==b.maps[0][off:off+4096] and memread(pa+4096,4096)==bytes(4096))
            page(1);invalidate()
            for channel in range(1,8):
                a=start(local,iova,64,base=h+channel*0x200)
                check(f'write_channel_{channel}',done(a))
            # The supplied demo's terminal-data TCB convention.
            d=0x20020000
            desc=struct.pack('<IIQQ',3,4096,local,iova)
            b.maps[0][0x20000:0x20018]=desc
            a=start(0,0,0,control=0x203,linked=d)
            check('linked_terminal_descriptor',done(a) and memread(pa,4096)==b.maps[0][off:off+4096])
            b.maps[0][0x20000:0x20018]=struct.pack('<IIQQ',3,4096,iova,local+0x10000)
            a=start(0,0,0,read=True,control=0x203,linked=d)
            check('linked_read_descriptor',done(a) and b.maps[0][off+0x10000:off+0x11000]==memread(pa,4096))
            # Chained data then LLP/TCB link toggles consumer cycle.
            data=struct.pack('<IIQQ',1,64,local,iova)
            link=struct.pack('<IIQQ',7,0,d+48,0)
            tail=struct.pack('<IIQQ',2,64,local+64,iova+64)
            b.maps[0][0x20000:0x20048]=data+link+tail
            a=start(0,0,0,control=0x203,linked=d)
            check('linked_pointer_cycle_toggle',done(a) and memread(pa,128)==b.maps[0][off:off+128])
            b.maps[0][0x20000:0x20018]=struct.pack('<IIQQ',2,64,local,iova)
            a=start(0,0,0,control=0x203,linked=d)
            check('bad_descriptor_cycle_aborts',done(a,4))
            # Outbound device address maps to Host IOVA; bypass never bypasses VT-d.
            atu=0xe0300000;device=0x18000000000
            for o,v in [(0,0),(8,device&0xffffffff),(12,device>>32),(16,0xffff),(32,device>>32),(20,iova),(24,0),(4,1<<31)]:wr(atu+o,v)
            a=start(local,device,64,control=2)
            check('outbound_translation',done(a) and memread(pa,64)==b.maps[0][off:off+64])
            wr(atu+4,0);a=start(local,device,64,control=2)
            check('disabled_outbound_aborts',done(a,4))
            # Cancel before a large transfer can finish; no writes after ack.
            for i in range(8):page(i)
            invalidate();a=start(local,iova,32768);wr(a,0)
            before=memread(pa,32768);time.sleep(.03)
            check('channel_disable_stops_old_writes',done(a,4) and memread(pa,32768)==before)
            # Local IRQ status is level-sensitive and separate from Host MSI-X.
            wr(h+0x88,0x50)
            a=start(local,iova,64)
            check('local_stop_irq',done(a) and struct.unpack_from('<I',b.maps[2],0x434)[0]==1)
            wr(h+0x8c,1)
            check('local_irq_w1c_deassert',struct.unpack_from('<I',b.maps[2],0x434)[0]==0)
            wr(h+0x88,0x51);a=start(local,iova,64)
            check('local_irq_masked',done(a) and struct.unpack_from('<I',b.maps[2],0x434)[0]==0)
            wr(h+0x88,0x50)
            check('local_irq_unmask_pending',struct.unpack_from('<I',b.maps[2],0x434)[0]==1)
            wr(h+0x8c,1);a=start(0,iova,64)
            check('local_abort_irq',done(a,4) and struct.unpack_from('<I',b.maps[2],0x434)[0]==1)
            wr(h+0x8c,4)
            # P2 inbound route is dynamic, follows BAR relocation and 64-bit targets.
            for o,v in [(0x18,0xd0000000),(0x1c,0),(0x20,0),(0x24,8)]:config(o,v)
            ib=atu+0x100
            def inbound(index,bar,target,base=0,limit=0,bar_mode=True):
                reg=ib+index*0x200
                wr(reg+4,0)
                for o,v in [(0,0),(8,base&0xffffffff),(12,base>>32),(16,limit&0xffffffff),
                            (32,limit>>32),(20,target&0xffffffff),(24,target>>32),
                            (4,(1<<31)|((1<<30)|(bar<<8) if bar_mode else 0))]:wr(reg+o,v)
            inbound(0,2,0x20030000)
            wr(0xd0000020,0x11223344)
            check('inbound_bar_mode_ddr0',struct.unpack_from('<I',b.maps[0],0x30020)[0]==0x11223344)
            config(0x18,0xd2000000)
            check('inbound_bar_relocation',rd(0xd2000020)==0x11223344)
            inbound(0,2,0x4000040000)
            wr(0xd2000020,0x55667788)
            check('inbound_target_above_4g',struct.unpack_from('<I',b.maps[1],0x40020)[0]==0x55667788)
            inbound(0,2,0x20050000,0xd2001000,0xd2001fff,False)
            wr(0xd2001000,0xaabbccdd)
            check('inbound_address_mode',struct.unpack_from('<I',b.maps[0],0x50000)[0]==0xaabbccdd)
            check('inbound_address_outside_rejected',rd(0xd2002000)==0xffffffff)
            inbound(0,4,0x20060000)
            wr(0x800001000,0x13579bdf)
            check('inbound_bar4_64bit',struct.unpack_from('<I',b.maps[0],0x61000)[0]==0x13579bdf)
            inbound(1,4,0x20070000)
            check('inbound_priority',rd(0x800001000)==0x13579bdf)
            wr(ib+4,0)
            check('inbound_next_region',rd(0x800001000)==0)
            inbound(1,4,(1<<64)-4)
            check('inbound_overflow_rejected',rd(0x800001000)==0xffffffff)
            inbound(1,4,0xdead0000)
            check('inbound_invalid_target_rejected',rd(0x800001000)==0xffffffff)
            wr(ib+0x204,0)
            check('inbound_disabled_rejected',rd(0x800001000)==0xffffffff)
            inbound(0,2,0x20030000)
            wr(ib+4,0xc0080200)
            wr(0xd2000020,0x12345678)
            check('inbound_function_match_zero',rd(0xd2000020)==0x12345678)
            wr(ib,1<<20)
            check('inbound_wrong_function_rejected',rd(0xd2000020)==0xffffffff)
            wr(ib,2)
            check('inbound_unsupported_type_rejected',rd(0xd2000020)==0xffffffff)
            inbound(0,2,0x21380000)
            wr(0xd2000034,0x2)
            check('inbound_controller_alias',rd(h+0x34)==0x2)
            inbound(0,2,0x18103000)
            wr(0xd2000180,0x12340000)
            check('inbound_ipc_alias',rd(0x818103180)==0x12340000)
            inbound(0,4,0x4000080000,0x8fffff000,0x900000fff,False)
            wr(ib,1<<13)
            wr(0x900000000,0x2468ace0)
            check('inbound_increase_region_size',struct.unpack_from('<I',b.maps[1],0x81000)[0]==0x2468ace0)
            wr(ib,0)
            check('inbound_upper_limit_requires_enable',rd(0x900000000)==0xffffffff)
            # Mapping writes cancel an old transfer before acknowledging the write.
            a=start(local,iova,32768);wr(atu+0x14,iova+4096)
            before=memread(pa,32768);time.sleep(.02)
            check('mapping_change_cancels_hdma',done(a,4) and memread(pa,32768)==before)
            for channel in range(1,8):
                a=start(iova,local,64,read=True,base=h+channel*0x200)
                check(f'read_channel_{channel}',done(a))
            page(1,0);invalidate()
            b.maps[0][off:off+8192]=bytes([0xcd])*8192
            a=start(iova,local,8192,read=True)
            check('partial_read_preserves_unread_tail',done(a,4) and
                  b.maps[0][off:off+4096]==memread(pa,4096) and
                  b.maps[0][off+4096:off+8192]==bytes([0xcd])*4096)
            page(1);invalidate()
            a=start(local,iova,64)
            check('hdma_retry_after_fault',done(a))
            qt.close();passed=True
    finally:
        stop(proc);b.close();sock.unlink(missing_ok=True)
        sources=[HERE/'run_hdma.py',HERE.parents[1]/'hw/misc/zc-pcie-p1.c',HERE.parents[1]/'hw/misc/zc-pcie-p2.h',HERE.parents[1]/'hw/misc/zc-pcie-p2-engine.h',QEMU]
        (work/'manifest.json').write_text(json.dumps({str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},indent=2))
        (work/'results.json').write_text(json.dumps({'pass':passed,'p2_full_exit':False,'checks':checks},indent=2))
        print('Evidence:',work,flush=True)

if __name__=='__main__':main()
