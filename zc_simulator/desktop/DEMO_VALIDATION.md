> 历史记录：文中的原开发机路径不作为当前操作入口。现行启动、构建和源码位置以 [统一使用教程](../完整构建与运行指南.md)、[统一使用教程](../完整构建与运行指南.md) 为准；当前使用不需要同级 simulator 仓库。

# 原版 host-demo / pcie-demo 接入实测

2026-11-02，本地实验，未提交。测试对象是同级 `simulator/host-demo` 和 `simulator/pcie-demo`，以及正在运行的八卡 Ubuntu 桌面。没有把原 P5 验收结果当成本次原版 demo 的验收结果。

## 实测结论

| 项目 | 本次结果 |
| --- | --- |
| 原版 `ssv_test_app.c` | 在 QEMU Ubuntu 内用 GCC 编译成功；有未使用 `print_buffer` 的告警 |
| host 可执行文件 | `/opt/t100/ssv_test_app.bin`，x86-64 Linux ELF；`.bin` 后缀不表示裸机镜像 |
| 八卡发现与打开 | `list-devices` 返回 0，ep0–ep7 均为 active；BAR2、BAR4、驱动 DMA 和结果大页映射成功 |
| 原版 Host 驱动 | 在 Linux 6.8.0-147 编译失败：`class_create(THIS_MODULE, CLASS_NAME)` 不符合新内核 API |
| 使用桌面已有驱动 | host 应用可打开八卡；本次未卸载或替换正在使用的驱动 |
| 原版 PCIe MCU 程序 | 编译失败，未生成新的 `mcu.bin` |
| `file-load` / `get-result` | ep0 写入 32 字节、源和 DMA 缓冲 CRC 均为 `0x7759b50e`；随后未收到 `Result #1`，外层 45 秒超时返回 124。端到端传输未通过 |

文件传输使用现有发布包 MCU/NPU runtime，不能据此推断原版 PCIe MCU demo 已运行。`file-load` 返回/打印 trigger 仅证明 Host 发出了请求，不能证明设备已经消费数据；本次没有定位超时的最终原因。测试脚本在 `get-result` 阶段被外层 timeout 结束，它自身会长期循环等待结果。

首次 host 运行因 `MAP_HUGETLB` 返回 `Cannot allocate memory`，所有端点被标为 inactive。临时设置 `vm.nr_hugepages=16` 后八卡打开通过；应用即便只执行 `list-devices` 也会为每张卡分配一个 2 MiB 大页。各次测试结束后恢复了原值 0。

## 可复现的桌面入口

在 QEMU 仓库根目录，保持桌面 VM 运行：

```sh
python3 zc_simulator/desktop/host_demo.py
python3 zc_simulator/desktop/host_demo.py --interactive
```

第一条同步同级 simulator 原版 host 源码，在来宾内编译、安装，然后执行八卡 `list-devices`；第二条进入原版 `ssv_test>` 命令行，可执行 `help`、`list-devices`、`set-device ep1`、`quit`。该入口只验证 Host 应用，不替换 MCU，也不自动执行可能长时间等待的固件加载命令。

已进入桌面终端或 `terminal.py` 的用户可直接执行：

```sh
sudo /opt/t100/run-host-demo
```

包装器串行化通过它启动的 host demo，临时预留至少 16 个大页，退出时恢复原设置。使用 `Ctrl+C` 可结束 `get-result` 并退出本次应用。不要同时直接启动其他会使用同一 DMA 缓冲的任务。

主机产物及日志位于 `desktop/state/share/pcie-demo-check/`：`ssv_test_app.bin`、`host-run.log`、`launcher.log`、`file-ipc.log`、`original-driver-build.log`。`state` 可由 `ZC_DESKTOP_STATE` 指定，以上均为受忽略的本地数据。来宾内产物另存于 `/opt/t100/`。

## MCU 编译阻塞及下一步条件

已实际使用 `/data/nuclei/gcc/bin/riscv64-unknown-elf-gcc` 执行：

```sh
cd ../simulator
mkdir -p build/desktop-demo-check
/data/nuclei/gcc/bin/riscv64-unknown-elf-gcc \
  -O2 -march=rv64gcv -mabi=lp64d -mcmodel=medany -ffreestanding -nostdlib \
  -DCONFIG_SOC_T100=1 -Ipcie-demo/include -Ipcie-demo/src/include \
  -Ipcie-demo/src -c pcie-demo/src/main.c \
  -o build/desktop-demo-check/main.o
```

报错 `common.h:11: fatal error: soc.h: No such file or directory`。日志为 `simulator/build/desktop-demo-check/original-build.log`。这只是编译探测，不是完整链接命令。

`pcie-demo` 当前是 BootROM 源码片段，没有 Makefile、启动汇编和链接脚本。还引用未提供的 `led_boot.h`、`version.h`、GPIO/UART/I2C 驱动、`clci_init.h`、`cache.h`、HSM 协议、NMSIS cache 等。原 `main()` 明确执行 AON 复位/时钟、efuse、UART、UCIe、HSM 和安全启动流程；不能靠补空头文件或返回成功的 stub 认定其在 simulator 上工作。

需要提供完整、可构建的 SDK，或按对话中的方案裁剪为 PCIe/IPC 专用入口，连同启动代码、链接脚本、编译选项和必要库一起交付。还需保留实际的 PCIe 初始化/协议实现并逐项核对模型所支持的寄存器。`CONFIG_SOC_QEMU` 是该 BootROM 的另一套 SRAM/UART 映射（SRAM `0xa0000000`），不能仅因 Host 使用 QEMU 就启用这个宏。

当前 desktop 的 `BundlePlatform.prepare()` 从 `simu/current/images/` 复制 MCU/NPU，每次运行生成 `firmware.cfg`，MCU hart 1 入口为 `0x40000000`，NPU hart 16 入口为 `0x4000000000`。裸 MCU 二进制必须按匹配的地址链接，并正确初始化栈、BSS 和需要的中断环境；Host ELF 不可放到这里。

现有 `simulator/tools/p5/runtime` 是平台专用的任务/文件 IPC runtime，可构建且已经随发布包运行，但它不是 `pcie-demo` 编译所得。其 MCU 处理文件请求的逻辑与 P5 任务调度都在同一个程序中；直接替换为 BootROM 将失去这些服务，仍需单独验证桌面任务兼容性。

后续拿到可构建 MCU 程序后，应在独立工作目录复制发布包并替换 MCU，更新摘要与加载配置，先验证单卡启动和 IPC，再测试 Host 文件往返、CRC 和多卡。当前没有生成或运行替代 MCU，也没有将失败的文件传输记为通过。
