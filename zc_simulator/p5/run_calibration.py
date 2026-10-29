#!/usr/bin/env python3
"""Analytic timing calibration and deterministic shared-link scaling checks."""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import time
from backend import Fabric, LinkConfig
HERE=Path(__file__).resolve().parent


def main():
    work=HERE/'evidence'/f'calibration-{time.time_ns()}';work.mkdir(parents=True)
    checks={};scenarios={};passed=False
    def check(k,v):
        checks[k]=bool(v)
        if not v:raise RuntimeError(k)
    try:
        for gen in (3,4,5):
            for lanes in (4,8,16):
                c=LinkConfig(generation=gen,lanes=lanes,latency_ns=137,credit_return_ns=0,header_credits=1024,data_credits=65536)
                for n in (4,128,256):
                    for write in (True,False):
                        name=f'g{gen}_x{lanes}_n{n}_{"write" if write else "read"}'
                        f=Fabric(cards=2,mps=256,mrrs=512,default=c)
                        r=f.submit(0,1,0,n,write);report=f.run()
                        # Independent integer/rational oracle derived from
                        # GT/s, lane count and 128/130 encoding. Do not call
                        # the model serialization method to predict itself.
                        def oracle(payload):
                            numerator=(payload+24)*130*8
                            denominator=(8*2**(gen-3))*lanes*128
                            return (numerator+denominator-1)//denominator
                        expected=2*(oracle(n)+137)
                        if not write:expected+=2*(oracle(0)+137)
                        check(name+'_latency_exact',r.finished_ns==expected)
                        check(name+'_credit_conserved',report['credits_balanced'])
                        scenarios[name]=dict(expected_ns=expected,measured_virtual_ns=r.finished_ns,tolerance_ns=0)
        # Uplink competition has a known lower bound: total serialized wire
        # service. It cannot become eight independently provisioned uplinks.
        results=[]
        for cards in (2,8):
            for policy in ('round_robin','fixed_priority','weighted_round_robin'):
                def experiment():
                    c=LinkConfig(latency_ns=0,credit_return_ns=0,header_credits=1024,data_credits=65536,arbitration=policy)
                    f=Fabric(cards=cards,ordering='pci_relaxed',default=c)
                    for i in range(cards):f.submit(i,-1,i*0x10000,8192,True)
                    return f.run()
                report=experiment();again=experiment()
                check(f'{cards}_{policy}_repeat_identical',report==again)
                wire=report['links']['host:up']['wire_ns']
                end=max(r['finished_ns'] for r in report['requests'])
                check(f'{cards}_{policy}_wire_lower_bound',end>=wire)
                # One source packet needs one extra ingress serialization stage.
                check(f'{cards}_{policy}_pipeline_upper_bound',end<=wire+LinkConfig().serialization(256))
                check(f'{cards}_{policy}_credit_conserved',report['credits_balanced'])
                name=f'competition-{cards}-{policy}'
                (work/f'{name}.json').write_text(json.dumps(report,indent=2))
                results.append(dict(cards=cards,policy=policy,virtual_end_ns=end,uplink_wire_ns=wire))
        (work/'analytic.json').write_text(json.dumps(scenarios,indent=2))
        (work/'scaling.json').write_text(json.dumps(results,indent=2))
        passed=True
    finally:
        (work/'results.json').write_text(json.dumps(dict(passed=passed,checks=checks,reference='analytic Gen3/4/5 128b/130b + 24B model framing; not measured hardware',tolerance_ns=0),indent=2))
        print('checks',len(checks),'PASS' if passed else 'FAIL');print('Evidence:',work,flush=True)

if __name__=='__main__':main()
