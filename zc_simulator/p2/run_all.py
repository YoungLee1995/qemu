#!/usr/bin/env python3
"""P2 first implementation gate; full P2 remains open until listed gaps close."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'p1'))
from run_p1 import ROOT,SIM,QEMU,SPIKE,digest
HERE=Path(__file__).resolve().parent


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--skip-build',action='store_true',help='Use already rebuilt current QEMU/Spike/firmware')
    args=ap.parse_args()
    work=HERE/'evidence'/f'all-{time.time_ns()}';work.mkdir(parents=True)
    result={'p2_first_implementation_pass':False,'p2_full_exit':False,
        'scope':'single-card-functional-host-dma-subset-v1',
        'remaining':['MCU hardware local IRQ routing','per-engine recoverable NPU DMA error completion',
                     'full inbound/iATU register semantics','Host file IPC runtime consumer/CRC/result protocol',
                     'complete per-engine fault/reset matrix and real Tensor integration'],
        'steps':{}}
    try:
        if not args.skip_build:
            for label,cmd in [('qemu',['ninja','-C',ROOT/'qemu/build','qemu-system-x86_64']),
                              ('spike',['make','-C',SIM/'build','-j8','spike']),
                              ('firmware',['make','-C',ROOT/'simulator/riscv-tests','firmware.riscv'])]:
                with (work/('build-'+label+'.log')).open('w') as log:
                    subprocess.run(list(map(str,cmd)),check=True,stdout=log,stderr=subprocess.STDOUT,timeout=1800)
        for label,script,limit in [('hdma',HERE/'run_hdma.py',120),('spike',HERE/'run_spike.py',180),
                                   ('linux',HERE/'run_linux.py',900),('p1',HERE.parent/'p1/run_full.py',2400)]:
            cmd=[sys.executable,str(script)]+(['--skip-build'] if label=='p1' else [])
            with (work/(label+'.log')).open('w') as log:
                code=subprocess.run(cmd,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,timeout=limit).returncode
            lines=(work/(label+'.log')).read_text().splitlines()
            path=next((Path(x.removeprefix('Evidence: ')) for x in reversed(lines) if x.startswith('Evidence: ')),None)
            result['steps'][label]={'exit_code':code,'evidence':str(path) if path else None}
            if code or path is None:raise RuntimeError(label+' failed')
            data=json.loads((path/'results.json').read_text())
            if not data.get('p1_full_exit' if label=='p1' else 'pass'):raise RuntimeError(label+' failed result')
            print(label,'PASS',path,flush=True)
        files=list(HERE.glob('*.py'))+list(HERE.glob('*.c'))+list((HERE/'runtime').glob('*'))
        files+=list((ROOT/'qemu/hw/misc').glob('zc-pcie-p[12]*'))
        files+=list((ROOT/'simulator/tools/p1').glob('*.py'))
        files+=[SIM/'riscv/sim.h',SIM/'riscv/sim.cc',SIM/'riscv/p2_pcie.h',SPIKE,QEMU]
        (work/'manifest.json').write_text(json.dumps({str(p):digest(p) for p in files if p.is_file()},indent=2))
        result['p2_first_implementation_pass']=True
    except BaseException as e:
        result['error']=str(e)
        raise
    finally:
        (work/'results.json').write_text(json.dumps(result,indent=2))
        print('Evidence:',work,flush=True)

if __name__=='__main__':main()
