# 单 die 信息包闭环首轮执行报告

执行日期：2026-10-26。此报告对应[实施指南](单die信息包执行闭环实施指南.md)及需求方授权的“先按 simu 内部需求做假包”。所有实现均为内部实验。

## 1. 结论

已完成本地构建、旧模式基线、mailbox 验证、四 core 模型回归及实验假包闭环。**同一个 Spike 进程中的 MCU 实验 runtime，连续接收并驱动三次真实 Tensor 动作，输出均与旧模式逐字节相同；五个非法请求被拒绝且不增加执行次数。**

当前验证的是自定义假包和新写的最小 runtime，正式工具链/生产 runtime、PCIe 连接与多 core 业务路由尚未验证。旧示例与 golden 的逐元素差异仍需数值口径确认。

## 2. 构建结果与环境处理

| 项目 | 实际执行 | 结果 |
| --- | --- | --- |
| Spike | 新建 `simulator/riscv-isa-sim/build`，`bash ../configure`，`make -j64` | 成功 |
| 裸机程序 | 在 `simulator/riscv-tests` 执行 `make -j64 RISCV_SIM=/data/ci/simulator/main_delivery/main/spike` | 全部构建成功，含本地 firmware 与 dispatch |
| MCU/NPU 常驻程序 | 新实验目录执行 Makefile | 成功，由本地 RISC-V GCC 从源码构建 |
| MoE 原业务程序 | 使用已复制的业务 bin | 未从 SDK 源码重建，当前未找到完整 SDK 构建依赖 |

复制后的 `configure` 没有可执行位，因此用 bash 运行；裸机 Makefile 会检查 Spike 是否存在，所以构建时显式提供交付版路径。该变量仅满足构建检查，所有运行验证实际使用新构建的 `simulator/riscv-isa-sim/build/spike` 和 `simulator/riscv-tests/firmware.riscv`。

没有清空源目录，也没有覆盖原示例输出。所有验证输出位于独立实验目录。

## 3. 已完成的验证

| 用例 | 结果与证据 |
| --- | --- |
| dispatch | 退出码 0，包含 `Test Dispatch finish!` |
| MoE 原模式 | 退出码 0，生成 33792 字节新输出；与原目录复制来的输出逐字节一致 |
| MCU0→NPU3→MCU0 mailbox | 本地生成测试 bin；运行退出码 0；完整 16 字节结果与预期一致 |
| SoC NoC、MIF adapter、reorder | `Makefile.die check` 对应用例通过 |
| single_die_4core_test | 正常及 retry-stress 通过，remote=56、DDR=50 的请求均完成 |
| GDB 追踪 | 命中 `sm_do_tensor` 和 `sm_npu_tensor_t::run`，保存调用栈 |
| 假包闭环 | A/B 正常，五个非法请求拒绝，C 恢复执行成功，STOP 后整个程序正常退出 |

默认配置为 `sm_func_mode=true`、`sm_perf_mode=false`。四 core 互联回归是独立 standalone 模型测试，不能等同于四个 core 都已运行信息包业务；本轮业务目标只有 NPU0。

### 数值检查的实际结果

旧 MoE 的原始比较脚本余弦相似度为 99.999553%，超过其 99.900% 门槛。但该脚本只比较 1109 个非零且有限元素。

本轮按 `gen_golden.py` 中的布局核对：66 行、每行 256 个 BF16 槽位，gather 对应的 33 行各 34 个有效输出，共 1122 个元素。新输出与 golden 的有效输出中有 322 个 BF16 编码不同，最大绝对误差为 1.0；有效区域无非有限值，区域外编码差异为 0。

因此只能确认旧模式复现及新旧路径等价，不能宣称与 golden 位级一致或符合尚未约定的逐元素容差。后续需确认量化、舍入及接近零结果的判定标准，不修改 golden 来掩盖差异。

## 4. 假包实现与调用链

新增代码位于[packet_runtime_test](../../simulator/riscv-tests/packet_runtime_test/实验收包执行说明.md)。

```text
Host Python：启动后写入一笔 160 字节假包
  → FIFO / 既有 sm_file MMIO
  → MCU0（hart1）实验 runtime：校验包并发布共享槽
  → NPU0（hart16）常驻执行器：加载 128 字节描述符向量
  → 执行 0x8204000b Tensor 指令
  → sm_do_tensor → scheduler enqueue → Tensor 运算
  → 增强 fence 等待队列/设备空闲
  → 复制输出到共享 DDR、发布完成
  → MCU 回传命令结果 → Host 校验后发送下一包
```

