"""Shared, repository-relative paths for the manual launch commands."""
import argparse
import json
from pathlib import Path
import shutil
import sys

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'zc_simulator/p5'))
from linux_bundle import validate_bundle
from run_p1 import ISA, digest


def parser(description):
    p = argparse.ArgumentParser(description=description)
    p.add_argument('--work', type=Path, required=True, help='New writable run directory')
    p.add_argument('--bundle', type=Path, default=REPO / 'zc_simulator/simu/current')
    p.add_argument('--cores', choices=('both', 'mcu', 'npu'), default='both')
    p.add_argument('--mcu-bin', type=Path, help='MCU raw BIN linked at 0x40000000')
    p.add_argument('--npu-bin', type=Path, help='NPU raw BIN linked at 0x4000000000')
    p.add_argument('--firmware', type=Path, help='Dispatcher ELF (not mcu.bin)')
    return p


def inputs(args, directory):
    bundle = args.bundle.resolve()
    validate_bundle(bundle)
    directory.mkdir(parents=True)
    lines = ['runtime_func:1']
    selected = [('mcu', 1, '0x40000000'), ('npu', 16, '0x4000000000')]
    selected = [c for c in selected if args.cores == 'both' or c[0] == args.cores]
    lines.append(f'pc_num:{len(selected)}')
    files = []
    for name, hart, address in selected:
        source = getattr(args, name + '_bin') or bundle / 'images' / (name + '.bin')
        target = directory / (name + '.bin')
        shutil.copy2(source.resolve(), target)
        files.append(target)
        lines.append(f'[{hart}, {address}, ./{name}.bin]')
    cfg = directory / 'firmware.cfg'
    cfg.write_text('\n'.join(lines + ['ddr_input_num:0', 'ddr_output_num:0',
                                    'l1buf_input_num:0', 'l1buf_output_num:0']) + '\n')
    firmware = (args.firmware or bundle / 'images/firmware.riscv').resolve()
    spike = bundle / 'bin/spike'
    return dict(spike=str(spike), firmware=str(firmware), isa=ISA,
                cfg=str(cfg), cwd=str(directory), p2=True, ready_cores=args.cores,
                sha256={str(p): digest(p) for p in files + [cfg, spike, firmware,
                                                          bundle / 'bin/sm_system.cfg']})


def save(path, data):
    path.write_text(json.dumps(data, indent=2) + '\n')
