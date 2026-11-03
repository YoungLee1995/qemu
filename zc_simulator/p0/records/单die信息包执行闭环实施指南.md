# 单 die 信息包执行闭环实施指南

## 1. 任务目标和完成标准

2026-10-27 状态补充：本文保留早期实施步骤与检查时的状态，不能将下文“本地 Spike 未发现”“runtime 待确认”等历史表述视作当前结论。runtime 已完成开发调试，当前源码核对、重跑证据、P0 准入结论及后续 P0—P4 计划见 [P0 阶段准入评估与 P 阶段开发方案](P0阶段准入评估与P阶段开发方案.md)。本地单 die 基线、mailbox/ECLIC 与假包 Tensor 闭环已再次验证；真实 Host 驱动联合路径另行验收。

首轮实际执行结果及授权假包实验见[单 die 信息包闭环首轮执行报告](单die信息包闭环首轮执行报告.md)。报告区分了已验证的实验 runtime 与尚未提供的正式软件接口。

本文细化早期单 die 信息包实验步骤；当前开发范围以[分阶段实现方案](内部PCIe互联平台分阶段实现方案.md)及上述 P0 评估为准，用于内部本地实验，不用于 QEMU 上游贡献。

本轮只做一张卡、一个 die、一个选定 NPU core、一类操作的闭环，保留自研单 die 4 core 完整模型。暂不接入 QEMU PCIe，不开发八卡或性能模型。

目标链路：

```text
测试提交端发送真实工具链信息包
  → MCU 上的常驻 runtime 接收并解析
  → 准备描述符、寄存器上下文并提交单笔动作
  → 现有 NPU 模型调度执行
  → 结果可见且动作真实完成
  → runtime 回传命令完成
```

同一进程、同一 runtime 连续完成两包；第二包在第一包完成后提交，执行期间不重启 Spike、不重新装载完整业务程序。固件、runtime 和必要的常驻执行器仍可在启动阶段加载。

“已收到包”“指令已退休”“scheduler 已入队”“计算和 DMA 已结束”必须分别辨认。最终成功以真实完成和数据校验为准。

## 2. 当前代码事实及尚缺内容

以下是静态检查结果，不表示已构建或运行：

| 对象 | 当前事实 | 实施含义 |
| --- | --- | --- |
| 典型用例 | 工作区根目录有 `moe_bf16_sint4_33x29x34_fix/` | 可用作输入、描述符及结果参考 |
| 旧启动方式 | `run.sh` 引用 `/data/ci/simulator/main_delivery/main/` 下的 Spike 和 firmware | 不能直接认为运行的是当前复制的源码版本 |
| 本地 Spike | 检查时未发现 `simulator/riscv-isa-sim/build/spike` | 先建立本地构建；若使用其他路径，明确版本和来源 |
| 原程序加载 | cfg 将完整业务 bin 加载到 hart16 的 `0x20000000` | 旧基线使用它；新收包实验不加载该业务 bin |
| 数据输入 | 四份 L1 数据，输出为 33792 字节 | 新旧链路应复用相同输入及结果布局 |
| 用例重建 | Makefile 引用 `../../../Build/Makefile.base`，源码引用 `common_gorden` 等 SDK 头文件 | 先检查依赖；只有此用例目录不保证可重建 |
| runtime 资料 | 已有 mailbox/ECLIC 测试及相关源码 | 可复用通知机制；完整命令 runtime 的位置仍需确认 |
| 动作入口 | `sm_do_tensor()` 会解析并入队 | 可作为模型执行路径定位起点，尚不是外部稳定 API |

必须取得的外部输入：真实工具链样包和格式说明、runtime 收包/解析/提交源码、所选动作的前置配置与完成定义。缺少这些时继续基线和静态分析，记录缺口，不自行宣称协议已冻结。

## 3. 建立目录与运行记录

以下命令均为待执行示例，本文没有替用户构建或运行。先在终端设置工作区变量；后续命令使用同一个 shell：

```bash
export PCIE_WORK=/workspace/GIT/pcie_qemu
export PCIE_SIM="$PCIE_WORK/simulator/riscv-isa-sim"
export PCIE_TESTS="$PCIE_WORK/simulator/riscv-tests"
export PCIE_CASE="$PCIE_WORK/moe_bf16_sint4_33x29x34_fix"
export PCIE_EVIDENCE="$PCIE_WORK/experiments/single_die_packet/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$PCIE_EVIDENCE"
```

