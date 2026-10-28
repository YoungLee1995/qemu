#!/usr/bin/env python3
"""P3 dual-card functional gate plus complete P2/P1 regression."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'p1'))
from run_p1 import ROOT,QEMU,SPIKE,SIM,digest
HERE=Path(__file__).resolve().parent


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--skip-build',action='store_true')
    args=ap.parse_args()
    work=HERE/'evidence'/f'all-{time.time_ns()}';work.mkdir(parents=True)
    result=dict(p3_full_exit=False,hardware_compatibility_exit=False,
                scope='P3-two-card-switch-local-DDR-functional-profile-v1',steps={})
    try:
        if not args.skip_build:
            for name,cmd in [('qemu',['ninja','-C',ROOT/'qemu/build','qemu-system-x86_64']),
                             ('spike',['make','-C',SIM/'build','-j8','spike'])]:
                with (work/f'build-{name}.log').open('w') as log:
                    subprocess.run(list(map(str,cmd)),check=True,stdout=log,stderr=subprocess.STDOUT,timeout=1800)
        files=list((ROOT/'qemu/hw/misc').glob('zc-pcie-p[123]*'))
        files+=list(HERE.glob('*.py'))+list((HERE.parent/'p2').glob('*.py'))
        files+=list((HERE.parent/'p2/runtime').glob('*'))
        files+=list((ROOT/'simulator/tools/p1').glob('*.py'))
        files+=[SPIKE,QEMU,ROOT/'simulator/riscv-tests/firmware.riscv',SIM/'riscv/p2_pcie.h',
                SIM/'smart/sm_npu.cc',ROOT/'simulator/riscv-tests/packet_runtime_test/tensor-args.bin',
                ROOT/'experiments/single_die_packet/run-20261027T100512-648188Z/moe/data/result_l1_spike.bin']
        files+=list((ROOT/'moe_bf16_sint4_33x29x34_fix/data').glob('*l1.bin'))
        before={str(p):digest(p) for p in files if p.is_file()}
        (work/'manifest.json').write_text(json.dumps(before,indent=2))
        for name,script,flags,limit in [('dual',HERE/'run_p3.py',[],600),
                                       ('p2',HERE.parent/'p2/run_all.py',['--skip-build'],3600)]:
            with (work/f'{name}.log').open('w') as log:
                code=subprocess.run([sys.executable,str(script)]+flags,cwd=ROOT,
                    stdout=log,stderr=subprocess.STDOUT,timeout=limit).returncode
            lines=(work/f'{name}.log').read_text().splitlines()
            path=next((Path(x.removeprefix('Evidence: ')) for x in reversed(lines) if x.startswith('Evidence: ')),None)
            result['steps'][name]=dict(exit_code=code,evidence=str(path) if path else None)
            if code or path is None:raise RuntimeError(name+' execution failed')
            data=json.loads((path/'results.json').read_text())
            if not data.get('passed' if name=='dual' else 'p2_full_exit'):raise RuntimeError(name+' gate failed')
            if name=='dual':
                required=['mixed_host_peer_hdma','real_host_vtd_fault','tensor_card1_mode4_all_bytes',
                          'peer_limit_guard_unchanged','trace_host_and_peer_domains']
                for engine in ('npu','hdma'):
                    for direction in ('read','write'):
                        required += [f'{engine}_{direction}_mutate_{mutation}_no_old_peer_write' for mutation in
                                     ('inbound','bar','mse','window','policy','bme','outbound')]
                        required += [f'{engine}_{direction}_disconnect_{side}_no_duplicate_completion' for side in ('source','target')]
                        required += [f'{engine}_{direction}_system_reset_no_old_peer_write']
                if not all(data['checks'].get(k) for k in required):raise RuntimeError('incomplete P3 matrix')
            print(name,'PASS',path,flush=True)
        after={p:digest(Path(p)) for p in before}
        result['manifest_unchanged']=before==after
        if before!=after:raise RuntimeError('source/binary changed during gate')
        result['p3_full_exit']=True
    except BaseException as e:
        result['error']=str(e)
        raise
    finally:
        (work/'results.json').write_text(json.dumps(result,indent=2))
        print('Evidence:',work,flush=True)

if __name__=='__main__':main()
