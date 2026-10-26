#!/bin/bash
set -euo pipefail
STAGE1_DIR="$(cd "$(dirname "$0")" && pwd)"
PCIE_WORK="$(cd "$STAGE1_DIR/../../.." && pwd)"
PCIE_OUT="$PCIE_WORK/experiments/qemu_stage1"
python3 "$STAGE1_DIR/prepare_assets.py"
mkdir -p "$PCIE_WORK/qemu/build"
cd "$PCIE_WORK/qemu/build"
bash ../configure \
  --python="$PCIE_OUT/python-tool/python/bin/python3" \
  --target-list=x86_64-softmmu --disable-docs --disable-werror \
  > "$PCIE_OUT/configure.log" 2>&1
ninja -j32 qemu-system-x86_64 tests/qtest/intel-iommu-test \
  tests/qtest/iommu-intel-inv-test > "$PCIE_OUT/build.log" 2>&1
mkdir -p "$PCIE_WORK/simulator/riscv-isa-sim/build"
cd "$PCIE_WORK/simulator/riscv-isa-sim/build"
bash ../configure > "$PCIE_OUT/spike-configure.log" 2>&1
make -j64 > "$PCIE_OUT/spike-build.log" 2>&1
cd "$PCIE_WORK/simulator/riscv-tests"
make -j64 RISCV_SIM="$PCIE_WORK/simulator/riscv-isa-sim/build/spike" \
  > "$PCIE_OUT/firmware-build.log" 2>&1
echo "Build complete. Run the baseline, then stage1/run_linux.py."