建议后续新增实验源码放在 `simulator/riscv-tests/packet_runtime_test/`，分别放 `runtime/`、`executor/`、`host/`、`configs/`、`tests/`。这些是建议位置，目前未创建，不存在可直接运行的收包测试命令。

每次运行记录：源码版本或源码快照标识、Spike/firmware/runtime 文件哈希、编译器版本、编译参数、配置、输入哈希、命令、退出状态和新生成输出。复制目录没有 Git 元数据时，不把 QEMU 的 commit 当作 simulator 版本。

## 4. 操作一：环境检查与旧用例复现

### 4.1 检查构建依赖

```bash
command -v g++
command -v make
command -v python3
python3 -c 'import numpy; print(numpy.__version__)'
test -x /data/nuclei/gcc/bin/riscv64-unknown-elf-gcc
ls "$PCIE_TESTS/firmware.riscv" "$PCIE_CASE/simulator.cfg"
rg -n 'NUCLEI_SDK_ROOT|include|INCDIRS|TOOLCHAIN' "$PCIE_CASE/Makefile"
rg -n 'SDK_ROOT|GCC_BIN|common_gorden|dump_descriptor' "$PCIE_CASE/build.sh"
```

补齐 SDK 的 `Build/Makefile.base`、`common_gorden`、`common_v2`、NPU/SoC/NMSIS 等实际依赖，并将 SDK 根路径参数化。不要在目录已迁移的情况下直接使用 `../../../` 推断 SDK 位置。先确认工具链支持当前自定义扩展，不能仅因能编译普通 RISC-V 程序就认定可编译本用例。

### 4.2 构建本地模型和 firmware

已有正确配置的 build 时保留并检查配置，不执行 `rm -r *`。首次构建可按下面方式开始，具体依赖和 configure 选项以本仓库要求为准：

```bash
mkdir -p "$PCIE_SIM/build"
cd "$PCIE_SIM/build"
../configure
make -j8
export PCIE_SPIKE="$PCIE_SIM/build/spike"
"$PCIE_SPIKE" --help > "$PCIE_EVIDENCE/spike-help.txt" 2>&1

cd "$PCIE_TESTS"
make -j8 firmware.riscv RISCV_PREFIX=/data/nuclei/gcc/bin/riscv64-unknown-elf-
export PCIE_FIRMWARE="$PCIE_TESTS/firmware.riscv"
sha256sum "$PCIE_SPIKE" "$PCIE_FIRMWARE" > "$PCIE_EVIDENCE/binaries.sha256"
```

构建失败时先记录第一处错误并补依赖，不用全目录清理掩盖配置问题。若先运行交付的二进制，标注“交付版本基线”；随后仍需补本地源码构建验证。固件成功构建不证明 MoE SDK 程序也能重建。

### 4.3 使用旧业务 bin 跑参考结果

在用例目录运行，使用明确的本地路径和有界超时。脚本整体建议保存成实验脚本执行，以免 `exit` 结束交互终端：

```bash
set -euo pipefail
cd "$PCIE_CASE"
if [ -e data/result_l1_spike.bin ]; then
    mv data/result_l1_spike.bin "$PCIE_EVIDENCE/previous-result.bin"
fi
sha256sum simulator.cfg moe_bf16_sint4_33x29x34_fix.bin data/*l1.bin \
    > "$PCIE_EVIDENCE/inputs.sha256"
timeout 300s "$PCIE_SPIKE" \
    --isa=rv64imafdcv_zvl512b_zve32d_zfh_zvfh_zfbfmin_zvfbfmin_zba_zbb_zbs_zicond \
    "$PCIE_FIRMWARE" -cfg ./simulator.cfg \
    2>&1 | tee "$PCIE_EVIDENCE/legacy.log"
test -f data/result_l1_spike.bin
test "$(stat -c %s data/result_l1_spike.bin)" -eq 33792
cp data/result_l1_spike.bin "$PCIE_EVIDENCE/legacy-result.bin"
python3 data/check_result.py 2>&1 | tee "$PCIE_EVIDENCE/legacy-check.log"
```

