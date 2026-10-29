#!/usr/bin/env python3
"""Nonblocking QEMU admission service using the P5 virtual-time fabric.

All endpoint DMA requests enter this one switch scheduler. Each fixed request
gets a grant after its modeled transaction/completions; QEMU then checks the
live route/IOMMU and commits data. Timing traces report admission, QEMU p2_host
traces report actual memory success. No QEMU MMIO thread waits on a socket.
"""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import selectors
import signal
import socket
import struct
from fabric import Fabric, LinkConfig
FRAME=struct.Struct('<QIIIIQIIQ')


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--socket',type=Path,required=True)
    ap.add_argument('--out',type=Path,required=True)
    ap.add_argument('--config',type=Path,required=True)
    args=ap.parse_args()
    cfg=json.loads(args.config.read_text())
    cfg['default']=LinkConfig(**cfg.get('default',{}))
    cfg['links']={k:LinkConfig(**v) for k,v in cfg.get('links',{}).items()}
    fabric=Fabric(**cfg)
    selector=selectors.DefaultSelector();clients={};messages=[];pending={};epochs={};running=True
    def shutdown(*_):
        nonlocal running
        running=False
    signal.signal(signal.SIGTERM,shutdown);signal.signal(signal.SIGINT,shutdown)
    server=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
    server.bind(str(args.socket));server.listen(16);server.setblocking(False)
    selector.register(server,selectors.EVENT_READ)
    def close(c):
        selector.unregister(c);clients.pop(c,None);c.close()
    try:
        while running:
            events=selector.select(.002)
            for key,mask in events:
                c=key.fileobj
                if c is server:
                    c,_=server.accept();c.setblocking(False);clients[c]=[bytearray(),bytearray()];selector.register(c,selectors.EVENT_READ)
                    continue
                if mask&selectors.EVENT_WRITE:
                    try:n=c.send(clients[c][1])
                    except BlockingIOError:n=0
                    except (BrokenPipeError,ConnectionResetError):close(c);continue
                    del clients[c][1][:n]
                    if not clients[c][1]:selector.modify(c,selectors.EVENT_READ)
                if mask&selectors.EVENT_READ:
                    try:data=c.recv(65536)
                    except BlockingIOError:continue
                    except ConnectionResetError:close(c);continue
                    if not data:close(c);continue
                    clients[c][0].extend(data)
                    while len(clients[c][0])>=FRAME.size:
                        frame=bytes(clients[c][0][:FRAME.size]);del clients[c][0][:FRAME.size]
                        token,slot,source,peer,write,address,length,epoch,session=FRAME.unpack(frame)
                        target=-1 if peer==0xffffffff else peer
                        identity=(session,epoch)
                        if source in epochs and epochs[source]!=identity:fabric.cancel(source,'epoch changed')
                        epochs[source]=identity
                        try:
                            r=fabric.submit(source,target,address,length,bool(write),queue=slot)
                            pending[r.id]=(c,token,slot)
                            messages.append(dict(request=r.id,token=token,card=source,slot=slot,session=session,epoch=epoch,address=address,length=length,write=bool(write),peer=target))
                        except (ValueError,BufferError):
                            clients[c][1].extend(FRAME.pack(token,slot,1,0,0,0,0,0,0));selector.modify(c,selectors.EVENT_READ|selectors.EVENT_WRITE)
            if pending:
                fabric.run(report=False)
                for rid,(c,token,slot) in list(pending.items()):
                    r=fabric.requests[rid]
                    if r.state=='pending':continue
                    if c in clients:
                        clients[c][1].extend(FRAME.pack(token,slot,0 if r.state=='complete' else 1,0,0,0,0,0,0))
                        selector.modify(c,selectors.EVENT_READ|selectors.EVENT_WRITE)
                    del pending[rid]
    finally:
        report=fabric.report();report['qemu_admission']=messages
        report['scope']='live DMA admission, virtual-time model; data result in QEMU trace'
        args.out.parent.mkdir(parents=True,exist_ok=True);args.out.write_text(json.dumps(report,indent=2))
        for c in list(clients):close(c)
        selector.close();server.close();args.socket.unlink(missing_ok=True)

if __name__=='__main__':main()
