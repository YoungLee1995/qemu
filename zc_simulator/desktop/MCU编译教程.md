# MCU `mcu.bin` 编译教程

## 先确认镜像类型

这里的 `mcu.bin` 是 simulator 的 RISC-V 裸机 MCU 镜像，运行在设备侧 hart 1，默认链接地址是 `0x40000000`。它不是 Ubuntu 的 x86-64 程序，也不是 Host demo 的 `.bin` 后缀 ELF。Host demo 应编译成 Linux ELF；MCU demo 才通过 `objcopy -O binary` 生成裸二进制。

当前 P5 参考 runtime 源码已随 QEMU 放在 `zc_simulator/tools/p5/runtime/`，其启动和链接文件来自同仓库 `zc_simulator/p2/runtime/`。无需克隆其他项目。

以下命令在 QEMU 仓库根目录、外层开发容器执行（不在 Ubuntu 来宾中）：

```sh
export RISCV_PREFIX=riscv64-unknown-elf-
"${RISCV_PREFIX}gcc" --version
make -B -C zc_simulator/tools/p5/runtime RISCV_PREFIX="$RISCV_PREFIX" mcu.bin
file zc_simulator/tools/p5/runtime/mcu.elf zc_simulator/tools/p5/runtime/mcu.bin
"${RISCV_PREFIX}readelf" -h zc_simulator/tools/p5/runtime/mcu.elf
```

交叉编译器由工具链组提供或从其上游安装，需支持 `rv64gcv`；可把前缀改为实际编译器路径。构建 MCU 不会替换运行包。若修改了集成 runtime 或来宾测试，使用 `python3 zc_simulator/p5/run_linux.py --runtime source --cards 2` 从本仓库源码重建并发布；此命令复用包内 Spike、firmware、内核和适配驱动，不编译模型本身。

## 推荐的工具链交付目录

工具链同事应提交或提供一个自包含目录，至少包含：

```text
my-mcu/
  Makefile
  start.S              # _start、栈、必要的中断入口
  link.ld              # 输出地址由 LOAD_BASE 传入
  main.c               # PCIe/IPC 专用入口
  include/             # simulator 可用的寄存器和 ABI 头文件
  lib/                 # 与编译器和 simulator 匹配的库（如需要）
```

下面给出一套可以直接构建的最小工程。它只用于验证编译、链接和启动约定，**不实现 PCIe/IPC 或 P5 ready 协议**，不能代替现有 runtime 跑桌面测试。先创建可写目录：

```sh
mkdir -p zc_simulator/desktop/state/mcu-dev
cd zc_simulator/desktop/state/mcu-dev
```

用编辑器分别创建下面四个文件。Makefile 命令行开头使用 Tab；复制后若遇到 `missing separator`，检查缩进。

`Makefile`：

```make
PREFIX ?= riscv64-unknown-elf-
CC := $(PREFIX)gcc
OBJCOPY := $(PREFIX)objcopy
CFLAGS := -O2 -g -march=rv64gcv -mabi=lp64d -mcmodel=medany \
          -msmall-data-limit=0 -ffreestanding -fno-builtin \
          -fno-stack-protector -nostdlib -nostartfiles

mcu.bin: mcu.elf
	$(OBJCOPY) -O binary $< $@

mcu.elf: start.S main.c link.ld
	$(CC) $(CFLAGS) -DMCU \
	  -Wl,-T,link.ld -Wl,--defsym=LOAD_BASE=0x40000000 \
	  -Wl,--build-id=none -o $@ start.S main.c
```

`link.ld`：

```ld
OUTPUT_ARCH(riscv)
ENTRY(_start)
SECTIONS {
  . = LOAD_BASE;
  .text : { KEEP(*(.text.start)) *(.text*) }
  .rodata : { *(.rodata*) *(.srodata*) }
  .data : { *(.data*) *(.sdata*) }
  . = ALIGN(16);
  .bss (NOLOAD) : {
    __bss_start = .;
    *(.bss*) *(.sbss*) *(COMMON)
    . = ALIGN(16);
    __bss_end = .;
  }
  .stack (NOLOAD) : {
    . = ALIGN(16);
    . += 16384;
    __stack_top = .;
  }
}
```

`start.S`：

```asm
.section .text.start,"ax"
.global _start
_start:
  csrw mie, zero
  csrci mstatus, 8
  li t0, 0x6600
  csrs mstatus, t0
  la sp, __stack_top
  la t0, __bss_start
  la t1, __bss_end
1:
  bgeu t0, t1, 2f
  sd zero, 0(t0)
  addi t0, t0, 8
  j 1b
2:
  call main
3:
  j 3b
```

`main.c`：

```c
#include <stdint.h>
volatile uint32_t mcu_started;

int main(void)
{
    mcu_started = 0x4d435531;
    for (;;) {
        __asm__ volatile ("nop");
    }
}
```

此例关闭中断，设置 16 KiB 栈，清零 BSS 后进入 C 代码。初始化数据与代码一起装载，无须从另一 ROM 地址搬运；没有配置 trap handler、定时器或 ECLIC。外部库若依赖 gp、TLS、构造函数、堆或其他启动流程，还需对应实现。仓库 P5 `start.S` 是专用启动代码，不含通用 BSS 清零逻辑，不能不检查就当作任意业务程序的 CRT 使用。

编译并检查：

```sh
export RISCV_PREFIX=riscv64-unknown-elf-
make PREFIX="$RISCV_PREFIX"
file mcu.elf mcu.bin
"${RISCV_PREFIX}readelf" -h mcu.elf
"${RISCV_PREFIX}readelf" -S mcu.elf
"${RISCV_PREFIX}objdump" -d mcu.elf | sed -n '1,80p'
"${RISCV_PREFIX}nm" -n mcu.elf
sha256sum mcu.elf mcu.bin
```

