#!/usr/bin/env python3
"""Generate and supervise the internal one-switch multi-card platform."""
import json
import mmap
import os
from pathlib import Path
import resource
import shutil
import socket
import struct
import subprocess
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'p1'))
from run_p1 import ROOT,SIM,SPIKE,FIRMWARE,ISA,QEMU,connect,stop,digest
HERE=Path(__file__).resolve().parent


def layout(count=8):
    if count not in (1,2,8):raise ValueError('supported card counts: 1, 2, 8')
    cards=[]
    for i in range(count):
        base=0xc0000000+i*0x4000000
        cards.append(dict(card=i,bus=i+3,devfn=0,bdf=f'{i+3:02x}:00.0',
            bar0=base,bar2=base+0x2000000,bar4=0x1000000000+i*0x800000000,
            downstream=f'ds{i}',mcu_hart=1,npu_hart=16,
            enabled_mcu=1,enabled_npu=1,ddr0_bytes=1<<30,ddr1_bytes=1<<38))
    return dict(schema=1,scope='internal-functional',cards=cards,peer_base=0x1000000000,
                peer_size=count*0x800000000,bar_sizes=[0x400000,0x2000000,0x800000000],
                total_enabled_mcu=count,total_enabled_npu=count)


class Platform:
    def __init__(self,work,count=8,trace=True,runtime_dir=None):
        self.work=Path(work).resolve();self.work.mkdir(parents=True,exist_ok=True)
        self.spec=layout(count);self.cards=self.spec['cards'];self.count=count
        self.runtime_dir=Path(runtime_dir) if runtime_dir else HERE.parent/'p2/runtime'
        self.trace=trace;self.services={};self.qemu=None;self.sockets=[]
        self.start_time=time.monotonic();self.usage_start=resource.getrusage(resource.RUSAGE_CHILDREN)
        self.counts=[0]*count;self.qt=self.qm=self.mf=None
        self.spec['created_hart_ids_per_backend']=[0,1,2,16,17,18,19]
        self.spec['created_processors_total']=count*7
        self.spec['enabled_runtime_harts_per_backend']=[1,16]
        self.spec['firmware_hart_per_backend']=0
        (self.work/'platform.json').write_text(json.dumps(self.spec,indent=2))
    def prepare(self):
        runtime=self.runtime_dir
        subprocess.run(['make','-C',str(runtime)],check=True,stdout=subprocess.PIPE)
        inputs=self.work/'inputs';inputs.mkdir()
        for name in ('mcu.bin','npu.bin'):shutil.copy2(runtime/name,inputs/name)
        fw=inputs/'firmware.cfg'
        fw.write_text('runtime_func:1\npc_num:2\n[1, 0x40000000, ./mcu.bin]\n[16, 0x4000000000, ./npu.bin]\nddr_input_num:0\nddr_output_num:0\nl1buf_input_num:0\nl1buf_output_num:0\n')
        files=[SPIKE,FIRMWARE,fw,inputs/'mcu.bin',inputs/'npu.bin',SIM/'smart/sm_system.cfg']
        self.cfg=self.work/'backend.json'
        self.cfg.write_text(json.dumps(dict(spike=str(SPIKE),firmware=str(FIRMWARE),isa=ISA,
            cfg=str(fw),cwd=str(inputs),p2=True,sha256={str(p):digest(p) for p in files}),indent=2))
        for i in range(self.count):
            self.sockets.append(Path(f'/tmp/zc-p4-{os.getpid()}-{i}.sock'))
            self.start_service(i)
    def start_service(self,i):
        with (self.work/f'service{i}.log').open('a') as log:
            self.services[i]=subprocess.Popen([sys.executable,str(ROOT/'simulator/tools/p1/service.py'),
                '--socket',str(self.sockets[i]),'--config',str(self.cfg),
                '--work',str(self.work/f'service{i}'),'--card',str(i)],stdout=log,stderr=subprocess.STDOUT)
        end=time.monotonic()+10
        while not self.sockets[i].exists() and time.monotonic()<end:
            if self.services[i].poll() is not None:raise RuntimeError('service exited')
            time.sleep(.005)
        if not self.sockets[i].exists():raise TimeoutError('service socket')
    def device_args(self):
        args=['-device','pcie-root-port,id=rp,addr=2,chassis=1',
              '-device','x3130-upstream,id=sw,bus=rp']
        for c in self.cards:
            i=c['card'];args+=['-device',f'xio3130-downstream,id=ds{i},bus=sw,addr={i+1:x},chassis={i+2}',
                '-device',f'zc-pcie-p1,id=ep{i},bus=ds{i},addr=0,p2=on,p3=on,p4-isolation=on,p2-trace={"on" if self.trace else "off"},card-id={i},peer-base={self.spec["peer_base"]},peer-size={self.spec["peer_size"]},backend-socket={self.sockets[i]},config-sha256={digest(self.cfg)}']
        return args
    def start(self):
        self.prepare()
        qt=Path(f'/tmp/zc-p4-{os.getpid()}-qt.sock');qm=Path(f'/tmp/zc-p4-{os.getpid()}-qm.sock');self.sockets.extend([qt,qm])
        args=[str(QEMU),'-machine','q35','-m','512M','-accel','qtest','-display','none','-nodefaults',
              '-global','q35-pcihost.pci-hole64-size=512G','-device','intel-iommu,caching-mode=on',
              '-qtest',f'unix:{qt},server=on,wait=off','-qtest-log','/dev/null',
              '-qmp',f'unix:{qm},server=on,wait=off']+self.device_args()
        (self.work/'qemu-command.json').write_text(json.dumps(args,indent=2))
        with (self.work/'qemu.log').open('w') as log:self.qemu=subprocess.Popen(args,stdout=log,stderr=subprocess.STDOUT)
        self.qt=connect(qt,self.qemu);self.qm=socket.socket(socket.AF_UNIX);self.qm.settimeout(30);self.qm.connect(str(qm))
        self.mf=self.qm.makefile('rwb',buffering=0);self.mf.readline();self.command('qmp_capabilities')
        self.configure()
        for i in range(self.count):
            if not self.wait(self.cards[i]['bar0']+8,1,60):raise TimeoutError(f'card {i} ready')
    def q(self,cmd):
        self.qt.write(cmd.encode()+b'\n');r=self.qt.readline().decode().split()
        if not r or r[0]!='OK':raise RuntimeError(r)
        return int(r[1],16) if len(r)>1 else 0
    def wr(self,a,v):self.q(f'writel {a:#x} {v:#x}')
    def wq(self,a,v):self.q(f'writeq {a:#x} {v:#x}')
    def rd(self,a):return self.q(f'readl {a:#x}')
    def config(self,b,d,o,v):
        self.q(f'outl 0xcf8 {0x80000000|(b<<16)|(d<<8)|o:#x}');self.q(f'outl 0xcfc {v:#x}')
    def cfgread(self,b,d,o):
        self.q(f'outl 0xcf8 {0x80000000|(b<<16)|(d<<8)|o:#x}');return self.q('inl 0xcfc')
    def bridge(self,b,d,primary,secondary,subordinate,low,high,pref,pref_end):
        for o,v in [(0x18,primary|(secondary<<8)|(subordinate<<16)),
                    (0x20,(low>>16)&0xfff0|(high&0xfff00000)),
                    (0x24,((pref>>16)&0xfff0)|1|(pref_end&0xfff00000)|0x10000),
                    (0x28,pref>>32),(0x2c,pref_end>>32),(4,6)]:self.config(b,d,o,v)
    def configure(self):
        end=self.spec['peer_base']+self.spec['peer_size']-1
        for b,d,p,s in [(0,16,0,1),(1,0,1,2)]:self.bridge(b,d,p,s,self.count+2,0xc0000000,0xc0000000+self.count*0x4000000-1,self.spec['peer_base'],end)
        for c in self.cards:
            i=c['card'];self.bridge(2,(i+1)*8,2,c['bus'],c['bus'],c['bar0'],c['bar0']+0x4000000-1,c['bar4'],c['bar4']+0x800000000-1)
            for o,v in [(0x10,c['bar0']),(0x14,0),(0x18,c['bar2']),(0x1c,0),(0x20,c['bar4']&0xffffffff),(0x24,c['bar4']>>32),(4,6)]:self.config(c['bus'],0,o,v)
    def wait(self,a,v,limit=30):
        end=time.monotonic()+limit
        while time.monotonic()<end:
            if self.rd(a)==v:return True
            if self.qemu.poll() is not None:raise RuntimeError('QEMU exited')
            time.sleep(.002)
        return False
    def command(self,name,args=None):
        self.mf.write((json.dumps(dict(execute=name,arguments=args or {}))+'\n').encode())
        while True:
            r=json.loads(self.mf.readline())
            if 'return' in r:return r['return']
            if 'error' in r:raise RuntimeError(r)
    def peer(self,i):return self.cards[i]['bar4']+0x20000000
    def inbound(self,i):
        addr=self.peer(i);a=self.cards[i]['bar0']+0x300100
        for o,v in [(4,0),(0,0),(8,addr&0xffffffff),(12,addr>>32),(16,(addr+0x1ffff)&0xffffffff),(20,0x20010000),(24,0),(4,0x80000000)]:self.wr(a+o,v)
    def outbound(self,i,target):
        a=self.cards[i]['bar0']+0x300000
        for o,v in [(4,0),(0,0),(8,0),(12,0x180),(16,0x1ffff),(20,target&0xffffffff),(24,target>>32),(4,0x80000000)]:self.wr(a+o,v)
    def write(self,a,data):
        data+=bytes(-len(data)%4);self.q(f'write {a:#x} {len(data):#x} 0x{data.hex()}')
    def read(self,a,n):
        self.qt.write(f'read {a:#x} {(n+3)&~3:#x}\n'.encode());r=self.qt.readline().decode().split()
        if not r or r[0]!='OK':raise RuntimeError(r)
        return bytes.fromhex(r[1].removeprefix('0x'))[:n]
    def submit(self,i,mode,src,dst,n,tensor=False,args=None):
        base=self.cards[i]['bar0']
        for ch in (0,0x100):self.wr(base+0x38008c+ch,0xffffffff)
        p=self.cards[i]['bar4']+0x10000100
        self.counts[i]+=1
        if args:
            for j in range(32):self.wr(p+0x80+j*4,struct.unpack_from('<I',args,j*4)[0])
        for o,v in [(4,mode),(8,n),(12,src&0xffffffff),(16,src>>32),(20,dst&0xffffffff),(24,dst>>32),(28,int(tensor))]:self.wr(p+o,v)
        self.wr(p,self.counts[i]);return self.counts[i]
    def finish(self,i,number):
        p=self.cards[i]['bar4']+0x10000000
        if not self.wait(p+44,number,60):raise TimeoutError(f'task {i}:{number}')
        return self.rd(p+32)
    def spawns(self,i):
        p=self.work/f'service{i}/service.jsonl'
        return [e['pid'] for e in map(json.loads,p.read_text().splitlines()) if e['event']=='spawn'] if p.exists() else []
    def backing(self,i,size):
        root=Path(f'/proc/{self.spawns(i)[-1]}/fd')
        path=next(p for p in root.iterdir() if 'zc-p1-' in os.readlink(p) and p.stat().st_size==size)
        fd=os.open(path,os.O_RDWR);m=mmap.mmap(fd,size);os.close(fd);return m
    def usage(self):
        pids=[self.qemu.pid] if self.qemu else []
        pids+= [p.pid for p in self.services.values() if p.poll() is None]
        pids+= [self.spawns(i)[-1] for i in range(self.count) if self.spawns(i)]
        rss={}
        for pid in pids:
            try:
                status=Path(f'/proc/{pid}/status').read_text().splitlines()
                rss[str(pid)]={line.split(':')[0]:line.split(':')[1].strip() for line in status if line.startswith(('VmRSS:','VmSize:','Threads:'))}
            except FileNotFoundError:pass
        return dict(elapsed_seconds=time.monotonic()-self.start_time,sampled_processes=rss,
                    note='RSS is a point sample; virtual sparse DDR capacity is not resident memory or PCIe performance')
    def close(self):
        metrics=self.usage()
        stop(self.qemu)
        for p in self.services.values():stop(p)
        for f in (self.qt,self.mf,self.qm):
            if f:f.close()
        for p in self.sockets:p.unlink(missing_ok=True)
        u=resource.getrusage(resource.RUSAGE_CHILDREN)
        metrics.update(children_user_seconds=u.ru_utime-self.usage_start.ru_utime,
                       children_system_seconds=u.ru_stime-self.usage_start.ru_stime,
                       max_child_rss_kib=u.ru_maxrss)
        (self.work/'resources.json').write_text(json.dumps(metrics,indent=2))
