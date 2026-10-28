#!/usr/bin/env python3
"""Eight real persistent backends, all directed pairs, domains and recovery."""
import json
import hashlib
import os
from pathlib import Path
import signal
import struct
import sys
import time
from cluster_platform import Platform,HERE,ROOT,digest,stop


def main():
    work=HERE/'evidence'/f'cluster-{time.time_ns()}';p=Platform(work);checks={};passed=False;coverage=[]
    def check(name,value):
        if name in checks:raise ValueError('duplicate check '+name)
        checks[name]=bool(value);print(name,'PASS' if value else 'FAIL',flush=True)
        if not value:raise RuntimeError(name)
    def payload(src,dst,seed,n=4099):return bytes((j*37+src*11+dst*17+seed)&255 for j in range(n))
    def peer_round(shift,mode,seed,data_seed=None):
        targets=[(i+shift)%8 for i in range(8)]
        for i,dst in enumerate(targets):p.outbound(i,p.peer(dst))
        jobs=[]
        for i,dst in enumerate(targets):
            data=payload(i,dst,seed if data_seed is None else data_seed);a=p.peer(dst)
            p.write(a,data);p.write(a+0x10000,bytes(len(data)))
            jobs.append((i,dst,p.submit(i,mode,a,a+0x10000,len(data)),data))
        for i,dst,number,data in jobs:
            label=f'peer_m{mode}_s{i}_d{dst}_seed{seed}'
            check(label+'_status',p.finish(i,number)==0)
            check(label+'_data',p.read(p.peer(dst)+0x10000,len(data))==bytes(x^0x5a for x in data))
            coverage.append(dict(source=i,target=dst,mode=mode,seed=seed,bytes=len(data),input_sha256=hashlib.sha256(data).hexdigest()))
    def hdma(i,src,dst,n=8192,reading=False,channel=1):
        a=p.cards[i]['bar0']+0x380000+channel*0x200+(0x100 if reading else 0)
        for o,v in [(0,1),(0x88,0),(0x8c,0xffffffff),(0x34,0x202),(0x1c,n),
                    (0x20,src&0xffffffff),(0x24,src>>32),(0x28,dst&0xffffffff),(0x2c,dst>>32),(4,1)]:p.wr(a+o,v)
        return a
    def done(a,success=True):return p.wait(a+0x80,3) and bool(p.rd(a+0x84)&(1 if success else 4))
    try:
        p.start();check('eight_real_ready',all(p.rd(c['bar0']+8)==1 for c in p.cards))
        config=[]
        for c in p.cards:
            i=c['card'];values={str(o):p.cfgread(c['bus'],0,o) for o in range(0,0x28,4)};config.append(values)
            check(f'card{i}_identity',values['0']==0xabcd16c3)
            for bar,size,attr in [(0,0x400000,4),(2,0x2000000,4),(4,0x800000000,12)]:
                o=0x10+bar*4;old=p.cfgread(c['bus'],0,o);hi=p.cfgread(c['bus'],0,o+4)
                p.config(c['bus'],0,o,0xffffffff);p.config(c['bus'],0,o+4,0xffffffff)
                mask=(p.cfgread(c['bus'],0,o)&0xfffffff0)|(p.cfgread(c['bus'],0,o+4)<<32)
                check(f'card{i}_bar{bar}_size_attr',((~mask)&((1<<64)-1))+1==size and old&15==attr)
                p.config(c['bus'],0,o,old);p.config(c['bus'],0,o+4,hi)
            p.inbound(i)
        (work/'config-space.json').write_text(json.dumps(config,indent=2))
        for mode in (1,2,3,4):
            for shift in range(1,8):peer_round(shift,mode,1)
        # Repeat the all-to-all matrix three times with fixed input (mode 1).
        for repeat in (2,3):
            for shift in range(1,8):peer_round(shift,1,repeat,data_seed=1)
        check('same_input_three_repeats',all(len({r['input_sha256'] for r in coverage if r['source']==i and r['target']==j and r['mode']==1 and r['seed'] in (1,2,3)})==1 for i in range(8) for j in range(8) if i!=j))
        check('56_directed_pairs_all_four_modes',all(len({(r['source'],r['target']) for r in coverage if r['mode']==m})==56 for m in (1,2,3,4)))
        initial=[p.spawns(i) for i in range(8)]
        check('ordinary_tasks_eight_persistent_spikes',all(len(x)==1 for x in initial))
        # Eight separate VT-d source contexts, same IOVA, distinct target GPA.
        root=0x10000000;iova=0x800000;pas=[0x12000000+i*0x200000 for i in range(8)]
        leaves=[]
        for i,c in enumerate(p.cards):
            ctx=0x10010000+i*0x1000;l4=0x10080000+i*0x10000;l3=l4+0x1000;l2=l3+0x1000;l1=l2+0x1000;leaves.append(l1)
            p.wq(root+c['bus']*16,ctx|1);p.wq(ctx,l4|1);p.wq(ctx+8,(i+1)<<8|2)
            p.wq(l4,l3|3);p.wq(l3,l2|3);p.wq(l2+4*8,l1|3)
            for j in range(32):p.wq(l1+j*8,(pas[i]+j*4096)|3)
        vtd=0xfed90000;p.wq(vtd+0x20,root);p.wr(vtd+0x18,1<<30);p.wr(vtd+0x18,1<<31)
        check('vtd_translation_on',bool(p.rd(vtd+0x1c)&(1<<31)))
        def invalidate():p.wq(vtd+0x28,(1<<63)|(1<<61));p.wq(vtd+0xf8,(1<<63)|(1<<60))
        for mode in (1,2,3,4):
            for i in range(8):p.outbound(i,iova)
            jobs=[]
            for i in range(8):
                data=payload(i,i,mode);p.write(pas[i],data);p.write(pas[i]+0x10000,bytes(len(data)))
                jobs.append((p.submit(i,mode,iova,iova+0x10000,len(data)),data))
            for i,(number,data) in enumerate(jobs):
                check(f'host_m{mode}_card{i}_status',p.finish(i,number)==0)
                check(f'host_m{mode}_card{i}_domain_data',p.read(pas[i]+0x10000,len(data))==bytes(x^0x5a for x in data))
        check('host_iova_is_not_gpa',p.read(iova,64)==bytes(64))
        # Read-only and revoked source-7 domain do not affect other domains.
        p.wq(leaves[7],pas[7]|1);invalidate()
        with p.backing(7,1<<30) as mem:mem[0x10000:0x12000]=bytes([0x79])*8192
        before=p.read(pas[7],4096);a=hdma(7,0x20010000,iova,4096)
        check('card7_readonly_host_write_abort',done(a,False) and p.read(pas[7],4096)==before)
        p.wq(leaves[7],0);invalidate();a=hdma(7,iova,0x20010000,64,True)
        check('card7_revoked_host_read_abort',done(a,False));check('actual_vtd_fault',bool(p.rd(vtd+0x34)))
        p.wq(leaves[7],pas[7]|3);invalidate()
        # Eight NPU peer tasks plus eight HDMA Host writes, then Host reads.
        for i in range(8):p.outbound(i,p.peer((i+1)%8))
        jobs=[];host=[]
        for i in range(8):
            dst=(i+1)%8;data=payload(i,dst,99,8192);p.write(p.peer(dst),data);p.write(p.peer(dst)+0x10000,bytes(8192))
            jobs.append((p.submit(i,1,p.peer(dst),p.peer(dst)+0x10000,8192),data))
        for i in range(8):
            expected=p.read(p.peer(i),8192);p.write(pas[i],bytes(8192));host.append((hdma(i,0x20010000,iova),expected))
        for i,(a,data) in enumerate(host):check(f'mixed_card{i}_host_write',done(a) and p.read(pas[i],8192)==data)
        for i,(number,data) in enumerate(jobs):check(f'mixed_card{i}_peer',p.finish(i,number)==0 and p.read(p.peer((i+1)%8)+0x10000,8192)==bytes(x^0x5a for x in data))
        host=[hdma(i,iova,0x20050000,8192,True) for i in range(8)]
        for i,a in enumerate(host):
            with p.backing(i,1<<30) as mem:check(f'mixed_card{i}_host_read',done(a) and mem[0x50000:0x52000]==p.read(pas[i],8192))
        # All cards execute the original Tensor kernel in each engine combination.
        blob=bytearray(0x9200);droot=ROOT/'moe_bf16_sint4_33x29x34_fix/data'
        for name,off in [('act_l1.bin',0),('wgt_l1.bin',0x8400),('wgt_scale_l1.bin',0x8a00),('moe_index_l1.bin',0x9000)]:
            data=(droot/name).read_bytes();blob[off:off+len(data)]=data
        args=(ROOT/'simulator/riscv-tests/packet_runtime_test/tensor-args.bin').read_bytes()
        expected=(ROOT/'experiments/single_die_packet/run-20261027T100512-648188Z/moe/data/result_l1_spike.bin').read_bytes()
        for mode in (1,2,3,4):
            for i in range(8):p.outbound(i,p.peer((i+1)%8))
            jobs=[]
            for i in range(8):
                dst=(i+1)%8;a=p.peer(dst);p.write(a,bytes(blob));p.write(a+0x10000,bytes(len(expected)))
                jobs.append(p.submit(i,mode,a,a+0x10000,len(blob),True,args))
            for i,number in enumerate(jobs):check(f'tensor_m{mode}_card{i}',p.finish(i,number)==0 and p.read(p.peer((i+1)%8)+0x10000,len(expected))==expected)
        check('all_normal_tasks_same_pids',all(p.spawns(i)==initial[i] for i in range(8)))
        # Fault a related source/target while six unrelated requests are held.
        # Their original processes, epochs, data and accepted operations survive.
        faults=('target_disconnect','source_disconnect','runtime_crash','target_bar','target_inbound','target_window')
        for engine in ('npu','hdma'):
            for reading in (False,True):
                for fault_index,fault in enumerate(faults):
                    label=f'isolate_{engine}_{"read" if reading else "write"}_{fault}'
                    for i in range(8):p.outbound(i,p.peer(i^1))
                    hold=(1 if reading else 2) if engine=='npu' else (4 if reading else 8)
                    jobs=[];oldpids=[p.spawns(i)[-1] for i in range(8)]
                    epochs=[p.rd(c['bar0']+20) for c in p.cards]
                    oldcounts=[len(p.spawns(i)) for i in range(8)]
                    for i in range(7):
                        dst=i^1;data=payload(i,dst,88,8192)
                        p.write(p.peer(dst),data);p.write(p.peer(dst)+0x10000,bytes(8192))
                        p.wr(p.cards[i]['bar0']+0x381080,hold)
                        if engine=='npu':
                            request=p.submit(i,1,p.peer(dst),p.peer(dst)+0x10000,8192)
                            with p.backing(i,4096) as mb:
                                end=time.monotonic()+30
                                while time.monotonic()<end:
                                    if struct.unpack_from('<I',mb,0x500)[0]==1 and struct.unpack_from('<I',mb,0x508)[0]==(1 if reading else 2):break
                                    time.sleep(.002)
                                check(label+f'_card{i}_accepted',struct.unpack_from('<I',mb,0x500)[0]==1 and struct.unpack_from('<I',mb,0x508)[0]==(1 if reading else 2))
                            check(label+f'_card{i}_pinned',p.wait(p.cards[i]['bar0']+0x381088,request))
                        else:
                            src,dest=(p.peer(dst),0x20050000) if reading else (0x20010000,p.peer(dst)+0x10000)
                            request=hdma(i,src,dest,reading=reading)
                            check(label+f'_card{i}_accepted',p.wait(request+0x80,1))
                        jobs.append((request,data))
                    with p.backing(7,1<<30) as target_mem,p.backing(6,1<<30) as source_mem:
                        before_target=target_mem[0x20000:0x22000];before_source=source_mem[0x50000:0x52000]
                        lost=6 if fault=='source_disconnect' else 7
                        if fault in ('target_disconnect','source_disconnect'):
                            stop_service=p.services[lost]
                            stop(stop_service);check(label+'_withdrawn',p.wait(p.cards[lost]['bar0']+8,0))
                            p.start_service(lost)
                        elif fault=='runtime_crash':os.kill(oldpids[7],signal.SIGKILL)
                        elif fault=='target_bar':
                            c=p.cards[7];p.config(c['bus'],0,0x24,0x80);p.config(c['bus'],0,0x24,c['bar4']>>32)
                        elif fault=='target_window':
                            old_limit=p.cfgread(2,8*8,0x2c)
                            p.config(2,8*8,0x2c,0);p.config(2,8*8,0x2c,old_limit)
                        else:p.inbound(7)
                        if fault in ('target_disconnect','source_disconnect','runtime_crash'):
                            end=time.monotonic()+30
                            while len(p.spawns(lost))==oldcounts[lost] and time.monotonic()<end:time.sleep(.005)
                            check(label+'_recovered',p.wait(p.cards[lost]['bar0']+8,1))
                            check(label+'_reaped_once',not Path(f'/proc/{oldpids[lost]}').exists() and len(p.spawns(lost))==oldcounts[lost]+1)
                            if lost==6:check(label+'_no_false_completion',p.rd(p.cards[6]['bar4']+0x1000002c)==0)
                        for i in range(7):p.wr(p.cards[i]['bar0']+0x381080,0)
                        for i,(request,data) in enumerate(jobs):
                            dst=i^1
                            if i==6:
                                if fault!='source_disconnect':
                                    check(label+'_related_abort',p.finish(i,request)!=0 if engine=='npu' else done(request,False))
                            else:
                                check(label+f'_unrelated_card{i}_completed',p.finish(i,request)==0 if engine=='npu' else done(request))
                                if engine=='npu':check(label+f'_unrelated_card{i}_data',p.read(p.peer(dst)+0x10000,8192)==bytes(x^0x5a for x in data))
                                elif reading:
                                    with p.backing(i,1<<30) as mem:check(label+f'_unrelated_card{i}_data',mem[0x50000:0x52000]==data)
                                else:check(label+f'_unrelated_card{i}_data',p.read(p.peer(dst)+0x10000,8192)==p.read(p.peer(i),8192))
                        check(label+'_no_late_peer_write',target_mem[0x20000:0x22000]==before_target)
                        if engine=='hdma' and reading:check(label+'_no_late_read_response',source_mem[0x50000:0x52000]==before_source)
                    check(label+'_unrelated_pids_epochs',all(p.spawns(i)[-1]==oldpids[i] and p.rd(p.cards[i]['bar0']+20)==epochs[i] for i in range(6)))
                    if fault in ('target_disconnect','source_disconnect','runtime_crash'):
                        p.inbound(lost);p.outbound(lost,p.peer(lost^1));p.counts[lost]=0
                    # New tasks on the recovered endpoints use the new session.
                    recovery_seed=1000+(0 if engine=='npu' else 100)+(10 if reading else 0)+fault_index
                    peer_round(1,1,recovery_seed)
        # NPU proxy timeout is a fatal backend error in this profile; the
        # service must reap/reconnect that card while all other cards complete.
        for i in range(8):p.outbound(i,p.peer((i+1)%8))
        old=[p.spawns(i)[-1] for i in range(8)];old_count=len(p.spawns(7))
        p.wr(p.cards[7]['bar0']+0x381080,1);jobs=[]
        for i in range(8):
            dst=(i+1)%8;data=payload(i,dst,50);p.write(p.peer(dst),data);p.write(p.peer(dst)+0x10000,bytes(len(data)))
            jobs.append((p.submit(i,1,p.peer(dst),p.peer(dst)+0x10000,len(data)),data))
        check('npu_timeout_request_pinned',p.wait(p.cards[7]['bar0']+0x381088,jobs[7][0]))
        for i,(number,data) in enumerate(jobs[:7]):check(f'npu_timeout_other_card{i}_completed',p.finish(i,number)==0 and p.read(p.peer(i+1)+0x10000,len(data))==bytes(x^0x5a for x in data))
        end=time.monotonic()+30
        while len(p.spawns(7))==old_count and time.monotonic()<end:time.sleep(.01)
        check('npu_timeout_one_replacement',p.wait(p.cards[7]['bar0']+8,1) and len(p.spawns(7))==old_count+1 and not Path(f'/proc/{old[7]}').exists())
        check('npu_timeout_other_processes_unchanged',all(p.spawns(i)[-1]==old[i] for i in range(7)))
        check('npu_timeout_no_peer_write',p.read(p.peer(0)+0x10000,len(jobs[7][1]))==bytes(len(jobs[7][1])))
        check('npu_timeout_no_false_completion',p.rd(p.cards[7]['bar4']+0x1000002c)==0)
        p.inbound(7);p.outbound(7,p.peer(0));p.counts[7]=0;peer_round(1,1,51)
        for reading in (False,True):
            label=f'channel_stop_{reading}'
            p.wr(p.cards[6]['bar0']+0x381080,4 if reading else 8)
            src,dst=(p.peer(7),0x20050000) if reading else (0x20010000,p.peer(7)+0x10000)
            a=hdma(6,src,dst,reading=reading)
            check(label+'_accepted',p.wait(a+0x80,1));before=p.read(p.peer(7)+0x10000,8192)
            p.wr(a,0);p.wr(p.cards[6]['bar0']+0x381080,0)
            check(label+'_abort',done(a,False));check(label+'_no_late_write',p.read(p.peer(7)+0x10000,8192)==before)
        # A backend runtime crash is recovered by its existing service/session.
        old=[p.spawns(i)[-1] for i in range(8)];old_count=len(p.spawns(7));os.kill(old[7],signal.SIGKILL)
        end=time.monotonic()+30
        while len(p.spawns(7))==old_count and time.monotonic()<end:time.sleep(.01)
        check('runtime_crash_card7_recovered',p.wait(p.cards[7]['bar0']+8,1) and len(p.spawns(7))==old_count+1 and not Path(f'/proc/{old[7]}').exists())
        p.inbound(7);p.outbound(7,p.peer(0));p.counts[7]=0
        check('runtime_crash_other_pids_unchanged',all(p.spawns(i)[-1]==old[i] for i in range(7)))
        peer_round(1,1,40)
        # Complete P3 already exercises engine/direction/reset/disconnect matrix;
        # here reset all eight and require one replacement per card, then rerun.
        old=[p.spawns(i)[-1] for i in range(8)];counts=[len(p.spawns(i)) for i in range(8)]
        p.command('system_reset');p.configure()
        for i,c in enumerate(p.cards):
            check(f'reset_card{i}_ready',p.wait(c['bar0']+8,1,60))
            check(f'reset_card{i}_reaped_once',not Path(f'/proc/{old[i]}').exists() and len(p.spawns(i))==counts[i]+1)
            check(f'reset_card{i}_no_false_completion',p.rd(c['bar4']+0x1000002c)==0)
            p.inbound(i)
        p.counts=[0]*8;peer_round(1,2,41)
        trace=[json.loads(x) for x in (work/'qemu.log').read_text().splitlines() if x.startswith('{"event":"p2_host"')]
        for engine in ('model_proxy','hdma_read','hdma_write'):
            pairs={(r['card'],r['peer_card']) for r in trace if r['engine']==engine and r['path']=='switch_peer' and r['memtx']==0}
            check(f'trace_{engine}_all56',len(pairs)==56)
        check('trace_host_all8',len({r['card'] for r in trace if r['path']=='host_dma_as' and r['memtx']==0})==8)
        passed=True
    finally:
        (work/'coverage.json').write_text(json.dumps(coverage,indent=2))
        p.close()
        (work/'results.json').write_text(json.dumps(dict(passed=passed,p4_full_exit=False,checks=checks),indent=2))
        print('Evidence:',work,flush=True)
    return 0 if passed else 1

if __name__=='__main__':sys.exit(main())