300 秒只是初始实验超时，不是性能要求；若超时，先检查日志判断是运行较慢还是挂起，再调整并记录。原 `run.sh` 没有启用 `pipefail`，且结束提示不代表 Spike 成功，以上流程补充了退出状态与新输出检查。

原比较脚本会取较短长度并过滤零值和非有限值。它只能作为辅助参考；还需核实 golden 大小、有效数据布局和数值容差。不要把 33792 字节中的 padding 当作 33×34 的连续有效矩阵。

**本操作产物**：基线日志、文件哈希、新输出、结果判定，以及“仅运行既有 bin”或“已源码重建”的明确标记。

## 5. 操作二：拿样包并填写执行契约

先让工具链/runtime 提供方交付一份能独立解释的样包，而不是整个业务 ELF 或反汇编。包若引用外部描述符，还须提供描述符二进制、布局和初始化顺序。

| 需填写内容 | 必须回答的问题 |
| --- | --- |
| 来源与粒度 | 哪个工具链版本产生？一包是一条 ISA 指令、一个 Tensor 动作，还是其他单元？ |
| 包布局 | 字段偏移、大小、端序、总长、版本、操作码、校验和是否存在？ |
| 目标 | card/die/core 如何指定？core0 与 hart16 是否映射？ |
| 参数 | 哪些在包内，哪些来自描述符、寄存器或上下文？ |
| 地址 | 描述符和数据地址是什么地址域？范围、对齐、生命周期是什么？ |
| runtime | 哪个 MCU 执行？具体接收函数、解析函数、提交函数在哪里？ |
| 动作依赖 | 是否依赖 DDEP、barrier、通道/单元配置或先前指令？ |
| 完成 | 哪个事件证明操作完成且输出可见？失败如何返回？ |

没有真实样包时，可以定义明确标注为实验格式的测试 envelope 验证收包，但不得把它作为最终工具链 ABI；此时状态是“协议待对齐”，不是第一步已通过。

## 6. 操作三：定位 runtime 到模型的执行入口

### 6.1 按已确认的代码入口追踪

```bash
cd "$PCIE_WORK"
rg -n 'matmulMoeQuant|L1_' "$PCIE_CASE/test_moe.cc"
rg -n 'sm_do_tensor|tmm_get_params|extract_desc_info' "$PCIE_SIM/smart/sm_insns.cc"
rg -n 'enqueue|schedule_tensor_insn|commit_done|func_mirror_exec' \
    "$PCIE_SIM/smart/sm_schedule.cc"
rg -n 'Busy|tick|run_matmul_desc' "$PCIE_SIM/smart/sm_npu.cc"
rg -n 'runtime_func|read_file|start_npu_idx' "$PCIE_TESTS/firmware/firmware.c"
```

Tensor 路径的静态起点为：

```text
riscv/insns/ism_tensor.h
  → smart/sm_insns.cc::sm_do_tensor(processor_t*, insn_t)
    → assure_avail / tmm_get_params / scheduler enqueue
      → smart/sm_schedule.cc 的调度与推进
        → Tensor 执行、内存访问与完成状态更新
```

`sm_do_tensor()` 会读取当前 processor 的上下文并生成描述符对象，不能只构造一个操作码就从任意线程调用。`sm_npu_tensor_t::run()` 下还有 Busy/周期状态；函数返回不应直接等价为命令完成。

中间性能模型的定位参考为 `smart/mid_perf/docs/tmm_perf_simulation_flow.md`。它描述了动作入队、模型推进和 done 回写路径；实际运行模式须从当前配置核实，不能把某模式下的完成事件套给所有模式。功能计算和性能模型的数据镜像路径也需确认。

`smart/sm_dispatch.cc` 负责设置 hart PC/参数并启动 hart。它可启动常驻执行器，但不是 Tensor 指令入队接口；不要每笔命令都靠重新启动整个 firmware 处理。

### 6.2 用断点验证调用链

本地构建具有调试符号时，可在旧用例目录启动 GDB：

```bash
cd "$PCIE_CASE"
gdb --args "$PCIE_SPIKE" \
    --isa=rv64imafdcv_zvl512b_zve32d_zfh_zvfh_zfbfmin_zvfbfmin_zba_zbb_zbs_zicond \
    "$PCIE_FIRMWARE" -cfg ./simulator.cfg
```

