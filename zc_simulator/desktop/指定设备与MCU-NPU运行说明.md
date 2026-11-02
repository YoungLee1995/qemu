# 指定设备、脚本与 MCU/NPU 的运行方式

## 当前支持到哪一层

| 需求 | 当前能力 | 命令执行位置 |
| --- | --- | --- |
| 指定 PCIe 卡执行现有任务 | 支持，`my-task` 的第一个参数为卡号 | Ubuntu 来宾 |
| 指定 Host 脚本，脚本向指定卡提交任务 | 支持，脚本调用现有 Host 工具即可 | Ubuntu 来宾，或从外层经 SSH 调用 |
| 每张桌面卡同时启动 MCU+NPU | 支持；固定使用发布包镜像，MCU hart 1、NPU hart 16 | desktop 启动流程 |
| 只启动 MCU、只启动 NPU、同时启动两者 | 底层 simulator 支持用 `firmware.cfg` 选择，三种最小程序已实际验证 | 外层容器的独立 simulator 进程 |
| 在运行中的八卡桌面选择卡 N、热加载任意 `mcu.bin` / NPU 程序 | 尚无通用入口 | 需要补设备端装载、调度和 Host 命令 |
| `desktop.py --card N --mcu-bin ... --npu-bin ...` | 当前没有这些参数；`--cards` 仅控制卡数量 | 不可按此假设调用 |

因此，同事要求的“命令 + 卡号 + 任意 MCU/NPU 程序路径 + 执行模式”**尚未作为桌面端完整功能交付**。下面分别给出现有功能的操作方法。

## 1. 在 Ubuntu 中指定一张卡执行现有任务

进入 `t100@t100-eight-card:~$` 后，先按 [操作教程](操作教程.md) 编译 `~/task-dev/my-task`。然后：

```sh
cd ~/task-dev
sudo ./my-task 3 4096 7 2
```

该命令选择 PCI BDF 排序中的卡 3，输入 4096 字节，执行现有 XOR 算法并校验，预期打印 `MY_TASK_PASS card=3 bytes=4096 mode=2`。它沿用启动时已装载的 MCU/NPU 程序，不接受自定义镜像路径。

`mode=1/2/3/4` 选择的是输入/输出 DMA 引擎，不是 MCU-only / NPU-only。现有 P5 runtime 在这些模式下都由 MCU 协调并通知 NPU 处理任务；即便 `mode=2` 使用 HDMA/HDMA，也仍会执行 NPU 算法。

如果需要按原版 demo 的设备名/BDF 选择端点：

```sh
sudo /opt/t100/run-host-demo
```

在 `ssv_test>` 中执行 `list-devices` 和 `set-device ep3`，或 `set-device 0000:06:00.0`（以实际枚举结果为准）。这只改变原版 Host demo 的当前端点；不能选择一份新的 MCU/NPU 程序启动。

## 2. 通过命令指定 Host 脚本和卡号

在 Ubuntu 创建脚本：

```sh
cat > ~/task-dev/run-card.sh <<'EOF'
#!/bin/bash
set -euo pipefail
card=${1:?usage: run-card.sh CARD}
cd "$HOME/task-dev"
sudo ./my-task "$card" 4096 7 2
EOF

bash "$HOME/task-dev/run-card.sh" 3
```

脚本路径可以替换为同事自己的 Host 脚本。卡号作为第一个参数传入，脚本应将它传给真正支持选卡的 Host 程序；平台不会自动解释任意脚本内部的卡号。

在**外层容器、QEMU 仓库根目录**，也可指定来宾脚本执行：

```sh
python3 zc_simulator/desktop/control.py ssh \
  'bash /home/t100/task-dev/run-card.sh 3'
```

路径 `/home/t100/...` 属于 Ubuntu；若外层脚本放在 `desktop/state/share/jobs/job.sh`，来宾中相应路径为 `/mnt/hostshare/jobs/job.sh`，可用 `bash /mnt/hostshare/jobs/job.sh 3` 执行。脚本需符合现有 Host/设备协议，同一张卡的共享缓冲和 mailbox 不应由多个测试同时操作。

## 3. 底层 simulator 选择 MCU-only / NPU-only / 两者

以下命令在**外层容器**执行，启动独立 simulator 实例；它们不会连接、替换或选中当前 Ubuntu 八卡中的某张 PCIe 卡。这里的 hart ID 表示一个 simulator 内的执行核，和 PCI BDF/卡号是两套编号。

先准备与入口地址匹配、能够独立运行的裸二进制：

```sh
cd /workspace/GIT/pcie_qemu/qemu
export CORE_WORK="$PWD/zc_simulator/desktop/state/core-dev"
mkdir -p "$CORE_WORK"
# 将工具链生成的文件复制为：
# "$CORE_WORK/mcu.bin"，链接入口 0x40000000
# "$CORE_WORK/npu.bin"，链接入口 0x4000000000
```

MCU 的编译工程见 [MCU 编译教程](MCU编译教程.md)。如果复用其中的启动/链接模板构建 NPU 上的 RISC-V 程序，需将 `LOAD_BASE` 改为 `0x4000000000` 并导出 `npu.bin`；模型专用 Tensor/DMA 指令还需要匹配工具链、描述符、输入和模型配置。

分别生成三种配置：

