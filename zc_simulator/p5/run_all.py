#!/usr/bin/env python3
"""P5 gate: protocol, live data/queues/runtime, Linux and full P4 regressions."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from backend import ROOT
HERE=Path(__file__).resolve().parent
SIMTOOLS=ROOT/'simulator/tools/p5'


def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--skip-build',action='store_true');ap.add_argument('--p4-evidence',type=Path);args=ap.parse_args()
    work=HERE/'evidence'/f'all-{time.time_ns()}';work.mkdir(parents=True)
    result=dict(p5_full_exit=False,physical_hardware_calibrated=False,
        scope='P5-existing-demo-simulator-owned-communication-and-virtual-fabric-v1',steps={})
    try:
        if not args.skip_build:
            with (work/'build.log').open('w') as log:
                for cmd in [['ninja','-C',str(ROOT/'qemu/build'),'qemu-system-x86_64'],
                            ['make','-C',str(SIMTOOLS/'runtime')]]:
                    subprocess.run(cmd,check=True,stdout=log,stderr=subprocess.STDOUT,timeout=1800)
        # Build both fixed runtime sets before pinning their generated outputs.
        for directory in [SIMTOOLS/'runtime',HERE.parent/'p2/runtime']:
            subprocess.run(['make','-C',str(directory)],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        files=list(HERE.glob('*.py'))+list(HERE.glob('*.c'))
        files += [p for p in SIMTOOLS.rglob('*') if p.is_file() and p.suffix in ('.py','.c','.h','.json','.bin','.elf')]
        files += [SIMTOOLS/'runtime/Makefile']
        files += list((ROOT/'qemu/hw/misc').glob('zc-pcie-p[1235]*'))
        for directory in ('p1','p2','p3','p4'):
            files += list((HERE.parent/directory).glob('*.py'))+list((HERE.parent/directory).glob('*.c'))
        files += [p for p in (HERE.parent/'p2/runtime').iterdir() if p.is_file()]
        files += [ROOT/'qemu/build/qemu-system-x86_64',ROOT/'simulator/riscv-isa-sim/build/spike',
                  ROOT/'simulator/riscv-tests/firmware.riscv',ROOT/'simulator/host-demo/ssv_test_driver.c',
                  ROOT/'simulator/host-demo/ssv_test_app.c',ROOT/'experiments/qemu_stage1/assets/vmlinuz-lts']
        files += list((ROOT/'moe_bf16_sint4_33x29x34_fix/data').glob('*l1.bin'))
        files += [ROOT/'simulator/riscv-tests/packet_runtime_test/tensor-args.bin',
                  ROOT/'experiments/single_die_packet/run-20261027T100512-648188Z/moe/data/result_l1_spike.bin',
                  ROOT/'simulator/riscv-isa-sim/smart/sm_system.cfg']
        before={str(p):digest(p) for p in sorted(set(files))};(work/'manifest.json').write_text(json.dumps(before,indent=2))
        steps=[('protocol',HERE/'test_fabric.py',[],300),('calibration',HERE/'run_calibration.py',[],300),
               ('tasks',HERE/'run_tasks.py',[],600),('irq-errors',HERE/'run_irq_errors.py',[],300),
               ('faults',HERE/'run_faults.py',[],300),
               ('communication2',HERE/'run_communication.py',['--cards','2'],600),
               ('communication8',HERE/'run_communication.py',['--cards','8'],900),
               ('linux2',HERE/'run_linux.py',['--cards','2'],1200),
               ('linux8',HERE/'run_linux.py',['--cards','8'],1200),
               ('p4',HERE.parent/'p4/run_all.py',['--skip-build'],3600)]
        for name,script,flags,limit in steps:
            if name=='p4' and args.p4_evidence:
                path=args.p4_evidence.resolve()
                previous=json.loads((path/'results.json').read_text())
                manifest=json.loads((path/'manifest.json').read_text())
                if not previous.get('p4_full_exit') or not previous.get('manifest_unchanged'):
                    raise RuntimeError('previous P4 gate not closed')
                if not all(digest(Path(p))==h for p,h in manifest.items()):
                    raise RuntimeError('previous P4 baseline changed; rerun without --p4-evidence')
                result['steps'][name]=dict(exit_code=0,evidence=str(path),reused=True,
                    regression_manifest_verified=True,manifest_entries=len(manifest))
                print(name,'PASS verified unchanged regression',path,flush=True)
                continue
            with (work/f'{name}.log').open('w') as log:
                code=subprocess.run([sys.executable,str(script)]+flags,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,timeout=limit).returncode
            text=(work/f'{name}.log').read_text()
            path=next((Path(line.removeprefix('Evidence: ')) for line in reversed(text.splitlines()) if line.startswith('Evidence: ')),None)
            step=dict(exit_code=code,evidence=str(path) if path else None)
            result['steps'][name]=step
            if code:raise RuntimeError(name+' failed')
            if name=='protocol':
                match=re.search(r'Ran (\d+) tests',text)
                if not match or '\nOK\n' not in text:raise RuntimeError('protocol test summary missing')
                step['tests']=int(match[1])
            else:
                if path is None:raise RuntimeError(name+' evidence missing')
                report=json.loads((path/'results.json').read_text())
                if not report.get('p4_full_exit' if name=='p4' else 'passed'):raise RuntimeError(name+' exit false')
                step['checks']=len(report.get('checks',{}))
            print(name,'PASS',path or work/f'{name}.log',flush=True)
        result['manifest_unchanged']=before=={p:digest(Path(p)) for p in before}
        if not result['manifest_unchanged']:raise RuntimeError('source/binary changed during P5 gate')
        result['p5_full_exit']=True
    except BaseException as exc:
        result['error']=str(exc);raise
    finally:
        (work/'results.json').write_text(json.dumps(result,indent=2));print('Evidence:',work,flush=True)

if __name__=='__main__':main()