假包包含 magic、版本、长度、命令 ID、core、动作类型、固定 Tensor 指令和 128 字节参数向量。参数从旧程序的真实 `sm_do_tensor` 入口提取；执行器恢复 v8/v9 及 VL32/SEW32/LMUL2 上下文。不是从完整业务 bin 随意截取字节，也没有直接调用计算函数绕开 scheduler。

启动仅加载 firmware、MCU 实验 runtime、NPU 常驻执行器和输入数据，未加载完整 MoE 业务程序。每笔动作前清空输出；执行器不会在单笔完成时退出。

MCU/NPU 之间采用共享 DDR 轮询。Host FIFO 是阻塞式本地实验载体，外层等待超时可终止进程；它不适用于最终 QEMU 非阻塞事件连接，后续应替换。

### 九笔请求验收

| 顺序 | 请求 | 返回状态 | 累计提交/执行 |
| --- | --- | --- | --- |
| 1 | A，ID=1 | 成功，33792 字节结果 | 1/1 |
| 2 | B，ID=2 | 成功，33792 字节结果 | 2/2 |
| 3 | 重复 ID=2 | 错误 4 | 2/2 |
| 4 | 无效 ID=0 | 错误 4 | 2/2 |
| 5 | 包头长度字段=159 | 错误 1 | 2/2 |
| 6 | core=4 | 错误 2 | 2/2 |
| 7 | 不支持的指令 | 错误 3 | 2/2 |
| 8 | C，ID=6 | 成功，33792 字节结果 | 3/3 |
| 9 | STOP，ID=7 | 成功，随后退出 | 3/3 |

A/B/C 输出 SHA256 均为 `37121ee2cd6afaed02f18587ed683d6fb5d5c4a6cd3b078f682a77891fe7e015`，与旧模式输出完全相同。

## 5. 模型接口对齐结果

| 层次 | 已定位入口 | 后续注意事项 |
| --- | --- | --- |
| firmware 启动 | `firmware/firmware.c`：`read_file`、`start_npu_idx` | 保留启动加载；不以它替代业务逐包提交 |
| hart 启动 | `smart/sm_dispatch.cc`：`dispatch` | 设置 PC/a0..a7；仅用于启动常驻程序 |
| Tensor decode | `smart/sm_insns.cc`：`sm_do_tensor`、`tmm_get_params` | opcode 本身不够，还依赖向量描述符状态 |
| 参数提取 | `smart/sm_vector_ext.h`：`extract_desc_info` | VLEN/SEW/LMUL/VL 与参数必须一致 |
| 入队/执行 | `smart/sm_schedule.cc`：`schedule_tensor_insn` | 复用队列和调度，不能从宿主线程随意调用 |
| 数据运算 | `smart/sm_npu.cc`：`sm_npu_tensor_t::run` | 返回后仍需考虑 Busy/完成状态 |
| 功能完成 | 增强 fence → `call_fence`、`sm_npu_t::update_status` | 实验使用队列为空且设备空闲的等待条件 |
| 正式包与 runtime | 尚未提供 | 本轮新写的 runtime 仅证明实验机制可行 |

## 6. 文件与复现入口

- [基线运行脚本](../../experiments/single_die_packet/run_baselines.py)：构建完成后执行 dispatch、MoE、mailbox 并保存独立结果。
- [假包运行脚本](../../simulator/riscv-tests/packet_runtime_test/run_mock.py)：编译实验程序并执行九笔请求测试。
- [假包格式与实现说明](../../simulator/riscv-tests/packet_runtime_test/实验收包执行说明.md)：包布局、固定地址、入口及限制。
- [基线结果](../../experiments/single_die_packet/run-20261026T081226-083744Z/results.json)。
- [假包最终结果](../../experiments/single_die_packet/mock-1793003076810945024/results.json)。
- [模型回归日志](../../experiments/single_die_packet/single-die-tests.log)、[GDB 追踪](../../experiments/single_die_packet/gdb-trace.log)。

```bash
cd /workspace/GIT/pcie_qemu
python3 experiments/single_die_packet/run_baselines.py
python3 simulator/riscv-tests/packet_runtime_test/run_mock.py
```

实验过程中修正了常驻程序入口布局和 Vector 状态初始化，失败日志也保留在 `experiments/single_die_packet/`。上述“假包最终结果”对应修正后通过版本。

## 7. 下一步

当前可进入实验接口设计：把 FIFO/sm_file 入口替换为有界、非阻塞的提交适配，保持 MCU 接包、NPU 动作和真实完成不变，再接入 QEMU EP。优先补包缓冲所有权、队列满、断连、取消/复位与命令 ID 的语义。

正式软件联调仍需真实工具链包、生产 runtime 和硬件提交协议。应将假包解析层与模型执行适配分开替换，保留本轮结果作为回归；不能将实验协议直接视为正式 BAR/doorbell 或驱动 ABI。
