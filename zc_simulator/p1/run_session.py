#!/usr/bin/env python3
"""P1-B: persistent service, real QEMU SCM_RIGHTS and reset/task recovery."""
import argparse
import ctypes
import threading
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import time

from run_p1 import HERE, ROOT, SIM, SPIKE, FIRMWARE, ISA, QEMU, Backing, connect, stop, digest
sys.path.insert(0, str(ROOT/'simulator/tools/p1'))
from protocol import *


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--linux-evidence',type=Path,required=True,help='P1-A run with host module/initramfs')
    args=ap.parse_args()
    work=HERE/'evidence'/f'session-{time.time_ns()}';work.mkdir(parents=True)
    ctypes.CDLL(None).prctl(36,1,0,0,0)  # Subreap deliberate service-kill test child.
    results={'p1_full_exit':False,'checks':{}}
    service=None;qemu=None;b=None
    def check(name,value):
        if name in results['checks']:
            name+=f'_{len(results["checks"])}'
        results['checks'][name]=bool(value)
        if not value:raise RuntimeError(name)
        print(name,'PASS',flush=True)
    try:
        # Reuse only preparation of inputs; no backend or DDR FDs inherited by QEMU.
        b=Backing();b.prepare(work/'inputs')
        b.close();b=None
        inputs=work/'inputs'
        paths=[SPIKE,FIRMWARE,inputs/'firmware.cfg',inputs/'mcu.bin',inputs/'npu.bin',SIM/'smart/sm_system.cfg']
        paths+=list((inputs/'data').glob('*l1.bin'))
        config={'spike':str(SPIKE),'firmware':str(FIRMWARE),'isa':ISA,'cfg':str(inputs/'firmware.cfg'),
                'cwd':str(inputs),'sha256':{str(p):digest(p) for p in paths}}
        cfg=work/'backend.json';cfg.write_text(json.dumps(config,indent=2))
        sha=digest(cfg)
        # Short socket location: evidence paths exceed sockaddr_un limit.
        sock=Path('/tmp')/f'zc-p1b-{os.getpid()}.sock'
        def launch_service(label='service',ready_timeout=20):
            nonlocal service
            with (work/(label+'.log')).open('w') as log:
                service=subprocess.Popen([sys.executable,str(ROOT/'simulator/tools/p1/service.py'),
                    '--socket',str(sock),'--config',str(cfg),'--work',str(work/'service'),
                    '--ready-timeout',str(ready_timeout)],stdout=log,stderr=subprocess.STDOUT)
            deadline=time.monotonic()+5
            while not sock.exists() and service.poll() is None and time.monotonic()<deadline:time.sleep(.01)
        launch_service()
        deadline=time.monotonic()+5
        while not sock.exists() and service.poll() is None and time.monotonic()<deadline:time.sleep(.01)
        check('service_listening',sock.exists() and service.poll() is None)
        def client():
            c=socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET);c.settimeout(5);c.connect(str(sock));return c
        session=100
        def exchange(c,kind,req,payload=b'',fds=(),epoch=0):
            send(c,kind,0,session,epoch,req,payload,fds)
            frame,got=receive(c)
            for fd in got:os.close(fd)
            return frame
        hello=struct.pack('<QII',CAPS,MAX_PAYLOAD,0)+bytes.fromhex(sha)
        def rejected(name,raw=None,modify=None):
            nonlocal session
            session+=1;c=client()
            if raw is not None:c.send(raw)
            else:send(c,HELLO,0,session,0,1,modify or hello)
            try:
                frame,fds=receive(c)
                for fd in fds:os.close(fd)
                okay=frame[0]==ERROR
            except (EOFError,ConnectionResetError):okay=True
            c.close();time.sleep(.02);check(name,okay and service.poll() is None)
        # Framing/version/capability/identity failure, while listener remains usable.
        raw=bytearray(encode(HELLO,0,101,0,1,hello));struct.pack_into('<H',raw,4,2)
        rejected('reject_version',raw=raw)
        raw=bytearray(encode(HELLO,0,102,0,1,hello));struct.pack_into('<I',raw,8,1000)
        rejected('reject_length',raw=raw)
        rejected('reject_capabilities',modify=bytes(48))
        rejected('reject_config_digest',modify=hello[:16]+bytes(32))
        raw=encode(HELLO,99,103,0,1,hello);rejected('reject_card_identity',raw=raw)
        raw=bytearray(encode(HELLO,0,104,0,1,hello));struct.pack_into('<I',raw,44,1)
        rejected('reject_reserved',raw=raw)
        rejected('reject_truncated_frame',raw=b'bad')
        for name,second in [('reject_duplicate_request',(PING,1,0)),('reject_stale_epoch',(PING,2,1)),
                            ('reject_unknown_message',(99,2,0))]:
            session+=1;c=client();check(name+'_hello',exchange(c,HELLO,1,hello)[0]==HELLO_ACK)
            frame=exchange(c,second[0],second[1],epoch=second[2]);check(name,frame[0]==ERROR)
            c.close();time.sleep(.02)
        rejected('reject_oversized_frame',raw=encode(HELLO,0,999,0,1,hello)+bytes(MAX_PAYLOAD))
        session+=1;c=client();exchange(c,HELLO,1,hello)
        send(c,PING,0,session+1,0,2)
        mismatch,_=receive(c);check('reject_session_identity',mismatch[0]==ERROR)
        c.close();time.sleep(.02)
        # Wrong region size and missing/aliased FDs must not spawn a backend.
        desc=b''.join(struct.pack('<QQII',base,size,i,0) for i,(base,size) in enumerate(REGIONS))
        for name,mode in [('reject_missing_fds','missing'),('reject_fd_size','size'),('reject_unsealed_fds','unsealed')]:
            session+=1;c=client();exchange(c,HELLO,1,hello)
            fds=[]
            if mode!='missing':
                for i,(_,size) in enumerate(REGIONS):
                    fd=os.memfd_create('bad',os.MFD_ALLOW_SEALING);os.ftruncate(fd,4096 if mode=='size' else size);fds.append(fd)
            check(name,exchange(c,MEM_SETUP,2,desc,fds)[0]==ERROR)
            for fd in fds:os.close(fd)
            c.close();time.sleep(.02)
        def prepared_client():
            nonlocal session
            session+=1;c=client();exchange(c,HELLO,1,hello)
            backing=Backing()
            check('valid_mem_setup',exchange(c,MEM_SETUP,2,desc,backing.fds)[0]==MEM_READY)
            return c,backing
        # A descriptor received on an otherwise invalid frame must be closed.
        baseline_fds=len(list(Path(f'/proc/{service.pid}/fd').iterdir()))
        for _ in range(20):
            session+=1;c=client()
            fd=os.open('/dev/null',os.O_RDONLY)
            send(c,HELLO,0,session,0,1,hello,[fd]);os.close(fd)
            try:receive(c)
            except (EOFError,OSError):pass
            c.close();time.sleep(.005)
        time.sleep(.03)
        check('invalid_frame_received_fds_closed',len(list(Path(f'/proc/{service.pid}/fd').iterdir()))==baseline_fds)
        def device(extra=''):
            return f'zc-pcie-p1,backend-socket={sock},config-sha256={sha},session-timeout-ms=25000'+extra
        def start_qtest(label):
            nonlocal qemu
            qtpath=Path('/tmp')/f'zc-p1qt-{os.getpid()}.sock';qmpath=Path('/tmp')/f'zc-p1qm-{os.getpid()}.sock'
            qtpath.unlink(missing_ok=True);qmpath.unlink(missing_ok=True)
            cmd=[str(QEMU),'-machine','q35','-accel','qtest','-display','none','-nodefaults',
                 '-qtest',f'unix:{qtpath},server=on,wait=off','-qtest-log','/dev/null',
                 '-qmp',f'unix:{qmpath},server=on,wait=off','-device',device(',addr=2')]
            (work/f'{label}-command.json').write_text(json.dumps(cmd,indent=2))
            with (work/f'{label}-qemu.log').open('w') as log:qemu=subprocess.Popen(cmd,stdout=log,stderr=subprocess.STDOUT)
            qt=connect(qtpath,qemu);qm=connect(qmpath,qemu);qm.readline()
            def q(command):
                qt.write(command.encode()+b'\n');r=qt.readline().decode().split()
                if not r or r[0]!='OK':raise RuntimeError(r)
                return int(r[1],16) if len(r)>1 else 0
            def m(command):
                qm.write(json.dumps({'execute':command}).encode()+b'\n')
                while True:
                    r=json.loads(qm.readline())
                    if 'return' in r:return r['return']
                    if 'error' in r:raise RuntimeError(r)
            m('qmp_capabilities')
            return q,m,qt,qm
        def mapped(q):
            for off,value in [(0x10,0xe0000000),(0x14,0),(0x18,0xd0000000),(0x1c,0),(0x20,0),(0x24,8),(4,6)]:
                q(f'outl 0xcf8 {0x80001000+off:#x}');q(f'outl 0xcfc {value:#x}')
        def wait(q,addr,value,timeout=30):
            deadline=time.monotonic()+timeout
            while time.monotonic()<deadline:
                if q(f'readl {addr:#x}')==value:return True
                if qemu.poll() is not None:return False
                time.sleep(.005)
            return False
        args_data=(ROOT/'simulator/riscv-tests/packet_runtime_test/tensor-args.bin').read_bytes()
        reference=(args.linux_evidence/'rootfs/legacy-result.bin').read_bytes()
        def task(q,number,seq):
            q('writel 0xd01ff000 0x1234abcd')
            packet=struct.pack('<8I',0x5041434b,1,160,number,0,1,0x8204000b,0)+args_data
            for off in range(0,len(packet),4):q(f'writel {0x810000100+off:#x} {struct.unpack_from("<I",packet,off)[0]:#x}')
            q(f'writel 0x818019008 {number}');q('writel 0x818019010 1')
            check(f'task_{number}_response_{seq}',wait(q,0x81000002c,seq))
            check(f'task_{number}_status',q('readl 0x810000020')==0 and q('readl 0x81000001c')==number)
            data=bytearray()
            for off in range(0,len(reference),4):data+=struct.pack('<I',q(f'readl {0x810001000+off:#x}'))
            check(f'task_{number}_full_output',bytes(data)==reference)
            check(f'task_{number}_mcu_npu_ddr0',q('readl 0x810000030')==0x1234abcd and q('readl 0x810000034')==0x1234abcd)
        q,m,qt,qm=start_qtest('reset')
        mapped(q);check('qemu_socket_software_ready',wait(q,0xe0000008,1))
        def current_spawn():
            events=[json.loads(x) for x in (work/'service/service.jsonl').read_text().splitlines()]
            return [(e['session'],e['pid']) for e in events if e['event']=='spawn']
        before_tasks=current_spawn()
        task(q,1,1);task(q,2,2)
        check('ordinary_tasks_same_spike_pid',current_spawn()==before_tasks)
        check('ordinary_tasks_backend_alive',Path(f'/proc/{before_tasks[-1][1]}').exists())
        # MMIO invalid widths must leave the shared packet unchanged.
        q('writel 0x810000100 0x12345678')
        q('writeb 0x810000100 0xff');q('writew 0x810000100 0xffff')
        check('illegal_width_no_write',q('readl 0x810000100')==0x12345678)
        q('writel 0x810000101 0xdeadbeef')
        check('unaligned_no_write',q('readl 0x810000100')==0x12345678)
        # Concurrent remote atomic SEND with Host W1C/INTEN on separate bits.
        # Test-only /proc access observes the actual QEMU-created mailbox FD.
        fdpath=next(p for p in Path(f'/proc/{qemu.pid}/fd').iterdir() if 'zc-p1-mailbox' in os.readlink(p))
        fd=os.open(fdpath,os.O_RDWR)
        import mmap
        mb=mmap.mmap(fd,4096);os.close(fd)
        word=ctypes.c_uint32.from_buffer(mb,5*0x68)
        lib=ctypes.CDLL('libatomic.so.1')
        atomic_or=lib.__atomic_fetch_or_4;atomic_or.argtypes=[ctypes.c_void_p,ctypes.c_uint32,ctypes.c_int]
        running=threading.Event();running.set()
        atomic_or(ctypes.byref(word),128,3)
        def sender():
            while running.is_set():atomic_or(ctypes.byref(word),128,3)
        thread=threading.Thread(target=sender);thread.start()
        okay=True;bank=0x818019000+5*0x68
        try:
            for i in range(2000):
                q(f'writel {bank+4:#x} {128 if i&1 else 0}')
                q(f'writel {bank+16:#x} 1')
                q(f'writel {bank:#x} 1')
                if q(f'readl {bank:#x}')!=128:okay=False;break
        finally:
            running.clear();thread.join();del word;mb.close()
        check('concurrent_send_inten_w1c_2000',okay)
        q(f'writel {bank:#x} 128');check('concurrent_final_clear',q(f'readl {bank:#x}')==0)
        # Reset without Python reaping/restarting any backend: service handles all.
        for epoch in (1,2,3):
            m('system_reset');mapped(q)
            check(f'reset_{epoch}_software_ready',wait(q,0xe0000008,1))
            check(f'reset_{epoch}_epoch',q('readl 0xe0000014')==epoch)
            task(q,1,1)
        # Kill the backend via its logged PID; QEMU must negotiate a fresh session.
        events=[json.loads(x) for x in (work/'service/service.jsonl').read_text().splitlines()]
        pid=[e['pid'] for e in events if e['event']=='spawn'][-1]
        q('writel 0xe0000020 31')
        check('pre_disconnect_msix_pending',q('readl 0xe0003000') & (1<<31))
        os.kill(pid,signal.SIGKILL)
        old_session=[e['session'] for e in events if e['event']=='spawn'][-1]
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            current=[json.loads(x) for x in (work/'service/service.jsonl').read_text().splitlines()]
            if any(e['event']=='spawn' and e['session']!=old_session for e in current):break
            time.sleep(.01)
        check('abnormal_exit_new_session',any(e['event']=='spawn' and e['session']!=old_session for e in current))
        check('abnormal_exit_new_session_ready',wait(q,0xe0000008,1))
        check('reconnect_msix_pending_cleared',q('readl 0xe0003000')==0)
        task(q,1,1)
        # Kill the service itself: Spike parent-death guard and fresh backing.
        events=[json.loads(x) for x in (work/'service/service.jsonl').read_text().splitlines()]
        old_pid=[e['pid'] for e in events if e['event']=='spawn'][-1]
        old_session=[e['session'] for e in events if e['event']=='spawn'][-1]
        service.kill();service.wait(timeout=5)
        check('service_death_withdraws_ready',wait(q,0xe0000008,0,timeout=5))
        deadline=time.monotonic()+5
        while Path(f'/proc/{old_pid}/stat').exists() and time.monotonic()<deadline:
            state=Path(f'/proc/{old_pid}/stat').read_text().split()[2]
            if state=='Z':break
            time.sleep(.01)
        check('service_death_stops_spike',not Path(f'/proc/{old_pid}/stat').exists() or Path(f'/proc/{old_pid}/stat').read_text().split()[2]=='Z')
        os.waitpid(old_pid,0)
        sock.unlink(missing_ok=True);launch_service('service-restart')
        check('service_restart_ready',wait(q,0xe0000008,1))
        events=[json.loads(x) for x in (work/'service/service.jsonl').read_text().splitlines()]
        check('service_restart_new_session', [e['session'] for e in events if e['event']=='spawn'][-1]!=old_session)
        task(q,1,1)
        # Clean disconnect must reap before the next QEMU can own fresh memory.
        m('quit');check('qtest_exit',qemu.wait(timeout=10)==0);qt.close();qm.close();time.sleep(.1)
        # Verify startup failure and ready timeout using the same service code.
        c,b=prepared_client()
        pinned=inputs/'mcu.bin';original=pinned.read_bytes();pinned.write_bytes(original+b'bad')
        check('changed_image_refused',exchange(c,START,3)[0]==ERROR)
        pinned.write_bytes(original);c.close();b.close();b=None;time.sleep(.03)
        stop(service);launch_service('service-fast-timeout',.000001)
        c,b=prepared_client();check('ready_timeout_error',exchange(c,START,3)[0]==ERROR)
        c.close();b.close();b=None;time.sleep(.05)
        stop(service);launch_service('service-normal')
        # The service must report Popen/exec failure without crashing listener.
        stop(service)
        good_config=cfg.read_bytes();good_sha=sha
        bad_program=work/'invalid-executable';bad_program.write_text('invalid executable format')
        bad_program.chmod(0o755)
        broken=dict(config);broken['spike']=str(bad_program)
        broken['sha256']=dict(config['sha256'],**{str(bad_program):digest(bad_program)})
        cfg.write_text(json.dumps(broken,indent=2));sha=digest(cfg)
        hello=struct.pack('<QII',CAPS,MAX_PAYLOAD,0)+bytes.fromhex(sha)
        launch_service('service-exec-failure')
        c,b=prepared_client();check('exec_failure_error',exchange(c,START,3)[0]==ERROR)
        c.close();b.close();b=None;time.sleep(.05)
        check('exec_failure_listener_alive',service.poll() is None)
        stop(service);cfg.write_bytes(good_config);sha=good_sha
        hello=struct.pack('<QII',CAPS,MAX_PAYLOAD,0)+bytes.fromhex(sha)
        launch_service('service-restored')
        # Successful STOP command must reap and acknowledge zero outstanding writes.
        c,b=prepared_client();check('direct_software_ready',exchange(c,START,3)[0]==SOFTWARE_READY)
        response=exchange(c,STOP,4)
        check('stop_ack_after_reap',response[0]==STOPPED and response[-1]==struct.pack('<I',0))
        c.close();b.close();b=None;time.sleep(.05)
        # Linux uses exactly the same persistent service and socket profile.
        cmd=json.loads((args.linux_evidence/'linux-command.json').read_text())
        cmd[cmd.index('-initrd')+1]=str(args.linux_evidence/'initramfs.cpio.gz')
        cmd[-1]=device(',bus=dp')
        if '-icount' not in cmd:
            cmd+=['-icount','shift=3,align=off,sleep=off']
        (work/'linux-command.json').write_text(json.dumps(cmd,indent=2))
        for boot in range(5):
            with (work/f'linux-{boot}.log').open('w') as log:
                qemu=subprocess.Popen(cmd,stdout=log,stderr=subprocess.STDOUT)
                check(f'linux_{boot}_exit',qemu.wait(timeout=180)==0)
            text=(work/f'linux-{boot}.log').read_text()
            check(f'linux_{boot}_no_panic','Kernel panic' not in text)
            check(f'linux_{boot}_tensor_rejections_stop','P1_PASS tensor=3 rejected=5' in text and 'GUEST_RESULT=0' in text)
            check(f'linux_{boot}_host_demo','HOST_PROBE_PASS' in text and 'Discovered endpoints: 1' in text)
            time.sleep(.05)
        time.sleep(.1)
        events=[json.loads(x) for x in (work/'service/service.jsonl').read_text().splitlines()]
        spawns=[x['pid'] for x in events if x['event']=='spawn']
        reaped=[x['pid'] for x in events if x['event']=='reaped']
        check('all_owned_backends_reaped',set(spawns)-{old_pid}==set(reaped) and all(not Path(f'/proc/{pid}').exists() for pid in set(spawns)-{old_pid}))
        check('service_survives_sessions',service.poll() is None)
        results['session_connection_pass']=True
        sources=[ROOT/'qemu/hw/misc/zc-pcie-p1.c',ROOT/'qemu/hw/misc/zc-pcie-p1-session.h',Path(__file__)]
        sources+=list((ROOT/'simulator/tools/p1').glob('*.py'))+[QEMU,SPIKE,cfg]
        (work/'manifest.json').write_text(json.dumps({str(p):digest(p) for p in sources},indent=2))
    except BaseException as e:
        results['error']=f'{type(e).__name__}: {e}';raise
    finally:
        stop(qemu);stop(service)
        if b:b.close()
        (work/'results.json').write_text(json.dumps(results,indent=2))
        print('Evidence:',work,flush=True)


if __name__=='__main__':main()
