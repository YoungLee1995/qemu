#!/usr/bin/env python3
"""P1 exit gate: build, integration, protocol faults, memory and regressions."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
from run_p1 import HERE, ROOT, digest


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--skip-build',action='store_true',help='Require existing latest Spike/QEMU/firmware build')
    args=ap.parse_args()
    work=HERE/'evidence'/f'full-{time.time_ns()}';work.mkdir(parents=True)
    result={'p1_full_exit':False,'scope':'single-card-software-functional-profile-v1',
            'hardware_bar_compatibility':False,'host_dma_accepted':False,'steps':{}}
    try:
        def run(name,command,timeout=600):
            with (work/(name+'.log')).open('w') as log:
                code=subprocess.run(list(map(str,command)),cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,timeout=timeout).returncode
            lines=(work/(name+'.log')).read_text().splitlines()
            evidence=next((Path(x.removeprefix('Evidence: ')) for x in reversed(lines) if x.startswith('Evidence: ')),None)
            item={'exit_code':code,'command':list(map(str,command)),'evidence':str(evidence) if evidence else None}
            result['steps'][name]=item
            if code or evidence is None:raise RuntimeError(name+' failed')
            data=json.loads((evidence/'results.json').read_text())
            if name=='legacy':okay=data.get('p1_shared_connection_pass') and all(data['checks'].values())
            elif name=='session':okay=data.get('session_connection_pass') and all(data['checks'].values())
            elif name=='stage1':okay=data.get('experimental_stage1_pass')
            else:okay=data.get('pass')
            if not okay:raise RuntimeError(name+' result did not pass')
            print(name,'PASS',evidence,flush=True)
            return evidence
        cmd=[sys.executable,HERE/'run_p1.py','--host-demo']
        if args.skip_build:cmd+=['--skip-build']
        legacy=run('legacy',cmd,1800)
        run('session',[sys.executable,HERE/'run_session.py','--linux-evidence',legacy],600)
        run('peer-faults',[sys.executable,HERE/'run_peer_faults.py'])
        run('memory',[sys.executable,HERE/'run_memory.py'])
        run('faults',[sys.executable,HERE/'run_faults.py'])
        run('stage1',[sys.executable,HERE.parent/'stage1/run_all.py'])
        sources=list(HERE.glob('*.py'))+list(HERE.glob('*.c'))+list((HERE/'runtime').glob('*'))
        sources+=[ROOT/'qemu/hw/misc/zc-pcie-p1.c',ROOT/'qemu/hw/misc/zc-pcie-p1-session.h']
        sources+=list((ROOT/'simulator/tools/p1').glob('*.py'))+list((ROOT/'simulator/tools/p1').glob('*.cc'))
        sources+=[ROOT/'simulator/riscv-isa-sim/spike_main/spike.cc']
        manifest={str(p):digest(p) for p in sources if p.is_file()}
        (work/'manifest.json').write_text(json.dumps(manifest,indent=2))
        result['p1_full_exit']=True
    except BaseException as e:
        result['error']=str(e)
        raise
    finally:
        (work/'results.json').write_text(json.dumps(result,indent=2))
        print('Evidence:',work,flush=True)


if __name__=='__main__':main()
