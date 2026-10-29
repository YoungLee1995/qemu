#!/usr/bin/env python3
"""Fail-closed fabric disconnect and recovery through opt-in PCIe FLR."""
import json
from pathlib import Path
import time
from run_communication import P5Platform
from cluster_platform import stop
HERE=Path(__file__).resolve().parent


def main():
    work=HERE/'evidence'/f'faults-{time.time_ns()}';p=P5Platform(work,2);checks={};passed=False
    def check(k,v):
        checks[k]=bool(v);print(k,'PASS' if v else 'FAIL',flush=True)
        if not v:raise RuntimeError(k)
    def program():
        a=p.cards[0]['bar0']+0x380200
        for o,v in [(0,1),(0x8c,0xffffffff),(0x34,0x202),(0x1c,128),
                    (0x20,0x20010000),(0x24,0),(0x28,p.peer(1)&0xffffffff),(0x2c,p.peer(1)>>32),(4,1)]:p.wr(a+o,v)
        return a
    try:
        p.start();p.inbound(0);p.inbound(1)
        before=p.read(p.peer(1),128);old=p.spawns(0)
        p.wr(p.cards[0]['bar0']+0x381080,8);a=program()
        check('accepted_before_disconnect',p.rd(a+0x80)==1)
        stop(p.fabric_process)
        (work/'live-fabric.json').rename(work/'fabric-before-disconnect.json')
        p.wr(p.cards[0]['bar0']+0x381080,0)
        check('no_fabric_bypass',p.wait(a+0x80,3,10) and p.rd(a+0x84)&5==4)
        check('no_disconnected_write',p.read(p.peer(1),128)==before)
        check('fabric_failure_keeps_spike',p.spawns(0)==old)
        p.fabric_socket.unlink(missing_ok=True);p.start_fabric()
        c=p.cards[0]
        p.config(c['bus'],0,0x88,p.cfgread(c['bus'],0,0x88)|(1<<15))
        for o,v in [(0x10,c['bar0']),(0x14,0),(0x18,c['bar2']),(0x1c,0),
                    (0x20,c['bar4']&0xffffffff),(0x24,c['bar4']>>32),(4,6)]:p.config(c['bus'],0,o,v)
        check('flr_after_service_restart_ready',p.wait(c['bar0']+8,1,60))
        p.inbound(0);p.write(p.peer(0),b'R'*128);a=program()
        check('new_fabric_transaction_passes',p.wait(a+0x80,3,10) and p.rd(a+0x84)&5==1)
        check('new_fabric_data',p.read(p.peer(1),128)==b'R'*128)
        passed=True
    finally:
        p.close();(work/'results.json').write_text(json.dumps(dict(passed=passed,checks=checks),indent=2));print('Evidence:',work,flush=True)

if __name__=='__main__':main()