```sh
cat > "$CORE_WORK/mcu.cfg" <<'EOF'
runtime_func:1
pc_num:1
[1, 0x40000000, ./mcu.bin]
ddr_input_num:0
ddr_output_num:0
l1buf_input_num:0
l1buf_output_num:0
EOF

cat > "$CORE_WORK/npu.cfg" <<'EOF'
runtime_func:1
pc_num:1
[16, 0x4000000000, ./npu.bin]
ddr_input_num:0
ddr_output_num:0
l1buf_input_num:0
l1buf_output_num:0
EOF

cat > "$CORE_WORK/both.cfg" <<'EOF'
runtime_func:1
pc_num:2
[1, 0x40000000, ./mcu.bin]
[16, 0x4000000000, ./npu.bin]
ddr_input_num:0
ddr_output_num:0
l1buf_input_num:0
l1buf_output_num:0
EOF
```

以当前发布包内的 simulator/firmware 执行，三条运行命令中选择一条：

```sh
export CORE_BUNDLE="$(readlink -f zc_simulator/simu/current)"
export CORE_ISA=rv64imafdcv_zvl512b_zve32d_zfh_zvfh_zfbfmin_zvfbfmin_zba_zbb_zbs_zicond
cd "$CORE_WORK"

# 只装载、派发 MCU0 的业务程序
timeout 30 "$CORE_BUNDLE/bin/spike" --isa="$CORE_ISA" --runtime-func \
  "$CORE_BUNDLE/images/firmware.riscv" -cfg "$CORE_WORK/mcu.cfg" > mcu.log 2>&1

# 只装载、派发 NPU0 的业务程序
timeout 30 "$CORE_BUNDLE/bin/spike" --isa="$CORE_ISA" --runtime-func \
  "$CORE_BUNDLE/images/firmware.riscv" -cfg "$CORE_WORK/npu.cfg" > npu.log 2>&1

# 同时装载、派发 MCU0 和 NPU0 的业务程序
timeout 30 "$CORE_BUNDLE/bin/spike" --isa="$CORE_ISA" --runtime-func \
  "$CORE_BUNDLE/images/firmware.riscv" -cfg "$CORE_WORK/both.cfg" > both.log 2>&1
```

`timeout` 返回 124 表示外层限时结束，不代表业务成功。根据需要调整 30 秒；常驻轮询程序本来就不会自然退出。配置只选业务 hart，启动 firmware 的 hart 0 和 simulator 创建的其他模型对象仍然存在。两者模式会共同派发，程序间依赖和数据同步需要由 MCU/NPU 自己实现。

如需要指定自己的运行脚本，可把上面选定的一条命令保存为外层 `run-my-cores.sh` 后执行 `bash /绝对路径/run-my-cores.sh`。脚本使用的 BIN、cfg 和工作目录都属于外层 simulator，不属于 Ubuntu 来宾。

## 4. NPU 指令、输入、输出和完成标志

`npu.bin` 需是 NPU hart 能执行的指令二进制；不能把 `.S` 文本、Python 脚本或一份没有约定格式的指令列表直接交给 `-cfg` 执行。汇编源码应先用匹配工具链编译、链接并通过 `objcopy` 导出 BIN；需设置 Tensor/DMA 参数的程序还应装入相应数据。

输入/输出的配置行分别采用：

```text
ddr_input_num:1
[0x4000100000, ./input.bin]
ddr_output_num:1
[0x4000200000, ./output.bin, 0x1000]
l1buf_input_num:1
[0x10000000, ./l1-input.bin]
l1buf_output_num:1
[0x10009200, ./l1-output.bin, 0x100]
```

这是格式示例，需替换原配置中的对应计数和条目，地址/长度必须与自己的程序一致，不能覆盖代码、栈或其他输入。输出行顺序是 **地址、文件路径、字节数**。

独立 runtime 程序正常结束时需向 dispatcher 写退出码。例如 MCU0 写 `0x7c000010`，NPU0 写 `0x7c000018`；写 0 后保持自旋，待所有配置业务 hart 完成，由 firmware 导出输出并结束模拟器。只从 `main` 返回通常会回到启动代码的自旋，不能作为任务完成条件。有关中断、mailbox 和退出槽位，见 [runtime 用例说明](../../../simulator/riscv-tests/runtime_test/README.md)。

现有 P5 的 `mcu.bin`/`npu.bin` 需要共享状态、访问代理及 Host 会话，不能保证抽出任一镜像单独执行就能完成。NPU-only 程序也不能等待一个根本没有启动的 MCU；MCU-only 程序不能无限等待 NPU 回包。

现有 `zc_simulator/p5/run_tasks.py` 可运行固定 Tensor 测试，但脚本内部固定八卡和测试程序，没有 `--card`、`--script` 或任意 NPU 程序路径选项。它也不是当前桌面已安装的通用 NPU 指令执行器。

## 5. 本次验证与桌面端尚需补齐的部分

2026-11-02 已使用当前发布包的 Spike 和 firmware，在隔离目录构建并运行三组最小程序：MCU-only、NPU-only、MCU+NPU。各组均通过 dispatcher 正常结束，退出码为 0，并导出 DDR 中对应的 MCU/NPU 标记校验通过。该验证覆盖启动选择、指令执行、完成和输出，不覆盖任意 Tensor 指令包或桌面 PCIe 卡的动态装载。

证据在 `desktop/state/core-selection-validation/`：`main.c`、`start.S`、`link.ld`、三份 cfg、各自的日志/输出及 `results.json`。初次输出配置字段顺序错误的日志以 `*-initial-wrong-output-config.log` 保留；修正后重新执行并核对实际输出，没有仅凭进程退出码判定成功。

若要满足同事完整需求，仍需增加桌面端的按卡镜像/任务配置入口，处理每卡输入目录与摘要、MCU/NPU 独立启动的 ready/完成约定，以及自定义 NPU 程序的装载和调用 ABI。现有 `--cards`、`set-device`、DMA `mode` 参数都不能替代这些功能。
