#!/usr/bin/env python3
"""Read/write one aligned 32-bit PCI BAR register inside the QEMU guest."""
import argparse
import mmap
from pathlib import Path
import re
import struct


def number(value):
    return int(value, 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bdf', help='PCI BDF, e.g. 0000:03:00.0')
    parser.add_argument('bar', type=int, choices=(0, 2, 4))
    parser.add_argument('offset', type=number, help='byte offset within BAR')
    parser.add_argument('value', type=number, nargs='?', help='omit to read; supply to write')
    args = parser.parse_args()
    if not re.fullmatch(r'[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]', args.bdf):
        parser.error('invalid PCI BDF')
    if args.offset < 0 or args.offset % 4:
        parser.error('offset must be nonnegative and 32-bit aligned')
    if args.value is not None and not 0 <= args.value <= 0xffffffff:
        parser.error('value must fit uint32')
    device = Path('/sys/bus/pci/devices') / args.bdf
    try:
        start, end, flags = (int(v, 16) for v in (device / 'resource').read_text().splitlines()[args.bar].split())
        if not start or not flags & 0x200 or args.offset + 4 > end - start + 1:
            parser.error('unassigned/non-memory BAR or offset outside BAR')
        base = args.offset & ~(mmap.PAGESIZE - 1)
        mode = 'rb' if args.value is None else 'r+b'
        with (device / f'resource{args.bar}').open(mode, buffering=0) as file:
            prot = mmap.PROT_READ | (mmap.PROT_WRITE if args.value is not None else 0)
            with mmap.mmap(file.fileno(), mmap.PAGESIZE, prot=prot, offset=base) as regs:
                offset = args.offset - base
                if args.value is None:
                    value = struct.unpack_from('<I', regs, offset)[0]
                    print(f'BAR{args.bar}+0x{args.offset:x} = 0x{value:08x}')
                else:
                    struct.pack_into('<I', regs, offset, args.value)
                    print(f'WRITE BAR{args.bar}+0x{args.offset:x} <- 0x{args.value:08x}')
    except (OSError, ValueError, IndexError) as exc:
        parser.exit(1, f'PCI MMIO failed: {exc}\n')


if __name__ == '__main__':
    main()
