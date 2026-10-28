#!/usr/bin/env python3
"""Real persistent MCU/NPU, four DMA combinations, endpoint and VT-d."""
import hashlib
import json
import mmap
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'p1'))
from run_p1 import QEMU, SPIKE, FIRMWARE, ISA, ROOT, SIM, connect, stop, digest
HERE=Path(__file__).resolve().parent


def main():
    work=HERE/'evidence'/f'spike-{time.time_ns()}';work.mkdir(parents=True)
    service=qemu=None;maps=[];checks={};passed=False
    sock=Path('/tmp')/f'zc-p2-service-{os.getpid()}.sock'
    qtpath=Path('/tmp')/f'zc-p2-qt-{os.getpid()}.sock'
    qmpath=Path('/tmp')/f'zc-p2-qm-{os.getpid()}.sock'
    def check(name,value):
        checks[name]=bool(value);print(name,'PASS' if value else 'FAIL',flush=True)
        if not value:raise RuntimeError(name)
    try:
        subprocess.run(['make','-C',str(HERE/'runtime')],check=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        inputs=work/'inputs';inputs.mkdir()
        for n in ['mcu.bin','npu.bin']:shutil.copy2(HERE/'runtime'/n,inputs/n)
        fw=inputs/'firmware.cfg'
        fw.write_text('runtime_func:1\npc_num:2\n[1, 0x40000000, ./mcu.bin]\n[16, 0x4000000000, ./npu.bin]\nddr_input_num:0\nddr_output_num:0\nl1buf_input_num:0\nl1buf_output_num:0\n')
        files=[SPIKE,FIRMWARE,fw,inputs/'mcu.bin',inputs/'npu.bin',SIM/'smart/sm_system.cfg']
        config={'spike':str(SPIKE),'firmware':str(FIRMWARE),'isa':ISA,'cfg':str(fw),
                'cwd':str(inputs),'p2':True,'sha256':{str(p):digest(p) for p in files}}
        cfg=work/'backend.json';cfg.write_text(json.dumps(config,indent=2))
        with (work/'service.log').open('w') as log:
            service=subprocess.Popen([sys.executable,str(ROOT/'simulator/tools/p1/service.py'),
                '--socket',str(sock),'--config',str(cfg),'--work',str(work/'service')],stdout=log,stderr=subprocess.STDOUT)
        deadline=time.monotonic()+5
        while not sock.exists() and service.poll() is None and time.monotonic()<deadline:time.sleep(.01)
        check('service_listening',sock.exists())
        with (work/'qemu.log').open('w') as log:
            qemu=subprocess.Popen([str(QEMU),'-machine','q35','-m','512M','-accel','qtest','-display','none','-nodefaults',
                '-device','intel-iommu,caching-mode=on','-qtest',f'unix:{qtpath},server=on,wait=off',
                '-qmp',f'unix:{qmpath},server=on,wait=off','-qtest-log','/dev/null',
                '-device',f'zc-pcie-p1,p2=on,p2-trace=on,addr=2,backend-socket={sock},config-sha256={digest(cfg)}'],stdout=log,stderr=subprocess.STDOUT)
        qt=connect(qtpath,qemu)
        qm=socket.socket(socket.AF_UNIX);qm.settimeout(10);qm.connect(str(qmpath));mf=qm.makefile('rwb',buffering=0);mf.readline()
        def command(name):
            mf.write((json.dumps({'execute':name})+'\n').encode())
            while True:
                r=json.loads(mf.readline())
                if 'return' in r:return r
                if 'error' in r:raise RuntimeError(r)
        command('qmp_capabilities')
        def q(cmd):
            qt.write(cmd.encode()+b'\n');r=qt.readline().decode().split()
            if not r or r[0]!='OK':raise RuntimeError(r)
            return int(r[1],16) if len(r)>1 else 0
        def wr(a,v):q(f'writel {a:#x} {v:#x}')
        def wq(a,v):q(f'writeq {a:#x} {v:#x}')
        def rd(a):return q(f'readl {a:#x}')
        def config(off,v):q(f'outl 0xcf8 {0x80001000+off:#x}');q(f'outl 0xcfc {v:#x}')
        def wait(a,v,limit=25):
            end=time.monotonic()+limit
            while time.monotonic()<end:
                if rd(a)==v:return True
                if qemu.poll() is not None:break
                time.sleep(.002)
            return False
        for off,val in [(0x10,0xe0000000),(0x14,0),(0x20,0),(0x24,8),(4,6)]:config(off,val)
        check('real_software_ready',wait(0xe0000008,1))
        def spawns():
            return [e['pid'] for e in map(json.loads,(work/'service/service.jsonl').read_text().splitlines()) if e['event']=='spawn']
        initial=spawns()
        # Real QEMU-created sealed backing, observed for register/engine assertions only.
        fdroot=Path(f'/proc/{qemu.pid}/fd')
        for name,size in [('mailbox',4096)]:
            fdpath=next(p for p in fdroot.iterdir() if 'zc-p1-' in os.readlink(p) and p.stat().st_size==size)
            fd=os.open(fdpath,os.O_RDWR);maps.append(mmap.mmap(fd,size));os.close(fd)
        mb=maps[0]
        vtd=0xfed90000;root=0x10000000;ctx=root+0x1000;l4=root+0x10000;l3=l4+0x1000;l2=l3+0x1000;l1=l2+0x1000
        iova=0x800000;pa=0x12000000
        wq(root,ctx|1);wq(ctx+16*16,l4|1);wq(ctx+16*16+8,2)
        wq(l4,l3|3);wq(l3,l2|3);wq(l2+4*8,l1|3)
        for i in range(32):wq(l1+i*8,(pa+i*4096)|3)
        wq(vtd+0x20,root);wr(vtd+0x18,1<<30);wr(vtd+0x18,1<<31)
        check('vtd_enabled',bool(rd(vtd+0x1c)&(1<<31)))
        def invalidate():wq(vtd+0x28,(1<<63)|(1<<61));wq(vtd+0xf8,(1<<63)|(1<<60))
        # Outbound conversion authority resides in QEMU; map 128KiB input/output aperture.
        atu=0xe0300000;dev=0x18000000000
        for o,v in [(0,0),(8,0),(12,dev>>32),(16,0x1ffff),(32,dev>>32),(20,iova),(24,0),(4,1<<31)]:wr(atu+o,v)
        def memwrite(a,data):q(f'write {a:#x} {len(data):#x} 0x{data.hex()}')
        def memread(a,n):
            qt.write(f'read {a:#x} {n:#x}\n'.encode());r=qt.readline().decode().split();return bytes.fromhex(r[1].removeprefix('0x'))
        packet=0x810000100
        def task(number,mode,n=8192):
            data=bytes((i*37+number)&255 for i in range(n));memwrite(pa,data);memwrite(pa+0x10000,bytes(n))
            for o,v in [(4,mode),(8,n),(12,iova),(16,0),(20,iova+0x10000),(24,0)]:wr(packet+o,v)
            wr(packet,number)
            check(f'mode{mode}_task{number}_done',wait(0x81000002c,number))
            check(f'mode{mode}_task{number}_status',rd(0x810000020)==0)
            check(f'mode{mode}_task{number}_data',memread(pa+0x10000,n)==bytes(x^0x5a for x in data))
        task(1,1)
        check('npu_without_hdma',rd(0xe0380000)==0 and rd(0xe0380100)==0)
        for number,mode in [(2,2),(3,3),(4,4),(5,1)]:task(number,mode)
        check('ordinary_tasks_same_spike',spawns()==initial and Path(f'/proc/{initial[0]}').exists())
        # MCU proxy sees the authoritative control1 written via Host BAR mirror.
        check('mcu_hdma_proxy_used',rd(0xe0380034)==0x202 and rd(0xe0380134)==0x202)
        # Original host-demo register/CRC/result protocol, real MCU consumer.
        import zlib
        ipc=0x800000000+0x18103180
        def file_command(trigger,addr,n,crc=0):
            for o,v in [(0,addr&0xffffffff),(4,addr>>32),(8,n),(12,0),(20,crc),(24,0)]:wr(ipc+o,v)
            wr(ipc+16,trigger)
        data=bytes((i*17+3)&255 for i in range(12345))
        memwrite(pa,data)
        file_command(0x5a5aa5a5,iova,len(data),zlib.crc32(data))
        check('file_crc_transfer',wait(ipc+16,0))
        memwrite(pa+0x10000,bytes(len(data)+8))
        file_command(0x72736c74,iova+0x10000,len(data)+8)
        check('file_result_done',wait(pa+0x10000,0x646f6e65))
        check('file_result_size',rd(pa+0x10004)==len(data))
        check('file_result_payload',memread(pa+0x10008,len(data))==data)
        check('file_result_command_retired',wait(ipc+16,0))
        file_command(0x5a5aa5a5,iova,len(data),zlib.crc32(data)^1)
        check('file_bad_crc_rejected',wait(ipc+16,0x80000003))
        file_command(0x72736c74,iova+0x10000,0)
        check('file_no_stale_result',wait(ipc+16,0x80000004))
        file_command(0x5a5aa5a5,iova,2097145,0)
        check('file_oversize_rejected',wait(ipc+16,0x80000001))
        wq(l1,0);invalidate()
        file_command(0x5a5aa5a5,iova,len(data),zlib.crc32(data))
        check('file_iommu_fault_returned',wait(ipc+16,0x80000002))
        wq(l1,pa|3);invalidate()
        file_command(0x5a5aa5a5,iova,len(data),zlib.crc32(data))
        check('file_retry_same_spike',wait(ipc+16,0) and spawns()==initial)
        check('mcu_local_irq_handler_executed',rd(0x810000030)>0)
        # Cancel queued HDMA before requesting service QUIESCE. Nothing can
        # write Host RAM after the reset acknowledgment, including old epochs.
        for o,v in [(0,1),(0x8c,0xffffffff),(0x34,0x202),(0x1c,0x1000000),
                    (0x20,0x20010000),(0x24,0),(0x28,iova),(0x2c,0),(4,1)]:wr(0xe0380000+o,v)
        wr(atu+0x104,0xc0000200)
        command('system_reset')
        snapshot=memread(pa,32768)
        for off,val in [(0x10,0xe0000000),(0x14,0),(0x20,0),(0x24,8),(4,6)]:config(off,val)
        check('reset_software_ready',wait(0xe0000008,1))
        check('reset_epoch_advanced',rd(0xe0000014)==1)
        check('reset_old_host_writes_stopped',memread(pa,32768)==snapshot)
        check('reset_registers_cleared',rd(0xe0380000)==0 and rd(atu+4)==0 and rd(atu+0x104)==0)
        check('reset_one_backend_replacement',len(spawns())==len(initial)+1 and spawns()[-1]!=initial[0])
        wq(vtd+0x20,root);wr(vtd+0x18,1<<30);wr(vtd+0x18,1<<31)
        for o,v in [(0,0),(8,0),(12,dev>>32),(16,0x1ffff),(32,dev>>32),(20,iova),(24,0),(4,1<<31)]:wr(atu+o,v)
        task(1,1)
        # Recoverable NPU faults: read, write, partial progress, mapping change.
        for num,index,perm,status in [(2,16,0,5),(4,0,0,4),(6,17,0,5),(8,16,1,5)]:
            wq(l1+index*8,(pa+index*4096)|perm);invalidate()
            before=memread(pa+0x10000,8192)
            for o,v in [(4,1),(8,8192)]:wr(packet+o,v)
            wr(packet,num)
            check(f'fault{num}_task_returned',wait(0x81000002c,num))
            check(f'fault{num}_status',rd(0x810000020)==status)
            check(f'fault{num}_ready_preserved',rd(0xe0000008)==1)
            if index!=17:check(f'fault{num}_no_target_write',memread(pa+0x10000,8192)==before)
            check(f'fault{num}_same_spike',len(spawns())==len(initial)+1)
            wq(l1+index*8,(pa+index*4096)|3);invalidate()
            task(num+1,1)
        wq(l1,pa|1);invalidate();task(10,1)
        check('npu_readonly_input_allowed',rd(0x810000020)==0)
        wq(l1,pa|3);invalidate()
        for num,kind in [(11,'bme'),(13,'outbound')]:
            if kind=='bme':config(4,2)
            else:wr(atu+4,0)
            wr(packet,num)
            check(f'npu_{kind}_fault_returned',wait(0x81000002c,num) and rd(0x810000020)==4)
            if kind=='bme':config(4,6)
            else:wr(atu+4,1<<31)
            task(num+1,1)
        check('npu_fault_matrix_same_spike',len(spawns())==len(initial)+1)
        check('npu_actual_iommu_fault' ,bool(rd(vtd+0x34)))
        # Existing Tensor operands + captured descriptors transported over PCIe.
        # Compare with the pinned standalone result; do not call this a new math golden.
        payload=bytearray(0x9200)
        for offset,name in [(0,'act_l1.bin'),(0x8400,'wgt_l1.bin'),
                            (0x8a00,'wgt_scale_l1.bin'),(0x9000,'moe_index_l1.bin')]:
            raw=(ROOT/'moe_bf16_sint4_33x29x34_fix/data'/name).read_bytes()
            payload[offset:offset+len(raw)]=raw
        args=(ROOT/'simulator/riscv-tests/packet_runtime_test/tensor-args.bin').read_bytes()
        reference=ROOT/'experiments/single_die_packet/run-20261027T100512-648188Z/moe/data/result_l1_spike.bin'
        expected=reference.read_bytes()
        check('tensor_reference_pin',hashlib.sha256(expected).hexdigest()=='37121ee2cd6afaed02f18587ed683d6fb5d5c4a6cd3b078f682a77891fe7e015')
        for number,mode in [(15,1),(16,2),(17,3),(18,4)]:
            memwrite(pa,payload);memwrite(pa+0x10000,bytes(33792))
            for i in range(32):wr(0x810000180+i*4,struct.unpack_from('<I',args,i*4)[0])
            for o,v in [(4,mode),(8,len(payload)),(12,iova),(16,0),(20,iova+0x10000),(24,0),(28,1)]:wr(packet+o,v)
            wr(packet,number)
            check(f'tensor_mode{mode}_done',wait(0x81000002c,number))
            check(f'tensor_mode{mode}_status',rd(0x810000020)==0)
            check(f'tensor_mode{mode}_all_bytes',memread(pa+0x10000,33792)==expected)
        check('tensor_tasks_same_process',len(spawns())==len(initial)+1)
        wr(packet+28,0)
        # Deterministic pause at an accepted, unfinished request. Only available
        # under qtest; use it to prove cancellation, never successful DMA data.
        def restore():
            for off,val in [(0x10,0xe0000000),(0x14,0),(0x20,0),(0x24,8),(4,6)]:config(off,val)
            check('recovery_ready_'+str(len(checks)),wait(0xe0000008,1))
            wq(vtd+0x20,root);wr(vtd+0x18,1<<30);wr(vtd+0x18,1<<31)
            for o,v in [(0,0),(8,0),(12,dev>>32),(16,0x1ffff),(32,dev>>32),(20,iova),(24,0),(4,1<<31)]:wr(atu+o,v)
        task_id=19
        for engine,read,hold in [('npu',True,1),('npu',False,2),('hdma',True,4),('hdma',False,8)]:
            for action in ['reset','disconnect']:
                label=f'{engine}_{"read" if read else "write"}_{action}'
                old_pid=spawns()[-1];old_count=len(spawns());old_epoch=rd(0xe0000014)
                wr(0xe0381080,hold)
                if engine=='npu':
                    for o,v in [(4,1),(8,8192),(28,0)]:wr(packet+o,v)
                    wr(packet,task_id);task_id+=1
                    end=time.monotonic()+10
                    while time.monotonic()<end:
                        if struct.unpack_from('<I',mb,0x500)[0]==1 and struct.unpack_from('<I',mb,0x508)[0]==(1 if read else 2):break
                        time.sleep(.002)
                    check(label+'_accepted',struct.unpack_from('<II',mb,0x500)[0]==1 and struct.unpack_from('<I',mb,0x508)[0]==(1 if read else 2))
                else:
                    channel=0xe0380000+(0x100 if read else 0)
                    src,dst=(iova,0x20010000) if read else (0x20010000,iova+0x10000)
                    for o,v in [(0,1),(0x8c,0xffffffff),(0x34,0x202),(0x1c,8192),(0x20,src&0xffffffff),(0x24,src>>32),(0x28,dst&0xffffffff),(0x2c,dst>>32),(4,1)]:wr(channel+o,v)
                    check(label+'_accepted',wait(channel+0x80,1))
                before=memread(pa+0x10000,32768)
                if action=='reset':
                    command('system_reset')
                else:
                    stop(service);service=None
                    check(label+'_ready_withdrawn',wait(0xe0000008,0))
                    with (work/'service-restart.log').open('a') as log:
                        service=subprocess.Popen([sys.executable,str(ROOT/'simulator/tools/p1/service.py'),
                            '--socket',str(sock),'--config',str(cfg),'--work',str(work/'service')],stdout=log,stderr=subprocess.STDOUT)
                restore()
                check(label+'_old_process_reaped',not Path(f'/proc/{old_pid}').exists())
                check(label+'_one_replacement',len(spawns())==old_count+1)
                check(label+'_no_old_host_write',memread(pa+0x10000,32768)==before)
                check(label+'_hold_cleared',rd(0xe0381080)==0)
                if action=='reset':check(label+'_epoch_advanced',rd(0xe0000014)==old_epoch+1)
                check(label+'_no_false_completion',rd(0x81000002c)==0)
                task(1,1);task_id=2
                notifications=rd(0xe0000010);time.sleep(.03)
                check(label+'_no_duplicate_completion',rd(0xe0000010)==notifications)
                # Fresh mailbox backing after reconnect/reset: remap for next fault.
                mb.close();maps.clear()
                fdpath=next(p for p in fdroot.iterdir() if 'zc-p1-' in os.readlink(p) and p.stat().st_size==4096)
                fd=os.open(fdpath,os.O_RDWR);mb=mmap.mmap(fd,4096);os.close(fd);maps.append(mb)
        trace=[json.loads(line) for line in (work/'qemu.log').read_text().splitlines() if line.startswith('{"event":"p2_host"')]
        check('trace_context_and_status',bool(trace) and all(
            all(key in item for key in ['timestamp_ns','bus','devfn','epoch','session','engine','parent_id','channel','task_id','hart_id','address_domain','memtx','phase']) for item in trace))
        check('trace_hdma_channel_and_parent',any(item['channel']>=0 and item['parent_id']>0 for item in trace))
        qt.close();mf.close();qm.close();passed=True
    finally:
        stop(qemu);stop(service)
        for m in maps:m.close()
        for p in [sock,qtpath,qmpath]:p.unlink(missing_ok=True)
        files=list((HERE/'runtime').glob('*'))+[HERE/'run_spike.py',SIM/'riscv/sim.cc',SIM/'riscv/sim.h',SIM/'riscv/p2_pcie.h',ROOT/'simulator/tools/p1/service.py',SPIKE,QEMU,
            ROOT/'simulator/riscv-tests/packet_runtime_test/tensor-args.bin',
            ROOT/'experiments/single_die_packet/run-20261027T100512-648188Z/moe/data/result_l1_spike.bin']
        files+=list((ROOT/'moe_bf16_sint4_33x29x34_fix/data').glob('*l1.bin'))
        (work/'manifest.json').write_text(json.dumps({str(p):digest(p) for p in files if p.is_file()},indent=2))
        (work/'results.json').write_text(json.dumps({'pass':passed,'p2_full_exit':False,'checks':checks},indent=2))
        print('Evidence:',work,flush=True)

if __name__=='__main__':main()
