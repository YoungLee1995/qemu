#!/usr/bin/env python3
"""Two/eight real persistent backends driven by the new communication library."""
import argparse
from dataclasses import asdict
import hashlib
import json
import os
import subprocess
from pathlib import Path
import sys
import time
from backend import Platform, DMAAdapter, Fabric, LinkConfig, Communication
HERE = Path(__file__).resolve().parent


class P5Platform(Platform):
    def __init__(self,*args,**kwargs):
        from backend import ROOT
        kwargs['runtime_dir']=ROOT/'simulator/tools/p5/runtime'
        super().__init__(*args,**kwargs)
        self.fabric_socket=Path(f'/tmp/zc-p5-fabric-{os.getpid()}-{time.time_ns()%1000000}.sock')
        self.fabric_process=None
    def prepare(self):
        super().prepare()
        self.start_fabric()
    def start_fabric(self):
        from backend import ROOT
        cfg=json.loads((ROOT/'simulator/tools/p5/profiles/gen4-eight.json').read_text())
        cfg['cards']=self.count
        (self.work/'fabric-config.json').write_text(json.dumps(cfg,indent=2))
        with (self.work/'fabric-service.log').open('w') as log:
            self.fabric_process=subprocess.Popen([sys.executable,str(ROOT/'simulator/tools/p5/fabric_service.py'),
                '--socket',str(self.fabric_socket),'--config',str(self.work/'fabric-config.json'),
                '--out',str(self.work/'live-fabric.json')],stdout=log,stderr=subprocess.STDOUT)
        end=time.monotonic()+10
        while not self.fabric_socket.exists() and time.monotonic()<end:
            if self.fabric_process.poll() is not None:raise RuntimeError('fabric service failed')
            time.sleep(.005)
        if not self.fabric_socket.exists():raise TimeoutError('fabric service socket')
    def device_args(self):
        return [a.replace('p4-isolation=on,', f'p4-isolation=on,p5-flr=on,p5-fabric-socket={self.fabric_socket},') for a in super().device_args()]
    def close(self):
        try:super().close()
        finally:
            from cluster_platform import stop
            stop(self.fabric_process)
            self.fabric_socket.unlink(missing_ok=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--cards', type=int, choices=(2, 8), default=8)
    args = ap.parse_args()
    work = HERE / 'evidence' / f'communication-{args.cards}-{time.time_ns()}'
    p = P5Platform(work, args.cards)
    checks, runs = {}, []
    passed = False
    def check(name, value):
        if name in checks:
            raise ValueError('duplicate check '+name)
        checks[name] = bool(value)
        print(name, 'PASS' if value else 'FAIL', flush=True)
        if not value:
            raise RuntimeError(name)
    def save(name, fabric, adapter):
        out = fabric.report()
        out['actual_dma_transfers'] = adapter.transfers
        (work / f'{name}.json').write_text(json.dumps(out, indent=2))
        runs.append(dict(name=name, transactions=len(adapter.transfers), virtual_ns=fabric.now))
    try:
        p.start()
        for i in range(args.cards):
            p.inbound(i)
        initial = [p.spawns(i) for i in range(args.cards)]
        f = Fabric(cards=args.cards, mps=128, mrrs=512, tags=2, ordering='pci_relaxed',
                   default=LinkConfig(header_credits=2, data_credits=8, credit_return_ns=200))
        a = DMAAdapter(p)
        comm = Communication(a, f, depth=args.cards+1)
        # All directed pairs admitted together; target receives use independent
        # per-source slots. The scheduler interleaves actual DMA fragments.
        jobs = []
        for src in range(args.cards):
            for dst in range(args.cards):
                if src == dst:
                    continue
                data = bytes((j*29+src*17+dst)&255 for j in range(517))
                t = comm.send(src, dst, data, offset=src*0x1000, queue=dst % 2)
                jobs.append((t, data))
        comm.barrier()
        for t, data in jobs:
            check(f'send_{t.source}_{t.target}',comm.wait(t) == data)
        check('all_directed_pairs',len(jobs)==args.cards*(args.cards-1))
        check('two_software_queues',len({t.queue for t in comm.history})==2 if args.cards==8 else True)
        # Raw peer reads exercise the complete reverse Completion path.
        reads=[]
        for i in range(args.cards):
            target=(i+1)%args.cards
            data=bytes((j+i*31)&255 for j in range(1031))
            p.write(p.peer(target)+0x10000,data)
            t=comm.read(i,target,len(data),offset=0x10000,queue=3)
            reads.append((t,data))
        comm.barrier()
        for t,data in reads:
            check(f'read_{t.source}_{t.target}',comm.wait(t)==data)
        # Full queue, wrap/reuse, overlap rejection, and end-to-end dependency.
        for round in range(3):
            job=comm.send(0,1,bytes([round])*259,offset=0x18000,queue=4)
            check(f'queue_reuse_{round}',comm.wait(job)==bytes([round])*259)
        check('credits_returned',f.report()['credits_balanced'])
        check('real_dma_for_every_data_packet',len(a.transfers)==sum(x.kind in ('MemWr','CplD') for x in f.packets.values()))
        check('read_no_posted_completion',all(x.kind!='CplD' or not f.requests[x.request].write for x in f.packets.values()))
        save('all-pairs',f,a)
        # Non-identity VT-d mappings; identical IOVA belongs to different
        # requesters. These are real Host DMA operations through the RC.
        root=0x10000000;iova=0x800000;gpas=[0x12000000+i*0x200000 for i in range(args.cards)]
        leaves=[]
        for i,c in enumerate(p.cards):
            ctx=0x10010000+i*0x1000;l4=0x10080000+i*0x10000;l3=l4+0x1000;l2=l3+0x1000;l1=l2+0x1000
            leaves.append(l1)
            p.wq(root+c['bus']*16,ctx|1);p.wq(ctx,l4|1);p.wq(ctx+8,(i+1)<<8|2)
            p.wq(l4,l3|3);p.wq(l3,l2|3);p.wq(l2+4*8,l1|3)
            for j in range(32):p.wq(l1+j*8,(gpas[i]+j*4096)|3)
        vtd=0xfed90000;p.wq(vtd+0x20,root);p.wr(vtd+0x18,1<<30);p.wr(vtd+0x18,1<<31)
        check('real_host_iommu_enabled',bool(p.rd(vtd+0x1c)&(1<<31)))
        fh=Fabric(cards=args.cards,ordering='pci_relaxed');ah=DMAAdapter(p,iova,gpas);ch=Communication(ah,fh)
        host_jobs=[]
        for i in range(args.cards):
            data=bytes((i*19+j)&255 for j in range(1031))
            host_jobs.append((ch.send(i,-1,data,queue=1),data))
        ch.barrier()
        for t,data in host_jobs:check(f'host_write_domain_{t.source}',ch.wait(t)==data)
        reads=[(ch.read(i,-1,len(data),queue=2),data) for i,(_,data) in enumerate(host_jobs)]
        ch.barrier()
        for t,data in reads:check(f'host_read_domain_{t.source}',ch.wait(t)==data)
        check('host_no_identity_bypass',p.read(iova,128)==bytes(128))
        check('shared_uplink_measured','host:up' in fh.stats and fh.stats['host:up']['packets']>args.cards)
        save('host-iommu',fh,ah)
        # Runtime computation still works after the communications library used
        # another channel pair, proving it did not replace MCU/NPU execution.
        for i in range(args.cards):
            target=(i+1)%args.cards;data=bytes((j+i)&255 for j in range(1024))
            p.write(p.peer(target),data);p.outbound(i,p.peer(target))
            number=p.submit(i,1,p.peer(target),p.peer(target)+0x10000,len(data))
            check(f'runtime_task_{i}',p.finish(i,number)==0 and p.read(p.peer(target)+0x10000,len(data))==bytes(x^0x5a for x in data))
        check('normal_jobs_same_spike_pids',all(p.spawns(i)==initial[i] for i in range(args.cards)))
        # Real target decode error: software completion must fail, no success
        # and no remote writes, then restore mapping and retry without restart.
        f2=Fabric(cards=args.cards);a2=DMAAdapter(p);c2=Communication(a2,f2)
        before=p.read(p.peer(1)+0x1c000,512)
        p.wr(p.cards[1]['bar0']+0x300104,0)
        bad=c2.send(0,1,b'z'*512,offset=0x1c000)
        try:c2.wait(bad)
        except RuntimeError:pass
        check('actual_inbound_error',bad.state=='error' and 'HDMA abort' in bad.error)
        p.inbound(1)
        check('error_no_write',p.read(p.peer(1)+0x1c000,512)==before)
        good=c2.send(0,1,b'y'*512,offset=0x1c000)
        check('same_process_error_retry',c2.wait(good)==b'y'*512 and p.spawns(0)==initial[0])
        save('route-error',f2,a2)
        # Queue reset cancels accepted work; other cards/queues can progress.
        f3=Fabric(cards=args.cards);a3=DMAAdapter(p);c3=Communication(a3,f3)
        doomed=c3.send(0,1,b'x'*128,offset=0x1e000)
        c3.reset(0)
        check('queue_reset_cancels',doomed.state=='error' and not a3.transfers)
        retry=c3.send(0,1,b'r'*128,offset=0x1e000)
        check('queue_reset_reuse',c3.wait(retry)==b'r'*128 and p.spawns(0)==initial[0])
        save('queue-reset',f3,a3)
        # Standard PCIe FLR is newly advertised only with p5-flr=on. Hold source
        # HDMA in flight and reset that function, not the whole QEMU machine.
        victim=args.cards-1;c=p.cards[victim]
        caps=p.cfgread(c['bus'],0,0x84)
        check('flr_advertised',bool(caps&(1<<28)))
        before_other=[p.spawns(i) for i in range(args.cards-1)]
        before_victim=p.spawns(victim)
        p.wr(c['bar0']+0x381080,8)
        addr=c['bar0']+0x380200
        for off,val in [(0,1),(0x8c,0xffffffff),(0x34,0x202),(0x1c,128),
                        (0x20,0x20010000),(0x24,0),(0x28,p.peer(0)&0xffffffff),
                        (0x2c,p.peer(0)>>32),(4,1)]:p.wr(addr+off,val)
        check('flr_inflight_accepted',p.rd(addr+0x80)==1)
        snapshot=p.read(p.peer(0),128)
        old=p.cfgread(c['bus'],0,0x88)
        p.config(c['bus'],0,0x88,old|(1<<15))
        check('flr_bit_cleared',not(p.cfgread(c['bus'],0,0x88)&(1<<15)))
        # QEMU's generic pci_device_reset clears command/BAR config. The
        # software reset caller restores this function's resources only.
        for o,v in [(0x10,c['bar0']),(0x14,0),(0x18,c['bar2']),(0x1c,0),
                    (0x20,c['bar4']&0xffffffff),(0x24,c['bar4']>>32),(4,6)]:p.config(c['bus'],0,o,v)
        check('flr_recovers',p.wait(c['bar0']+8,1,60))
        check('flr_one_backend_replaced',len(p.spawns(victim))==len(before_victim)+1)
        check('flr_unrelated_pids',all(p.spawns(i)==before_other[i] for i in range(args.cards-1)))
        check('flr_no_stale_write',p.read(p.peer(0),128)==snapshot)
        p.inbound(victim);p.outbound(victim,p.peer(0));data=b'k'*128;p.write(p.peer(0),data)
        number=p.submit(victim,1,p.peer(0),p.peer(0)+0x10000,128)
        check('flr_runtime_reuse',p.finish(victim,number)==0 and p.read(p.peer(0)+0x10000,128)==bytes(x^0x5a for x in data))
        passed=True
    finally:
        p.close()
        try:
            if passed:
                report=json.loads((work/'live-fabric.json').read_text())
                check('live_fabric_saw_actual_dma',len(report['qemu_admission'])>len(a.transfers))
                check('live_fabric_credit_balanced',report['credits_balanced'])
                slots={x['slot'] for x in report['qemu_admission']}
                check('live_hdma_and_npu_admission',16 in slots and 2 in slots)
        except BaseException:
            passed=False
            raise
        finally:
            (work/'results.json').write_text(json.dumps(dict(passed=passed,cards=args.cards,checks=checks,runs=runs),indent=2))
            print('Evidence:',work,flush=True)

if __name__=='__main__':main()
