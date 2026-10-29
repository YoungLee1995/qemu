#!/usr/bin/env python3
"""Generate/replay a self-contained task package using demo file IPC and NPU."""
import hashlib
import json
from pathlib import Path
import struct
import time
import zlib
from backend import ROOT
from tasks import TaskPackage
from run_communication import P5Platform
HERE=Path(__file__).resolve().parent
FILE_BUFFER=0x4000400000


def package(directory):
    directory.mkdir()
    data=bytearray(0x9200)
    source=ROOT/'moe_bf16_sint4_33x29x34_fix/data'
    for name,offset in [('act_l1.bin',0),('wgt_l1.bin',0x8400),('wgt_scale_l1.bin',0x8a00),('moe_index_l1.bin',0x9000)]:
        b=(source/name).read_bytes();data[offset:offset+len(b)]=b
    (directory/'input.bin').write_bytes(data)
    (directory/'parameters.bin').write_bytes((ROOT/'simulator/riscv-tests/packet_runtime_test/tensor-args.bin').read_bytes())
    (directory/'expected.bin').write_bytes((ROOT/'experiments/single_die_packet/run-20261027T100512-648188Z/moe/data/result_l1_spike.bin').read_bytes())
    program=ROOT/'qemu/zc_simulator/p2/runtime/execute.S'
    (directory/'program.S').write_bytes(program.read_bytes())
    spec=dict(schema=1,kind='existing-tensor',program='program.S',input='input.bin',parameters='parameters.bin',expected='expected.bin',
              note='Simulator-generated reproducible package from existing inputs; not a newly supplied production compiler ABI',
              sha256={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.iterdir()})
    (directory/'task.json').write_text(json.dumps(spec,indent=2))
    return spec


def main():
    work=HERE/'evidence'/f'tasks-{time.time_ns()}'
    p=P5Platform(work,8);checks={};passed=False
    def check(k,v):
        checks[k]=bool(v);print(k,'PASS' if v else 'FAIL',flush=True)
        if not v:raise RuntimeError(k)
    def ipc(card,addr,n,crc,trigger):
        base=p.cards[card]['bar4']+0x18103180
        for off,val in [(0,addr&0xffffffff),(4,addr>>32),(8,n),(12,0),(20,crc),(24,0)]:p.wr(base+off,val)
        p.wr(base+16,trigger)
        end=time.monotonic()+30
        while time.monotonic()<end:
            status=p.rd(base+16)
            if status!=trigger:return status
            time.sleep(.002)
        raise TimeoutError('file IPC')
    try:
        spec=package(work/'task')
        task=TaskPackage(work/'task')
        data,args,expected=task.data,task.parameters,task.expected
        check('package_hashes',all(hashlib.sha256((work/'task'/n).read_bytes()).hexdigest()==h for n,h in spec['sha256'].items()))
        p.start()
        for i in range(8):p.inbound(i)
        initial=[p.spawns(i) for i in range(8)]
        # Host RAM IOVA=GPA for this file sample; non-identity mappings are
        # independently exercised by communication/VT-d tests.
        for i in range(8):
            addr=0x12000000+i*0x200000;p.write(addr,data)
            check(f'card{i}_demo_file_load',ipc(i,addr,len(data),zlib.crc32(data),0x5a5aa5a5)==0)
        jobs=[]
        for i in range(8):jobs.append(p.submit(i,5,FILE_BUFFER,FILE_BUFFER,len(data),True,args))
        for i,number in enumerate(jobs):check(f'card{i}_real_npu_program',p.finish(i,number)==0)
        for i in range(8):
            addr=0x12000000+i*0x200000+0x10000
            p.write(addr,bytes(len(expected)+8))
            check(f'card{i}_demo_result_status',ipc(i,addr,0,0,0x72736c74)==0)
            header=p.read(addr,8)
            check(f'card{i}_done_header',struct.unpack('<II',header)==(0x646f6e65,len(expected)))
            check(f'card{i}_tensor_result',p.read(addr+8,len(expected))==expected)
        check('all_eight_same_pids',all(p.spawns(i)==initial[i] for i in range(8)))
        # Malformed size/CRC must fail without stopping runtime; a valid
        # subsequent file transfer remains usable.
        addr=0x12000000
        check('bad_crc_rejected',ipc(0,addr,len(data),0,0x5a5aa5a5)==0x80000003)
        check('empty_file_rejected',ipc(0,addr,0,0,0x5a5aa5a5)==0x80000001)
        check('valid_retry_after_bad_file',ipc(0,addr,len(data),zlib.crc32(data),0x5a5aa5a5)==0)
        check('invalid_local_task_address',p.finish(0,p.submit(0,5,0x21380000,FILE_BUFFER,len(data),True,args))==6)
        check('file_errors_no_restart',p.spawns(0)==initial[0])
        passed=True
    finally:
        p.close();(work/'results.json').write_text(json.dumps(dict(passed=passed,checks=checks),indent=2))
        print('Evidence:',work,flush=True)

if __name__=='__main__':main()
