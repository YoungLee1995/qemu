#!/usr/bin/env python3
"""Install this repository's native PCIe extension into an existing Spike source tree.

Developer build helper only. Runtime uses the published Spike ELF directly.
"""
import argparse
from pathlib import Path
import shutil


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path, help='riscv-isa-sim source root')
    args = parser.parse_args()
    source = args.source / 'spike_main/spike.cc'
    text = source.read_text()
    text = text.replace('#include "spike_pcie.h"', '#include "../smart/spike_pcie.h"')
    marker = 'int main(int argc, char **argv) {'
    hook = ('#include "../smart/spike_pcie.h"\n\n' + marker + '\n'
            '  int pcie_result = zc_pcie_entry(argc, argv);\n'
            '  if (pcie_result >= 0) return pcie_result;')
    if '#include "../smart/spike_pcie.h"' not in text:
        if text.count(marker) != 1:
            parser.error('unsupported spike.cc main signature; source left unchanged')
        text = text.replace(marker, hook)
    help_line = '  fprintf(stderr, "  -d                    Interactive debug mode\\n");'
    if '--pcie-socket PATH' not in text:
        if help_line not in text:
            parser.error('unsupported spike.cc help; source left unchanged')
        text = text.replace(help_line, help_line + '\n' +
            '  fprintf(stderr, "  --pcie-socket PATH    Listen for QEMU PCIe session (native mode)\\n");\n'
            '  fprintf(stderr, "  --pcie-config-sha256 HEX  Configuration identity shared with QEMU\\n");\n'
            '  fprintf(stderr, "  --pcie-card N         Card ID (default 0)\\n");\n'
            '  fprintf(stderr, "  --pcie-log FILE       Dedicated QEMU interaction log\\n");')
    shutil.copyfile(Path(__file__).with_name('spike_pcie.h'), args.source / 'smart/spike_pcie.h')
    legacy = source.with_name('spike_pcie.h')
    if legacy.is_file() and legacy.read_bytes() == (args.source / 'smart/spike_pcie.h').read_bytes():
        legacy.unlink()
    source.write_text(text)
    print(f'Native PCIe extension installed: {source}; rebuild Spike before use.')


if __name__ == '__main__':
    main()
