#!/usr/bin/env python3
"""Two persistent real Spike backends behind a PCIe switch; P3 experiment."""
import hashlib
import json
import mmap
import socket
import struct
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'p1'))
from run_p1 import QEMU, SPIKE, FIRMWARE, ISA, ROOT, SIM, connect, stop, digest
HERE = Path(__file__).resolve().parent


def main():
    work = HERE / 'evidence' / f'dual-{time.time_ns()}'
    work.mkdir(parents=True)
    procs, sockets, checks = [], [], {}
    passed = False
    def check(name, value):
        checks[name] = bool(value)
        print(name, 'PASS' if value else 'FAIL', flush=True)
        if not value:
            raise RuntimeError(name)
    try:
        runtime = HERE.parent / 'p2/runtime'
        subprocess.run(['make', '-C', str(runtime)], check=True, stdout=subprocess.PIPE)
        inputs = work / 'inputs'
        inputs.mkdir()
        for name in ('mcu.bin', 'npu.bin'):
            shutil.copy2(runtime / name, inputs / name)
        fw = inputs / 'firmware.cfg'
        fw.write_text('runtime_func:1\npc_num:2\n[1, 0x40000000, ./mcu.bin]\n[16, 0x4000000000, ./npu.bin]\nddr_input_num:0\nddr_output_num:0\nl1buf_input_num:0\nl1buf_output_num:0\n')
        files = [SPIKE, FIRMWARE, fw, inputs/'mcu.bin', inputs/'npu.bin', SIM/'smart/sm_system.cfg']
        cfg = work / 'backend.json'
        cfg.write_text(json.dumps(dict(spike=str(SPIKE), firmware=str(FIRMWARE), isa=ISA,
            cfg=str(fw), cwd=str(inputs), p2=True, sha256={str(p):digest(p) for p in files}), indent=2))
        for card in range(2):
            sock = Path(f'/tmp/zc-p3-{os.getpid()}-{card}.sock'); sockets.append(sock)
            with (work/f'service{card}.log').open('w') as log:
                procs.append(subprocess.Popen([sys.executable, str(ROOT/'simulator/tools/p1/service.py'),
                    '--socket', str(sock), '--config', str(cfg), '--work', str(work/f'service{card}'),
                    '--card', str(card)], stdout=log, stderr=subprocess.STDOUT))
        end = time.monotonic()+10
        while not all(p.exists() for p in sockets) and time.monotonic()<end:
            time.sleep(.01)
        check('two_services', all(p.exists() for p in sockets))
        qtpath = Path(f'/tmp/zc-p3-{os.getpid()}-qt.sock'); sockets.append(qtpath)
        qmpath = Path(f'/tmp/zc-p3-{os.getpid()}-qm.sock'); sockets.append(qmpath)
        args = [str(QEMU), '-machine', 'q35', '-m', '512M', '-accel', 'qtest', '-display', 'none', '-nodefaults',
                '-qmp', f'unix:{qmpath},server=on,wait=off',
                '-device', 'intel-iommu,caching-mode=on', '-qtest', f'unix:{qtpath},server=on,wait=off', '-qtest-log', '/dev/null',
                '-device', 'pcie-root-port,id=rp,addr=2,chassis=1',
                '-device', 'x3130-upstream,id=sw,bus=rp',
                '-device', 'xio3130-downstream,id=ds0,bus=sw,addr=1,chassis=2',
                '-device', 'xio3130-downstream,id=ds1,bus=sw,addr=2,chassis=3']
        for i in range(2):
            args += ['-device', f'zc-pcie-p1,id=ep{i},bus=ds{i},addr=0,p2=on,p3=on,p2-trace=on,card-id={i},backend-socket={sockets[i]},config-sha256={digest(cfg)}']
        (work/'qemu-command.json').write_text(json.dumps(args, indent=2))
        with (work/'qemu.log').open('w') as log:
            proc = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT); procs.append(proc)
        qt = connect(qtpath, proc)
        qm = socket.socket(socket.AF_UNIX);qm.settimeout(30);qm.connect(str(qmpath))
        mf=qm.makefile('rwb',buffering=0);mf.readline()
        def command(name,arguments=None):
            mf.write((json.dumps(dict(execute=name,arguments=arguments or {}))+'\n').encode())
            while True:
                r=json.loads(mf.readline())
                if 'return' in r:return r['return']
                if 'error' in r:raise RuntimeError(r)
        command('qmp_capabilities')
        def q(cmd):
            qt.write(cmd.encode()+b'\n'); r=qt.readline().decode().split()
            if not r or r[0]!='OK': raise RuntimeError(r)
            return int(r[1],16) if len(r)>1 else 0
        def wr(a,v): q(f'writel {a:#x} {v:#x}')
        def wq(a,v): q(f'writeq {a:#x} {v:#x}')
        def rd(a): return q(f'readl {a:#x}')
        def config(bus,devfn,off,val):
            q(f'outl 0xcf8 {0x80000000|(bus<<16)|(devfn<<8)|off:#x}');q(f'outl 0xcfc {val:#x}')
        # Real root/upstream/two downstream bridge hierarchy, numbered by test BIOS.
        bridges=[(0,16,0,1,4),(1,0,1,2,4),(2,8,2,3,3),(2,16,2,4,4)]
        def bridge(b,d,primary,secondary,subordinate):
            for o,v in [(0x18,primary|(secondary<<8)|(subordinate<<16)),
                        (0x20,0xe030e000 if b==2 and d==8 else
                              0xe070e040 if b==2 and d==16 else 0xe070e000),
                        (0x24,0xfff10001),
                        (0x28,0x18 if b==2 and d==16 else 0x10),
                        (0x2c,0x17 if b==2 and d==8 else 0x1f),(4,6)]:
                config(b,d,o,v)
        for br in bridges: bridge(*br)
        bars=[0x1000000000,0x1800000000]; regs=[0xe0000000,0xe0400000]
        for i in range(2):
            for o,v in [(0x10,regs[i]),(0x14,0),(0x20,bars[i]&0xffffffff),(0x24,bars[i]>>32),(4,6)]: config(3+i,0,o,v)
        def wait(a,v,limit=30):
            end=time.monotonic()+limit
            while time.monotonic()<end:
                if rd(a)==v:return True
                if proc.poll() is not None:break
                time.sleep(.002)
            return False
        for i in range(2):check(f'card{i}_real_ready',wait(regs[i]+8,1))
        # Address-matched inbound exposes only a 128KiB DDR payload window.
        peer=[b+0x20000000 for b in bars]
        def inbound(i,enabled=True,target=0x20010000):
            a=regs[i]+0x300100
            for o,v in [(4,0),(0,0),(8,peer[i]&0xffffffff),(12,peer[i]>>32),
                        (16,(peer[i]+0x1ffff)&0xffffffff),(20,target),(24,0),(4,0x80000000 if enabled else 0)]:wr(a+o,v)
        def outbound(i):
            a=regs[i]+0x300000;dst=peer[1-i]
            for o,v in [(4,0),(0,0),(8,0),(12,0x180),(16,0x1ffff),(20,dst&0xffffffff),(24,dst>>32),(4,0x80000000)]:wr(a+o,v)
        for i in range(2):inbound(i);outbound(i)
        def write(a,data):
            data+=bytes(-len(data)%4)
            q(f'write {a:#x} {len(data):#x} 0x{data.hex()}')
        def read(a,n):
            qt.write(f'read {a:#x} {(n+3)&~3:#x}\n'.encode());r=qt.readline().decode().split()
            if not r or r[0]!='OK':raise RuntimeError(r)
            return bytes.fromhex(r[1].removeprefix('0x'))[:n]
        counts=[0,0]
        def submit(i,mode,n=12345):
            # Host-injected diagnostic channels relinquish their old W1C status
            # before the MCU owns the aggregate IRQ again.
            for ch in (0,0x100):wr(regs[i]+0x38008c+ch,0xffffffff)
            counts[i]+=1;number=counts[i];p=bars[i]+0x10000100;target=peer[1-i]
            data=bytes((j*37+i*11+number)&255 for j in range(n))
            write(target,data);write(target+0x10000,bytes(n))
            for o,v in [(4,mode),(8,n),(12,target&0xffffffff),(16,target>>32),
                        (20,(target+0x10000)&0xffffffff),(24,(target+0x10000)>>32),(28,0)]:wr(p+o,v)
            wr(p,number)
            return number,data
        def finish(i,number,data,success=True):
            check(f'card{i}_task{number}_retired',wait(bars[i]+0x1000002c,number))
            status=rd(bars[i]+0x10000020)
            check(f'card{i}_task{number}_status',status==0 if success else status!=0)
            if success:check(f'card{i}_task{number}_peer_data',read(peer[1-i]+0x10000,len(data))==bytes(x^0x5a for x in data))
        for mode in (1,2,3,4):
            jobs=[submit(i,mode) for i in range(2)]
            for i,(number,data) in enumerate(jobs):finish(i,number,data)
        # Seeded independent card windows did not alias under bidirectional traffic.
        check('independent_ddr',read(peer[0],64)!=read(peer[1],64))
        # Explicit fault paths through each original engine, followed by recovery.
        for mode in (1,2):
            inbound(1,False)
            n,data=submit(0,mode);finish(0,n,data,False)
            inbound(1)
            n,data=submit(0,mode);finish(0,n,data)
        # Target memory decode off; stale peer address must not become Host access.
        config(4,0,4,4)
        n,data=submit(0,2);finish(0,n,data,False)
        config(4,0,4,6)
        n,data=submit(0,2);finish(0,n,data)
        # Bridge prefetch window closed, then restored.
        config(2,16,0x2c,0)
        n,data=submit(0,2);finish(0,n,data,False)
        bridge(*bridges[3])
        n,data=submit(0,2);finish(0,n,data)
        # Current BAR relocation invalidates former bus addresses.
        config(4,0,0x24,0x20)
        n,data=submit(0,2);finish(0,n,data,False)
        config(4,0,0x24,bars[1]>>32)
        n,data=submit(0,2);finish(0,n,data)
        # Explicit P2P policy can be changed by the local control plane.
        for mode in (1,2):
            wr(regs[0]+0x381084,0)
            n,data=submit(0,mode);finish(0,n,data,False)
            wr(regs[0]+0x381084,1)
            n,data=submit(0,mode);finish(0,n,data)
        # VT-d maps only card 1's Host IOVA. Card 0 has no Host domain at all;
        # its simultaneous peer transaction must still succeed.
        root=0x10000000;ctx=root+0x1000;l4=root+0x10000;l3=l4+0x1000;l2=l3+0x1000;l1=l2+0x1000
        iova=0x800000;pa=0x12000000;vtd=0xfed90000
        wq(root+4*16,ctx|1);wq(ctx,l4|1);wq(ctx+8,2)
        wq(l4,l3|3);wq(l3,l2|3);wq(l2+4*8,l1|3)
        for j in range(8):wq(l1+j*8,(pa+j*4096)|3)
        wq(vtd+0x20,root);wr(vtd+0x18,1<<30);wr(vtd+0x18,1<<31)
        check('vtd_enabled',bool(rd(vtd+0x1c)&(1<<31)))
        def hdma(i,src,dst,n=8192,reading=False):
            ch=regs[i]+0x380000+(0x100 if reading else 0)
            for off,v in [(0,1),(0x88,0),(0x8c,0xffffffff),(0x34,0x202),(0x1c,n),
                          (0x20,src&0xffffffff),(0x24,src>>32),(0x28,dst&0xffffffff),(0x2c,dst>>32),(4,1)]:wr(ch+off,v)
            return ch
        def channel_done(ch,success=True):
            return wait(ch+0x80,3) and bool(rd(ch+0x84)&(1 if success else 4))
        # Switch-local traffic does not traverse the upstream Host window.
        source=read(peer[0],8192)
        config(0,16,0x2c,0)
        ch=hdma(0,0x20010000,peer[1]+0x10000)
        check('switch_peer_with_host_window_closed',channel_done(ch))
        bridge(*bridges[0])
        check('switch_local_window_data',read(peer[1]+0x10000,8192)==source)
        n,data=submit(0,1)
        ch=hdma(1,0x20010000,iova+128)
        check('mixed_host_peer_hdma',channel_done(ch) and read(pa+128,8192)==data[:8192])
        finish(0,n,data)
        check('host_iova_not_gpa',read(iova+128,64)==bytes(64))
        ch=hdma(0,0x20010000,iova,64)
        check('missing_source_host_domain_aborts',channel_done(ch,False))
        check('real_host_vtd_fault',bool(rd(vtd+0x34)))
        ch=hdma(1,iova+128,0x20050000,8192,True)
        check('host_read_after_peer',channel_done(ch))
        # Hold an accepted channel, mutate the remote inbound, and restore it.
        # The old request must abort even though the resulting mapping is equal.
        for reading in (False,True):
            wr(regs[0]+0x381080,4 if reading else 8)
            src,dst=(peer[1],0x20050000) if reading else (0x20010000,peer[1]+0x10000)
            ch=hdma(0,src,dst,reading=reading)
            check(f'inflight_hdma_{reading}_accepted',wait(ch+0x80,1))
            before=read(peer[1]+0x10000,8192)
            inbound(1,False);inbound(1)
            wr(regs[0]+0x381080,0)
            check(f'inflight_hdma_{reading}_mapping_abort',channel_done(ch,False))
            check(f'inflight_hdma_{reading}_no_old_write',read(peer[1]+0x10000,8192)==before)
        def spawns(i):
            return [e['pid'] for e in map(json.loads,(work/f'service{i}/service.jsonl').read_text().splitlines()) if e['event']=='spawn']
        for i in range(2):check(f'card{i}_ordinary_tasks_one_spike',len(spawns(i))==1)
        def backing(i,size):
            fdroot=Path(f'/proc/{spawns(i)[-1]}/fd')
            path=next(p for p in fdroot.iterdir() if 'zc-p1-' in os.readlink(p) and p.stat().st_size==size)
            fd=os.open(path,os.O_RDWR)
            m=mmap.mmap(fd,size);os.close(fd);return m
        def fault_request(engine,reading,label):
            hold=(1 if reading else 2) if engine=='npu' else (4 if reading else 8)
            wr(regs[0]+0x381080,hold)
            if engine=='npu':
                job=submit(0,1,8192)
                with backing(0,4096) as mb:
                    end=time.monotonic()+15
                    while time.monotonic()<end:
                        if struct.unpack_from('<I',mb,0x500)[0]==1 and struct.unpack_from('<I',mb,0x508)[0]==(1 if reading else 2):break
                        time.sleep(.002)
                    check(label+'_accepted',struct.unpack_from('<I',mb,0x500)[0]==1 and struct.unpack_from('<I',mb,0x508)[0]==(1 if reading else 2))
                check(label+'_route_pinned',wait(regs[0]+0x381088,job[0]))
                return job
            src,dst=(peer[1],0x20050000) if reading else (0x20010000,peer[1]+0x10000)
            ch=hdma(0,src,dst,reading=reading)
            check(label+'_accepted',wait(ch+0x80,1))
            return ch
        # In-flight mutation matrix. Restore the same configuration before
        # releasing the deterministic hold: previous requests still abort.
        for engine in ('npu','hdma'):
            for reading in (False,True):
                for mutation in ('inbound','bar','mse','window','policy','bme','outbound'):
                    label=f'{engine}_{"read" if reading else "write"}_mutate_{mutation}'
                    request=fault_request(engine,reading,label)
                    before=read(peer[1]+0x10000,8192)
                    if mutation=='inbound':inbound(1,False);inbound(1)
                    elif mutation=='bar':config(4,0,0x24,0x20);config(4,0,0x24,bars[1]>>32)
                    elif mutation=='mse':config(4,0,4,4);config(4,0,4,6)
                    elif mutation=='window':config(2,16,0x2c,0);bridge(*bridges[3])
                    elif mutation=='policy':wr(regs[0]+0x381084,0);wr(regs[0]+0x381084,1)
                    elif mutation=='bme':config(3,0,4,2);config(3,0,4,6)
                    else:outbound(0)
                    wr(regs[0]+0x381080,0)
                    if engine=='npu':finish(0,*request,False)
                    else:check(label+'_aborted',channel_done(request,False))
                    check(label+'_no_old_peer_write',read(peer[1]+0x10000,8192)==before)
                    n,data=submit(0,1);finish(0,n,data)
        # Range and target-class guards, observed in actual backing memory.
        with backing(1,1<<30) as ddr:
            guard=bytes((j*19+7)&255 for j in range(4096))
            ddr[0x30000:0x31000]=guard
            ch=hdma(0,0x20010000,peer[1]+0x1f000,8192)
            check('peer_cross_inbound_limit_aborts',channel_done(ch,False))
            check('peer_limit_guard_unchanged',ddr[0x30000:0x31000]==guard)
            # Invalid first matching region cannot fall through to RAM.
            inbound(1,target=0x21380000)
            ch=hdma(0,0x20010000,peer[1],64)
            check('peer_controller_target_rejected',channel_done(ch,False))
            inbound(1,target=0x10000000)
            ch=hdma(0,0x20010000,peer[1],64)
            check('peer_non_ddr_target_rejected',channel_done(ch,False))
            inbound(1)
        # Original Tensor execution also consumes/produces peer DDR, using the
        # same pinned standalone reference as P2 (not a numerical golden).
        payload=bytearray(0x9200)
        data_root=ROOT/'moe_bf16_sint4_33x29x34_fix/data'
        for name,off in [('act_l1.bin',0),('wgt_l1.bin',0x8400),('wgt_scale_l1.bin',0x8a00),('moe_index_l1.bin',0x9000)]:
            content=(data_root/name).read_bytes();payload[off:off+len(content)]=content
        args=(ROOT/'simulator/riscv-tests/packet_runtime_test/tensor-args.bin').read_bytes()
        expected=(ROOT/'experiments/single_die_packet/run-20261027T100512-648188Z/moe/data/result_l1_spike.bin').read_bytes()
        check('tensor_reference_pinned',hashlib.sha256(expected).hexdigest()=='37121ee2cd6afaed02f18587ed683d6fb5d5c4a6cd3b078f682a77891fe7e015')
        for mode in (1,2,3,4):
            for i in range(2):
                for ch in (0,0x100):wr(regs[i]+0x38008c+ch,0xffffffff)
                counts[i]+=1;number=counts[i];p=bars[i]+0x10000100;target=peer[1-i]
                write(target,bytes(payload));write(target+0x10000,bytes(len(expected)))
                for j in range(32):wr(p+0x80+j*4,struct.unpack_from('<I',args,j*4)[0])
                for off,v in [(4,mode),(8,len(payload)),(12,target&0xffffffff),(16,target>>32),
                              (20,(target+0x10000)&0xffffffff),(24,(target+0x10000)>>32),(28,1)]:wr(p+off,v)
                wr(p,number)
                label=f'tensor_card{i}_mode{mode}'
                check(label+'_done',wait(bars[i]+0x1000002c,number))
                check(label+'_status',rd(bars[i]+0x10000020)==0)
                check(label+'_all_bytes',read(target+0x10000,len(expected))==expected)
                wr(p+28,0)
        # Disconnect one service at a time, proving both source and target
        # lifetime changes invalidate accepted operations in both engines.
        for engine in ('npu','hdma'):
            for reading in (False,True):
                for lost in (0,1):
                    label=f'{engine}_{"read" if reading else "write"}_disconnect_{"source" if lost==0 else "target"}'
                    old_pids=[spawns(i)[-1] for i in range(2)]
                    old_counts=[len(spawns(i)) for i in range(2)]
                    request=fault_request(engine,reading,label)
                    with backing(1,1<<30) as old_ddr:
                        before=old_ddr[0x20000:0x22000]
                        stop(procs[lost]);check(label+'_ready_withdrawn',wait(regs[lost]+8,0))
                        if lost==1:
                            if engine=='npu':finish(0,*request,False)
                            else:check(label+'_aborted',channel_done(request,False))
                        with (work/f'service{lost}-restart.log').open('a') as log:
                            service=subprocess.Popen([sys.executable,str(ROOT/'simulator/tools/p1/service.py'),
                                '--socket',str(sockets[lost]),'--config',str(cfg),'--work',str(work/f'service{lost}'),
                                '--card',str(lost)],stdout=log,stderr=subprocess.STDOUT)
                        procs[lost]=service
                        check(label+'_recovered',wait(regs[lost]+8,1))
                        check(label+'_old_process_reaped',not Path(f'/proc/{old_pids[lost]}').exists())
                        check(label+'_one_replacement',len(spawns(lost))==old_counts[lost]+1)
                        check(label+'_other_card_same_spike',spawns(1-lost)[-1]==old_pids[1-lost])
                        check(label+'_no_old_peer_write',old_ddr[0x20000:0x22000]==before)
                    wr(regs[0]+0x381080,0)
                    inbound(lost);outbound(lost)
                    if lost==0:counts[0]=0
                    n,data=submit(0,1);finish(0,n,data)
                    notifications=rd(regs[0]+16);time.sleep(.03)
                    check(label+'_no_duplicate_completion',rd(regs[0]+16)==notifications)
        # QEMU reset covers both endpoints: accepted operations do not survive
        # reset, both backends are reaped before their single replacement.
        for engine in ('npu','hdma'):
            for reading in (False,True):
                label=f'{engine}_{"read" if reading else "write"}_system_reset'
                old_pids=[spawns(i)[-1] for i in range(2)]
                old_counts=[len(spawns(i)) for i in range(2)]
                fault_request(engine,reading,label)
                with backing(1,1<<30) as old_ddr:
                    before=old_ddr[0x20000:0x22000]
                    command('system_reset')
                    for br in bridges:bridge(*br)
                    for i in range(2):
                        for o,v in [(0x10,regs[i]),(0x14,0),(0x20,bars[i]&0xffffffff),(0x24,bars[i]>>32),(4,6)]:config(3+i,0,o,v)
                        check(label+f'_card{i}_ready',wait(regs[i]+8,1))
                        check(label+f'_card{i}_reaped',not Path(f'/proc/{old_pids[i]}').exists())
                        check(label+f'_card{i}_one_replacement',len(spawns(i))==old_counts[i]+1)
                        check(label+f'_card{i}_no_false_completion',rd(bars[i]+0x1000002c)==0)
                    check(label+'_no_old_peer_write',old_ddr[0x20000:0x22000]==before)
                for i in range(2):inbound(i);outbound(i)
                counts[:]=[0,0]
                jobs=[submit(i,1) for i in range(2)]
                for i,job in enumerate(jobs):finish(i,*job)
        for i in range(2):
            events=[json.loads(x) for x in (work/f'service{i}/service.jsonl').read_text().splitlines()]
            spawned=[e['pid'] for e in events if e['event']=='spawn']
            check(f'card{i}_current_spike_alive',bool(spawned) and Path(f'/proc/{spawned[-1]}').exists())
        trace=[json.loads(x) for x in (work/'qemu.log').read_text().splitlines() if x.startswith('{"event":"p2_host"')]
        for engine in ('model_proxy','hdma_read','hdma_write'):
            for card in range(2):
                check(f'trace_card{card}_{engine}_peer',any(t['card']==card and t['engine']==engine and t['path']=='switch_peer' and t['peer_card']==1-card and t['memtx']==0 for t in trace))
        check('trace_host_and_peer_domains',any(t['path']=='host_dma_as' and t['memtx']==0 for t in trace) and any(t['path']=='switch_peer' and t['memtx']==0 for t in trace))
        mf.close();qm.close();qt.close()
        passed=True
    finally:
        for p in reversed(procs):stop(p)
        for p in sockets:p.unlink(missing_ok=True)
        trace=[]
        if (work/'qemu.log').exists():
            for line in (work/'qemu.log').read_text(errors='replace').splitlines():
                if line.startswith('{"event":"p2_host"'):trace.append(json.loads(line))
        (work/'results.json').write_text(json.dumps(dict(passed=passed,p3_full_exit=False,checks=checks,
            trace_count=len(trace),source_sha256={str(p.relative_to(ROOT)):digest(p) for p in
            [ROOT/'qemu/hw/misc/zc-pcie-p3-route.h',ROOT/'qemu/hw/misc/zc-pcie-p2-engine.h',Path(__file__)]}),indent=2))
        print('Evidence:',work,flush=True)
    return 0 if passed else 1

if __name__=='__main__':sys.exit(main())
