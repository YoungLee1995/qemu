#!/usr/bin/env python3
"""Start an internal 1/2/8-card qtest platform with persistent real backends."""
import argparse
import json
import signal
import threading
from cluster_platform import Platform,layout


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--cards',type=int,choices=(1,2,8),default=8)
    ap.add_argument('--work',required=True,help='New run directory; contains plan, sockets, logs and inputs')
    ap.add_argument('--duration',type=float,help='Optional bounded run in seconds after ready')
    ap.add_argument('--generate-only',action='store_true',help='Write platform.json without starting processes')
    args=ap.parse_args();p=Platform(args.work,args.cards);done=threading.Event()
    if args.generate_only:
        print(json.dumps(layout(args.cards),indent=2));return
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,lambda *_:done.set())
    try:
        p.start()
        for i in range(args.cards):p.inbound(i)
        # Leave the standard QEMU control sockets available to external clients.
        for f in (p.qt,p.mf,p.qm):f.close()
        p.qt=p.mf=p.qm=None
        print(json.dumps(dict(state='ready',cards=args.cards,work=str(p.work),
                              qtest=str(p.sockets[-2]),qmp=str(p.sockets[-1]),
                              backend_pids=[p.spawns(i)[-1] for i in range(args.cards)])),flush=True)
        import time
        end=None if args.duration is None else time.monotonic()+args.duration
        while not done.wait(.25):
            if p.qemu.poll() is not None:raise RuntimeError('QEMU exited')
            if any(s.poll() is not None for s in p.services.values()):raise RuntimeError('service exited')
            if end is not None and time.monotonic()>=end:break
    finally:p.close()

if __name__=='__main__':main()
