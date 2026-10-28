#!/usr/bin/env python3
"""Link real shared_mem_t to end/overflow/mapping regressions."""
import json
import subprocess
import time
from run_p1 import HERE, SIM, ROOT


def main():
    work=HERE/'evidence'/f'memory-{time.time_ns()}';work.mkdir(parents=True)
    passed=False
    try:
        build=SIM/'build'
        libraries=['spike_main','smart','riscv','disasm','sm_vae','sm_matrix','sm_sfu','sm_model_common','softfloat','fesvr','fdt']
        cmd=['g++','-std=c++2a','-O2','-fopenmp','-I'+str(build)]
        cmd+=['-I'+str(SIM/p) for p in ['riscv','fesvr','smart','softfloat']]
        cmd+=[str(ROOT/'simulator/tools/p1/memory_check.cc'),'-o',str(work/'memory-check'),'-Wl,--start-group']
        cmd+=[str(build/('lib'+p+'.a')) for p in libraries]
        cmd+=['-Wl,--end-group','-lpthread','-lboost_regex','-lboost_system']
        with (work/'build.log').open('w') as log:subprocess.run(cmd,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=120)
        with (work/'run.log').open('w') as log:subprocess.run([str(work/'memory-check')],stdout=log,stderr=subprocess.STDOUT,check=True,timeout=30)
        passed=(work/'run.log').read_text().count('SHARED_MEMORY_PASS')==2
        if not passed:raise RuntimeError('memory check')
    finally:
        (work/'results.json').write_text(json.dumps({'pass':passed},indent=2))
        print('Evidence:',work,flush=True)

if __name__=='__main__':main()