确认 `_start`、ELF 入口和 `.text` 起点均为 `0x40000000`，`mcu.bin` 非空，ELF 为 RISC-V 64 位。参数沿用当前参考 runtime 的 `rv64gcv/lp64d`；其他 ISA/ABI 组合需要另行验证。`.bss` 和栈不会作为数据写入裸 BIN，因此必须由启动代码和装载内存提供可用空间。不要用 x86 `gcc` 生成 MCU ELF，也不要把 `mcu.elf` 直接改名为 `mcu.bin`。

将 `main.c` 换成工具链的 PCIe/IPC 入口后，把所有新增 `.c`/`.S` 文件列入 ELF 依赖和链接命令，用 `-I` 指定真实头文件目录。当前使用 `-nostdlib`，`printf`、`memcpy`、`malloc` 等不会自动获得实现；应提供所需的裸机实现或匹配库。C++ 代码还需处理运行时、异常及构造函数约定。遇到 `undefined reference` 应补齐真实实现，不能忽略链接错误。

## 编译产物如何交给 simulator

底层 simulator 已支持通过 cfg 选择 MCU-only、NPU-only 或同时启动；具体命令和完成协议见 [指定设备与 MCU/NPU 运行说明](指定设备与MCU-NPU运行说明.md)。这是独立 simulator 的能力，桌面端的按卡自定义镜像入口状态如下。

当前 desktop 固定从 `simu/current` 发布包复制镜像，暂未提供 `--mcu-bin` 或自定义发布包选择参数。上面的构建命令**不会自动切换桌面固件**。不要直接覆盖 `simu/current/images/mcu.bin`：发布包的 `manifest.json` 校验镜像 SHA-256，直接替换会导致启动校验失败。

设备侧装载配置约定如下，供对接 simulator 装载流程使用；这段配置本身不是启动命令：

```text
runtime_func:1
pc_num:2
[1, 0x40000000, ./mcu.bin]
[16, 0x4000000000, ./npu.bin]
ddr_input_num:0
ddr_output_num:0
l1buf_input_num:0
l1buf_output_num:0
```

当前每次桌面运行的 `inputs/firmware.cfg` 由 `BundlePlatform.prepare()` 自动生成，随后后端配置对输入镜像生成摘要；相对路径依据 backend 的 inputs 工作目录解析。只改 cfg、只拷贝 BIN 到共享目录、或在 Ubuntu 执行 `./mcu.bin` 都不会使 MCU 开始运行。也不能用 Host 的 `fw-load` 代替该启动装载流程。

交付后应在独立测试发布包内替换镜像并重新生成 manifest，再接入明确选择该包的实验启动入口。当前这部分需要额外接入，本文没有声称自定义 MCU 已启动。届时先测单卡，再扩到 2/8 卡；检查后端日志、设备侧 ready 和实际协议行为。`control.py status` 中的 SSH/桌面就绪只证明 Ubuntu 可用，不能作为任意 MCU 程序执行成功的依据。

## 重要的 ABI 和模型边界

裸 MCU 程序必须自己完成启动、栈、BSS、必要的中断和 IPC 轮询。本仓库 `zc_simulator/tools/p5/runtime/runtime.c` 的 MCU 入口处理文件 DMA、CRC、结果回传以及 P5 任务协议；把它替换成只初始化 PCIe 的程序会使这些现有任务失效，除非新程序实现同一 ABI。

旧芯片 BootROM demo（未纳入本仓库运行入口）是 BootROM 风格入口，会调用 efuse、AON/MCU 时钟复位、UART、UCIe、HSM、安全启动和大量未随目录提供的 SDK 头文件/库。当前实际编译会报 `soc.h: No such file or directory`。不能用空 stub 解决后就认为代码可在 simulator 运行。工具链同事需要提供完整匹配 SDK，或交付裁剪后的 PCIe/IPC 专用版本。

模型只实现文档和源码中声明的 PCIe、BAR、DMA、IPC 地址及 P5 运行时行为；真实芯片上的 efuse、安全 HSM、UART、上电复位和所有未建模寄存器不能假定可用。代码应明确关闭或隔离这些路径，并提供 simulator 专用配置。

## 建议的交付验收

工具链同事交付时请同时给出：编译器版本、完整 Makefile、所有源文件/头文件/库、`mcu.elf`、`mcu.bin`、链接地址和入口地址、`readelf` 输出，以及单卡启动日志。验证顺序为：编译可复现 → ELF 地址检查 → 单卡 ready → Host IPC/PCIe → 2 卡互访 → 8 卡回归。任何一步失败都应保留日志和原始镜像，不能只报告 `mcu.bin` 已生成。

## 本次教程验证

2026-11-02 在外层容器使用 Nuclei GCC 14.2.1，直接提取本文四段代码到隔离目录构建成功，生成 RISC-V ELF64 和 82 字节裸 BIN；`readelf` 确认入口为 `0x40000000`，`nm` 确认 BSS 和 16 KiB 栈符号地址。链接器报告简化链接脚本的 LOAD 段具有 RWX 权限告警。本次只验证构建和地址，不代表该示例已在 simulator 执行。

另在隔离目录中使用现有 P5 Makefile、`runtime.c` 和启动/链接文件，成功构建 P5 的 `mcu.bin`；没有覆盖正在运行的固件或发布包。证据位于 `desktop/state/mcu-tutorial-validation/build.log` 和该目录的 `p5-reference/build.log`。原版 `pcie-demo` 缺失 SDK 的阻塞仍然存在，不能用参考 runtime 构建通过推断其编译通过。