建议断点为 `sm_do_tensor` 和 `sm_npu_tensor_t::run`；通过 `bt` 核对调用者，并在 scheduler 的实际完成分支记录入队、开始、完成顺序。若断点未命中，先核对构建版本和运行模式。GDB 调试可单独运行，不使用旧基线的 300 秒超时。

还需从 SDK 找到 `matmulMoeQuant` 的实现，核实它生成的设置指令、描述符和目标操作。不要猜测反汇编中的某个自定义指令就是全部动作；若反汇编不能识别扩展，用匹配的工具链解码。

**本操作产物**：一张“包字段 → runtime 字段 → processor/描述符状态 → 调度队列 → 完成信号”的表，列具体文件和函数，并区分已验证与待验证。

## 7. 操作四：选择接入方式并实现最小闭环

### 7.1 先选执行接入层

| 方式 | 实现内容 | 采用条件 |
| --- | --- | --- |
| 常驻 NPU 执行器 | MCU runtime 通过 mailbox/队列把解析后的动作交给目标 NPU；常驻代码设置上下文并执行对应操作，等待完成后回报 | 操作类型已知且能复用 ISA 路径，建议优先评估 |
| 有界动态指令片段 | runtime 根据真实包生成该动作必要的设置/操作/等待片段，由执行器调用 | 必须支持动态编码时评估；须确认可执行区、跳转边界、取指缓存失效及同步 |
| 模型动作提交接口 | 在仿真 MMIO 或等效设备接口中增加动作提交，映射为已有 scheduler 请求 | 真实 runtime 的执行方式适合该契约时采用；属于显式记录的仿真适配，不能伪装成真实硬件接口 |

三者不并行实现。根据真实包和 runtime 已有实现选择一种；所有方案都保留 scheduler、内存路径、上下文隔离和真实完成定义。直接调用 `run_matmul_desc()` 仅用于结果对照，不作闭环主路径。

### 7.2 实验模块与修改顺序

下面是建议新增的逻辑职责，并非现有 API 名称或已完成的代码：

| 顺序 | 模块 | 具体实现工作 |
| --- | --- | --- |
| 1 | 包定义与解析 | 复用真实工具链格式；显式解码端序和长度，检查溢出、未知操作、目标和地址，避免把未对齐字节直接强转为结构体 |
| 2 | MCU runtime 命令循环 | 实现 READY、收包、校验、提交、等待、回报；本轮单 outstanding，上一包完成后才能复用其内存 |
| 3 | NPU 动作执行适配 | 按选定方式设置描述符/上下文并提交到已有模型；完成后保持等待下一命令 |
| 4 | 本地测试提交端 | 在仿真设备提供的受控入口投递到 MCU 可见队列，并通知 MCU；不直接调用 parser 或计算函数冒充 MCU 执行 |
| 5 | 完成出口与 trace | 将命令 ID 关联到真实完成及错误；提交端读回 completion 和结果 |
| 6 | 配置及回归脚本 | 启动固件、常驻 runtime/执行器及初始数据；包由启动后提交，保存完整日志和退出状态 |

收包入口若尚不存在，可新增最小仿真 MMIO inbox/outbox 设备：宿主测试提交端经仿真事件投递请求，设备把包放入 MCU 可见区域；runtime 以 MMIO/内存访问消费。该设备只是本地实验载体，后续由 PCIe 传输接入同一语义入口，不提前规定 BAR 偏移。

首轮可由提交端将包写入队列缓冲后更新 producer 状态，runtime 完成读取后释放槽位；具体内存屏障、缓存处理和通知按 MCU 模型规定实现。输出和 completion 内容写入可见后，最后发布完成标志；消费者先确认标志再读取内容。

宿主提交通过仿真调度上下文处理，不从外部线程直接操作 hart、队列或 MemoryRegion 状态。若本地提交使用 socket，它只承担测试端到仿真入口的载体，不替代设备 runtime。

### 7.3 启动和常驻逻辑

现有 runtime mailbox 测试依赖 simulator 的 `--runtime-func` 与 firmware cfg 中的 `runtime_func:1`，可作为启动配置参考。实际工程需明确 MCU 与 NPU 的常驻程序及入口；现有 pingpong 测试程序不能直接当作命令 runtime。

建议启动顺序：

