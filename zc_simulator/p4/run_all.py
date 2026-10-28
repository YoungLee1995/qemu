#!/usr/bin/env python3
"""P4 eight-card gate: real-model, guest-driver and complete P3/P2/P1 layers."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
from cluster_platform import HERE,ROOT,SIM,SPIKE,QEMU,digest


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--skip-build',action='store_true');args=ap.parse_args()
    work=HERE/'evidence'/f'all-{time.time_ns()}';work.mkdir(parents=True)
    result=dict(p4_full_exit=False,hardware_compatibility_exit=False,
                scope='P4-eight-card-functional-profile-v1',steps={})
    try:
        if not args.skip_build:
            for name,cmd in [('qemu',['ninja','-C',ROOT/'qemu/build','qemu-system-x86_64']),
                             ('spike',['make','-C',SIM/'build','-j8','spike'])]:
                with (work/f'build-{name}.log').open('w') as log:subprocess.run(list(map(str,cmd)),check=True,stdout=log,stderr=subprocess.STDOUT,timeout=1800)
        files=list(HERE.glob('*.py'))+list(HERE.glob('*.c'))
        files+=list((ROOT/'qemu/hw/misc').glob('zc-pcie-p[123]*'))
        files+=list((HERE.parent/'p2/runtime').glob('*'))+list((ROOT/'simulator/tools/p1').glob('*.py'))
        files+=list((ROOT/'moe_bf16_sint4_33x29x34_fix/data').glob('*l1.bin'))
        files+=[SPIKE,QEMU,ROOT/'simulator/riscv-tests/firmware.riscv',
                SIM/'smart/sm_regs.h',SIM/'riscv/p2_pcie.h',SIM/'smart/sm_npu.cc',
                ROOT/'simulator/riscv-tests/packet_runtime_test/tensor-args.bin',
                ROOT/'experiments/single_die_packet/run-20261027T100512-648188Z/moe/data/result_l1_spike.bin',
                ROOT/'simulator/host-demo/ssv_test_driver.c',ROOT/'simulator/host-demo/ssv_test_app.c',
                HERE.parent/'p1/host-demo.patch',HERE.parent/'p1/build_host_demo.py',
                ROOT/'experiments/qemu_stage1/assets/vmlinuz-lts']
        before={str(p):digest(p) for p in files if p.is_file()};(work/'manifest.json').write_text(json.dumps(before,indent=2))
        for name,script,flags,limit in [('cluster',HERE/'run_cluster.py',[],900),('linux',HERE/'run_linux.py',[],1200),
                                       ('p3',HERE.parent/'p3/run_all.py',['--skip-build'],3600)]:
            with (work/f'{name}.log').open('w') as log:code=subprocess.run([sys.executable,str(script)]+flags,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,timeout=limit).returncode
            lines=(work/f'{name}.log').read_text().splitlines()
            path=next((Path(x.removeprefix('Evidence: ')) for x in reversed(lines) if x.startswith('Evidence: ')),None)
            result['steps'][name]=dict(exit_code=code,evidence=str(path) if path else None)
            if code or path is None:raise RuntimeError(name+' failed')
            data=json.loads((path/'results.json').read_text())
            if not data.get('p3_full_exit' if name=='p3' else 'passed'):raise RuntimeError(name+' exit false')
            if name=='cluster':
                required=['56_directed_pairs_all_four_modes','same_input_three_repeats','all_normal_tasks_same_pids',
                          'trace_model_proxy_all56','trace_hdma_read_all56','trace_hdma_write_all56',
                          'npu_timeout_one_replacement','npu_timeout_no_peer_write']
                required+=[f'isolate_{engine}_{direction}_{fault}_unrelated_pids_epochs' for engine in ('npu','hdma')
                           for direction in ('read','write') for fault in ('target_disconnect','source_disconnect','runtime_crash','target_bar','target_inbound','target_window')]
                if not all(data['checks'].get(k) for k in required):raise RuntimeError('P4 fault/coverage matrix incomplete')
                coverage=json.loads((path/'coverage.json').read_text())
                for mode in (1,2,3,4):
                    if len({(r['source'],r['target']) for r in coverage if r['mode']==mode})!=56:raise RuntimeError('missing directed pair')
            print(name,'PASS',path,flush=True)
        after={name:digest(Path(name)) for name in before};result['manifest_unchanged']=before==after
        if before!=after:raise RuntimeError('source/binary changed during gate')
        result['p4_full_exit']=True
    except BaseException as e:
        result['error']=str(e);raise
    finally:
        (work/'results.json').write_text(json.dumps(result,indent=2));print('Evidence:',work,flush=True)

if __name__=='__main__':main()
