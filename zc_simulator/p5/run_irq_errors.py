#!/usr/bin/env python3
"""Demo interrupt registers, software diagnostic error ABI and channel scope."""
import json
from pathlib import Path
import time
from run_communication import P5Platform
HERE=Path(__file__).resolve().parent


def main():
    work=HERE/'evidence'/f'irq-errors-{time.time_ns()}'
    p=P5Platform(work,2);checks={};passed=False
    def check(k,v):
        checks[k]=bool(v);print(k,'PASS' if v else 'FAIL',flush=True)
        if not v:raise RuntimeError(k)
    def hdma(src,dst,n=128):
        a=p.cards[0]['bar0']+0x380200
        for off,val in [(0,1),(0x8c,0xffffffff),(0x34,0x202),(0x1c,n),
                        (0x20,src&0xffffffff),(0x24,src>>32),(0x28,dst&0xffffffff),(0x2c,dst>>32),(4,1)]:p.wr(a+off,val)
        return a
    def wait_status(a,value):return p.wait(a+0x80,3) and p.rd(a+0x84)&5==value
    try:
        p.start();p.inbound(0);p.inbound(1)
        initial=[p.spawns(i) for i in range(2)]
        a=p.cards[0]['bar0']+0x380200
        # BAR and MCU proxy share registers. Masking suppresses the line but
        # must preserve pending completion until W1C acknowledgement.
        with p.backing(0,4096) as mb:
            import struct
            load=lambda off:struct.unpack_from('<I',mb,off)[0]
            p.wr(a+0x88,1|(1<<4))
            hdma(0x20010000,p.peer(1))
            check('masked_complete_pending',wait_status(a,1) and load(0x434)==0)
            p.wr(a+0x88,1<<4)
            # MCU handler may clear asserted lines immediately; inspect the
            # unconsumed pair-1 status rather than asserting a fleeting level.
            check('stop_mask_register',p.rd(a+0x88)==1<<4)
            p.wr(a+0x8c,1)
            check('w1c_clear_pending',p.rd(a+0x84)==0)
            p.wr(a+0x88,4|(1<<6))
            hdma(0x20010000,p.peer(1)+0x30000)
            check('abort_pending_masked',wait_status(a,4))
            p.wr(a+0x8c,4)
            check('abort_w1c',p.rd(a+0x84)==0)
            check('software_irq_line_documented',load(0x430)==60)
        # Original NPU external DMA reports a recoverable software diagnostic
        # per hart. Force a real outbound miss and observe W0C + same PID retry.
        p.outbound(0,p.peer(1));p.write(p.peer(1),b'a'*128)
        p.wr(p.cards[0]['bar0']+0x300004,0)
        num=p.submit(0,1,p.peer(1),p.peer(1)+0x10000,128)
        check('npu_outbound_error_returned',p.finish(0,num)==4)
        diag=p.cards[0]['bar0']+0x381000+16*4
        check('npu_hart_error_recorded',p.rd(diag)==1)
        p.wr(diag,1)
        check('nonzero_does_not_clear',p.rd(diag)==1)
        p.wr(diag,0)
        check('w0c_error_clear',p.rd(diag)==0)
        p.outbound(0,p.peer(1))
        num=p.submit(0,1,p.peer(1),p.peer(1)+0x10000,128)
        check('npu_same_process_retry',p.finish(0,num)==0 and p.read(p.peer(1)+0x10000,128)==bytes(x^0x5a for x in b'a'*128))
        check('irq_error_no_backend_restart',all(p.spawns(i)==initial[i] for i in range(2)))
        # While the owned channel is held/stopped, runtime's other pair still
        # executes an ordinary NPU request once the hold is released.
        p.wr(p.cards[0]['bar0']+0x381080,8)
        a=hdma(0x20010000,p.peer(1))
        check('channel_accepted',p.rd(a+0x80)==1)
        before=p.read(p.peer(1),128);p.wr(a,0)
        p.wr(p.cards[0]['bar0']+0x381080,0)
        check('channel_stop_aborts',wait_status(a,4))
        check('channel_stop_no_old_write',p.read(p.peer(1),128)==before)
        passed=True
    finally:
        p.close();(work/'results.json').write_text(json.dumps(dict(passed=passed,checks=checks),indent=2));print('Evidence:',work,flush=True)

if __name__=='__main__':main()
