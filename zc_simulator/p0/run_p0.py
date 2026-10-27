#!/usr/bin/env python3
"""Internal P0 baseline: fresh builds, fixed cases, hashes and explicit gates."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import struct
import subprocess
import sys
import time

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SIM = ROOT / 'simulator/riscv-isa-sim'
TESTS = ROOT / 'simulator/riscv-tests'
ISA = 'rv64imafdcv_zvl512b_zve32d_zfh_zvfh_zfbfmin_zvfbfmin_zba_zbb_zbs_zicond'


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')


def capture(args, cwd):
    return subprocess.check_output(args, cwd=cwd).decode(errors='replace').strip()


def snapshot(out, label):
    """HEAD plus working-tree patch and hashes of tracked/untracked source files."""
    manifest = {}
    for name in ('qemu', 'simulator'):
        repo = ROOT / name
        patch = subprocess.check_output(['git', 'diff', '--binary', 'HEAD'], cwd=repo)
        (out / f'{label}-{name}.patch').write_bytes(patch)
        names = subprocess.check_output(
            ['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], cwd=repo)
        files = {}
        for raw in names.split(b'\0'):
            if not raw:
                continue
            rel = os.fsdecode(raw)
            # QEMU upstream is identified by HEAD+patch; hash project additions.
            if name == 'qemu' and not rel.startswith('zc_simulator/'):
                continue
            path = repo / rel
            if path.is_file():
                files[rel] = sha(path)
            elif not path.exists():
                files[rel] = None
        manifest[name] = {'head': capture(['git', 'rev-parse', 'HEAD'], repo),
                          'status': capture(['git', 'status', '--short'], repo),
                          'patch_sha256': hashlib.sha256(patch).hexdigest(),
                          'files': files}
    write_json(out / f'{label}-sources.json', manifest)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--jobs', type=int, default=16)
    parser.add_argument('--tool-prefix', default='/data/nuclei/gcc/bin/riscv64-unknown-elf-')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error('--jobs must be positive')
    out = (args.output or HERE / 'evidence' / datetime.now(timezone.utc).strftime(
        'run-%Y%m%dT%H%M%S-%fZ')).resolve()
    out.mkdir(parents=True, exist_ok=False)
    results = {'schema_version': 1, 'started_utc': datetime.now(timezone.utc).isoformat(),
               'output': str(out), 'steps': {}, 'checks': {}, 'source_stable': False,
               'build_mode': 'fresh isolated Spike, firmware, dispatch, runtime and standalone',
               'profile': 'single-die-functional-experiment',
               'real_driver_accepted': False, 'golden_accepted': False,
               'p0_full_exit': False}
    write_json(out / 'results.json', results)

    def run(name, command, cwd, env=None, timeout=600):
        command = list(map(str, command))
        started = time.monotonic()
        item = {'command': command, 'cwd': str(cwd), 'exit_code': None}
        results['steps'][name] = item
        print(f'[{name}] started', flush=True)
        with (out / f'{name}.log').open('w') as log:
            proc = subprocess.Popen(command, cwd=cwd, env=env, stdout=log,
                                    stderr=subprocess.STDOUT, start_new_session=True)
            try:
                item['exit_code'] = proc.wait(timeout=timeout)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
                item['exit_code'] = 124
                raise
            finally:
                item['seconds'] = round(time.monotonic() - started, 3)
                write_json(out / 'results.json', results)
        print(f'[{name}] exit={item["exit_code"]}', flush=True)
        if item['exit_code']:
            raise RuntimeError(f'{name} failed; see {out / (name + ".log")}')

    def check(name, condition):
        results['checks'][name] = bool(condition)
        if not condition:
            raise RuntimeError(f'check failed: {name}')

    try:
        before = snapshot(out, 'before')
        tools = {name: capture([name, '--version'], ROOT).splitlines()[0]
                 for name in ('gcc', 'g++', 'make', 'dtc', args.tool_prefix + 'gcc',
                              args.tool_prefix + 'as', args.tool_prefix + 'ld',
                              args.tool_prefix + 'objcopy')}
        tools['python'] = sys.version
        tools['numpy'] = np.__version__
        tools['build_environment'] = {key: os.environ[key] for key in
                                      ('CC', 'CXX', 'CFLAGS', 'CXXFLAGS', 'LDFLAGS',
                                       'MAKEFLAGS', 'OMP_NUM_THREADS', 'SOURCE_DATE_EPOCH')
                                      if key in os.environ}
        tools['uname'] = capture(['uname', '-a'], ROOT)
        write_json(out / 'tools.json', tools)
        build = out / 'build'
        build.mkdir()
        run('configure-spike', ['sh', SIM / 'configure'], build)
        run('build-spike', ['make', f'-j{args.jobs}', 'spike'], build, timeout=3600)
        # sm_main locates configuration beside the executable in an isolated build.
        shutil.copy2(SIM / 'smart/sm_system.cfg', build / 'sm_system.cfg')
        spike = build / 'spike'
        firmware_dir = out / 'software'
        firmware_dir.mkdir()
        run('build-firmware', ['make', '-f', TESTS / 'Makefile', f'src_dir={TESTS}',
                              f'RISCV_PREFIX={args.tool_prefix}', f'RISCV_SIM={spike}',
                              'firmware.riscv', 'dispatch.riscv'], firmware_dir)
        firmware = firmware_dir / 'firmware.riscv'
        run('dispatch', [spike, f'--isa={ISA}', firmware_dir / 'dispatch.riscv'], firmware_dir)
        check('dispatch_marker', 'Test Dispatch finish!' in (out / 'dispatch.log').read_text())

        moe = out / 'moe'
        shutil.copytree(ROOT / 'moe_bf16_sint4_33x29x34_fix', moe)
        reference = moe / 'data/result_l1_spike.bin'
        reference.unlink(missing_ok=True)
        run('moe', [spike, f'--isa={ISA}', firmware, '-cfg', moe / 'simulator.cfg'], moe)
        check('moe_output_length', reference.stat().st_size == 33792)
        check('moe_regression_hash', sha(reference) ==
              '37121ee2cd6afaed02f18587ed683d6fb5d5c4a6cd3b078f682a77891fe7e015')
        # Keep numerical differences visible; this is a path regression, not golden acceptance.
        run('moe-golden-report', [sys.executable, moe / 'data/check_result.py'], moe)
        actual = np.fromfile(reference, dtype='<u2').reshape(66, 256)
        golden = np.fromfile(moe / 'data/result_l1_golden.bin', dtype='<u2').reshape(66, 256)
        rows = np.fromfile(moe / 'data/moe_index_l1.bin', dtype='<i2')
        valid = np.zeros((66, 256), dtype=bool)
        valid[rows, :34] = True
        a = (actual.astype(np.uint32) << 16).view(np.float32).astype(np.float64)
        b = (golden.astype(np.uint32) << 16).view(np.float32).astype(np.float64)
        results['golden_report'] = {
            'active_elements': int(valid.sum()),
            'active_bit_mismatches': int(np.count_nonzero(actual[valid] != golden[valid])),
            'active_nonfinite': int(np.count_nonzero(~np.isfinite(a[valid]) | ~np.isfinite(b[valid]))),
            'active_max_abs_error': float(np.max(np.abs(a[valid] - b[valid]))),
            'tolerance_approved': False}

        mailbox = out / 'mailbox'
        shutil.copytree(TESTS / 'runtime_test/case_mcu0_npu3_pingpong', mailbox)
        target = mailbox / 'runtime_mailbox_mcu0_npu3_pingpong_cfg_out.bin'
        target.unlink(missing_ok=True)
        run('generate-mailbox', ['perl', mailbox / 'gen_runtime_mailbox_npu3_pingpong.pl'], mailbox)
        run('mailbox', [spike, f'--isa={ISA}', '--runtime-func', firmware,
                        '-cfg', mailbox / 'firmware.cfg'], mailbox)
        words = struct.unpack('<II', (mailbox / 'runtime_mailbox_mcu0_npu3_pingpong_input.bin').read_bytes()[:8])
        check('mailbox_payload', target.read_bytes() == struct.pack('<IIII', words[1], words[0], words[1], words[0]))

        dual = out / 'dual-mcu'
        shutil.copytree(TESTS / 'runtime_test/case_mcu0_mcu1_npu0_irq', dual)
        (dual / 'dual_mcu_irq_out.bin').unlink(missing_ok=True)
        run('dual-input', ['perl', dual / 'gen_input.pl'], dual)
        for core in ('mcu0', 'mcu1', 'npu0'):
            run(f'assemble-{core}', [args.tool_prefix + 'as', '-march=rv64imafdc',
                                    '-o', f'{core}.o', f'{core}.S'], dual)
            run(f'link-{core}', [args.tool_prefix + 'ld', '-Ttext=0', '-o', f'{core}.elf', f'{core}.o'], dual)
            run(f'objcopy-{core}', [args.tool_prefix + 'objcopy', '-O', 'binary', f'{core}.elf', f'{core}.bin'], dual)
        run('dual-mcu', [spike, f'--isa={ISA}', '--runtime-func', firmware,
                         '-cfg', dual / 'firmware.cfg'], dual)
        check('dual_mcu_payload_count', (dual / 'dual_mcu_irq_out.bin').read_bytes() ==
              struct.pack('<5I', 0x07060504, 0x03020100, 0x0f0e0d0c, 0x0b0a0908, 2))

        mock_src = out / 'mock-source'
        shutil.copytree(TESTS / 'packet_runtime_test', mock_src,
                        ignore=shutil.ignore_patterns('*.o', '*.elf', 'mcu.bin', 'npu.bin', '__pycache__'))
        env = dict(os.environ, PCIE_WORKSPACE=str(ROOT), PCIE_BASELINE=str(reference),
                   PCIE_MOCK_OUTPUT=str(out / 'mock'), SPIKE_BIN=str(spike),
                   FIRMWARE_BIN=str(firmware), RISCV_PREFIX=args.tool_prefix)
        env.pop('PCIE_GDB_SCRIPT', None)
        run('mock', [sys.executable, mock_src / 'run_mock.py'], ROOT, env=env)
        history = json.loads((out / 'mock/results.json').read_text())
        check('mock_three_tasks_five_rejections_stop',
              len(history) == 10 and history[-1].get('pass') is True and
              sum(i.get('output_bytes', 0) > 0 for i in history) == 3 and
              sum(i.get('status', 0) > 0 for i in history) == 5 and
              history[-2].get('executed') == 3)

        tests_src = SIM / 'smart/mid_perf/tests'
        standalone = out / 'standalone'
        standalone.mkdir()
        for path in tests_src.glob('*.cc'):
            shutil.copy2(path, standalone / path.name)
        run('standalone', ['make', '-f', tests_src / 'Makefile.die', f'BUILD_DIR={build}',
                           f'SRC_DIR={SIM}', 'check'], standalone)
        artifacts = [spike, firmware, firmware_dir / 'dispatch.riscv',
                     build / 'sm_system.cfg', mock_src / 'mcu.bin', mock_src / 'npu.bin',
                     TESTS / 'packet_runtime_test/tensor-args.bin']
        artifacts += [p for p in moe.rglob('*') if p.is_file()]
        artifacts += [p for folder in (mailbox, dual, standalone, out / 'mock')
                      for p in folder.rglob('*') if p.is_file() and p.suffix != '.log']
        write_json(out / 'artifacts.json', {str(p): sha(p) for p in artifacts})
        after = snapshot(out, 'after')
        check('source_stable', before == after)
        results['source_stable'] = True
        results['local_baseline_pass'] = True
    except Exception as error:
        results['error'] = f'{type(error).__name__}: {error}'
        results['local_baseline_pass'] = False
        print(results['error'], file=sys.stderr)
    finally:
        results['finished_utc'] = datetime.now(timezone.utc).isoformat()
        write_json(out / 'results.json', results)
        print(f'Evidence: {out}', flush=True)
    return 0 if results.get('local_baseline_pass') else 1


if __name__ == '__main__':
    sys.exit(main())
