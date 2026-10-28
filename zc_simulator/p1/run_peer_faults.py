#!/usr/bin/env python3
"""QEMU-side protocol validation against malformed/stalled backend peers."""
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import threading
import time
from run_p1 import HERE,ROOT,QEMU,connect,stop
sys.path.insert(0,str(ROOT/'simulator/tools/p1'))
from protocol import *


def main():
    out=HERE/'evidence'/f'peer-faults-{time.time_ns()}';out.mkdir(parents=True)
    checks={};passed=False
    cases=['version','length','session','card','epoch','request','reserved','capability','type','ancillary','timeout','disconnect']
    try:
        for case in cases:
            path=Path('/tmp')/f'zc-peer-{os.getpid()}.sock';path.unlink(missing_ok=True)
            qtpath=Path('/tmp')/f'zc-peer-qt-{os.getpid()}.sock';qtpath.unlink(missing_ok=True)
            server=socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET);server.bind(str(path));server.listen(1);server.settimeout(5)
            error=[]
            def peer():
                try:
                    c,_=server.accept();c.settimeout(5)
                    frame,fds=receive(c)
                    for fd in fds:os.close(fd)
                    _,card,session,epoch,request,_,hello=frame
                    data=bytearray(encode(HELLO_ACK,card,session,epoch,request,hello))
                    offsets={'version':(4,'<H',2),'length':(8,'<I',1000),'session':(16,'<Q',session^1),
                        'card':(12,'<I',99),'epoch':(24,'<Q',epoch+1),'request':(32,'<Q',request+1),
                        'reserved':(44,'<I',1),'type':(6,'<H',MEM_READY),'capability':(48,'<Q',0)}
                    if case=='timeout':
                        time.sleep(.5)
                    elif case=='disconnect':pass
                    elif case=='ancillary':
                        fd=os.open('/dev/null',os.O_RDONLY)
                        c.sendmsg([data],[(socket.SOL_SOCKET,socket.SCM_RIGHTS,struct.pack('i',fd))]);os.close(fd)
                    else:
                        offset,fmt,value=offsets[case];struct.pack_into(fmt,data,offset,value);c.send(data)
                    c.close()
                except BaseException as e:error.append(str(e))
                finally:server.close()
            thread=threading.Thread(target=peer);thread.start();proc=None
            try:
                with (out/(case+'.log')).open('w') as log:
                    cmd=[str(QEMU),'-machine','q35','-accel','qtest','-display','none','-nodefaults',
                        '-qtest',f'unix:{qtpath},server=on,wait=off','-qtest-log','/dev/null',
                        '-device',f'zc-pcie-p1,backend-socket={path},config-sha256={"00"*32},session-timeout-ms=200,addr=2']
                    proc=subprocess.Popen(cmd,stdout=log,stderr=subprocess.STDOUT)
                    qt=connect(qtpath,proc)
                    def q(cmd):
                        qt.write(cmd.encode()+b'\n');r=qt.readline().decode().split()
                        if not r or r[0]!='OK':raise RuntimeError(r)
                        return int(r[1],16) if len(r)>1 else 0
                    for off,value in [(0x10,0xe0000000),(0x20,0),(0x24,1),(4,6)]:
                        q(f'outl 0xcf8 {0x80001000+off:#x}');q(f'outl 0xcfc {value:#x}')
                    deadline=time.monotonic()+5
                    while q('readl 0xe000000c')!=4 and time.monotonic()<deadline:time.sleep(.005)
                    checks[case]=q('readl 0xe000000c')==4 and q('readl 0xe0000008')==0
                    # Failed session never accepts a Host packet write.
                    q('writel 0x110000100 0xdeadbeef')
                    checks[case+'_blocks_write']=q('readl 0x110000100')==0
                    if not checks[case] or not checks[case+'_blocks_write']:raise RuntimeError(case)
                    print(case,'PASS',flush=True);qt.close()
            finally:
                stop(proc);thread.join(timeout=6);path.unlink(missing_ok=True);qtpath.unlink(missing_ok=True)
            if error:raise RuntimeError(error)
        passed=True
    finally:
        (out/'results.json').write_text(json.dumps({'pass':passed,'checks':checks},indent=2))
        print('Evidence:',out,flush=True)


if __name__=='__main__':main()
