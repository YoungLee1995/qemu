# QEMU repository development sources

These project sources were migrated from the former sibling simulator/tools
layout so the QEMU delivery does not need that repository.

- p1/: backend lifecycle service, wire protocol and memory diagnostic source.
- p5/: transaction/fabric model, Python communication API, C communication
  header, task package reader, profiles and MCU/NPU runtime source.
- p5/runtime/Makefile: needs no external workspace path;
  BASE resolves to this repository's zc_simulator/p2/runtime. Override
  RISCV_PREFIX when the bare-metal compiler is not on PATH.

Production run entry points validate and use simu/current. Editing these
sources alone does not mutate a running package; run
`python3 zc_simulator/p5/run_linux.py --runtime source --prepare-only`
from the QEMU repository root to rebuild/publish local runtime and guests.
Spike, firmware and the Linux kernel/driver baseline are retained from the
validated prebuilt bundle. See the main user guide for the supported scope.