```text
启动 Spike / 完整 die 模型
→ firmware 加载 MCU runtime 和 NPU 常驻执行器
→ 初始化输入、权重、描述符缓冲、命令和完成队列
→ MCU/NPU 发布 READY
→ 提交端发送 A → runtime 提交 → 模型完成 → 返回 A
→ 提交端发送 B → runtime 提交 → 模型完成 → 返回 B
→ 提交端发送实验停止请求 → 各 hart 完成受控退出
```

现有测试通过 dispatcher exit-code MMIO 结束 hart，不能把这一路径用作每笔命令的完成通知，否则无法保持常驻。实验结束或不可恢复故障时才执行整体退出。

### 7.4 单包生命周期

```text
RECEIVED → VALIDATED → SUBMITTED → EXECUTING → COMPLETED
    └─ 校验失败 → REJECTED
                     已提交执行失败 → FAILED
```

这些是实验状态名称，不预定硬件状态寄存器编码。首轮通过软件记录命令 ID、core、复位代次、动作及状态转移；超时意味着等待失败，不意味着设备已停止操作。只有完成或确认取消后才能释放描述符和数据缓冲。

## 8. 操作五：两包验证、数值校验和错误处理

| 用例 | 输入/操作 | 必须检查 |
| --- | --- | --- |
| 包 A 正常执行 | 原示例输入和已对齐的 Tensor 信息包 | 包参数正确、模型执行、输出大小/布局/数值及真实完成 |
| 包 B 连续执行 | A 完成后清理输出，重新提交；可使用相同输入 | 不重启、不读取 A 残留输出，B 有独立命令 ID 和一次完成 |
| 上下文更新 | ABI 支持时，B 改为第二个有效输出缓冲 | 无旧描述符/寄存器串扰，缓冲布局已确认有效 |
| 截断/非法包 | 长度不足、未知操作或非法描述符 | 明确拒绝，未进入执行队列，runtime 仍可接收后续合法包 |
| 错误目标 | core 超出 0..3，具体字段以 ABI 为准 | 不误投递至其他 hart |
| 执行等待超时 | 测试限制或可控故障 | 返回可诊断状态，不提前复用在途内存；可恢复方式明确 |

第一轮只执行选定 core；其他 core 保留完整模型，后续扩展路由验证。重复命令 ID、复位代次和并发是下一轮完善项，必须在接入 PCIe 前定义。

结果校验按以下顺序实现：

1. 检查文件由本次执行生成且长度为预期；同一布局的新旧路径输出均应为 33792 字节。
2. 从 SDK 明确有效元素、分块、stride、padding 和 BF16 编码；不能直接将所有字节解释成连续矩阵。
3. 相同确定性功能模型及输入，先对有效输出做位级比较；若不一致，逐项定位解析、上下文或模型路径差异。
4. 若硬件语义允许数值差异，预先约定绝对/相对容差，逐元素比较，单独定义零、NaN/Inf 规则；报告最大误差和失败元素数，不只用余弦相似度。
5. 保存包字节、解析字段、动作/完成 trace、输出及比较报告，使任一失败可按命令 ID 回溯。

建议最小日志字段：`run_id`、命令 ID、core/hart、复位代次、操作、接收/校验/提交/完成状态、描述符地址、输出范围、错误码。仿真时间用于顺序诊断，不宣称 PCIe 性能。

## 9. 交付物、阻塞判断与下一步

本轮交付：

- 基线版本清单、可复现启动命令及旧示例结果。
- 《单 die 信息包执行接口对齐清单》：真实包格式、runtime 函数、模型函数、地址/完成语义，逐项标注证据。
- 新增实验代码与运行脚本：MCU runtime 接包、NPU 动作执行、本地提交、完成回报。
- A/B 两包与非法包的回归报告；列出尚未实现的协议、动作和错误场景。

**第一项可以立即执行的工作**：检查本地构建与 SDK 依赖，建立旧样例结果，同时取得真实信息包和命令 runtime 源码；接着定位 `sm_do_tensor → scheduler → 完成` 调用链，形成字段映射表。没有真实包/runtime 时，停留在明确记录的接口研究阶段，继续可独立完成的基线与模型分析。

只有“真实 runtime 按包驱动真实模型、连续两包不重启、结果与失败可校验”全部通过，才开始把本地提交载体替换为来宾 Host/驱动/PCIe EP，并接入 IOMMU、ATU 和数据搬运路径。后续还需验证真实工具链发送端，不能以测试包回放端代替全部软件联调。
