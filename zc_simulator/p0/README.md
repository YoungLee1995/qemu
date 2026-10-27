# P0 实施入口

此目录用于内部联合仿真实验。P0 负责冻结软件和接口、形成 P1/P2 可执行设计；共享 DDR、真实 EP、HDMA 与外部 DMA 的功能实现属于后续阶段。

## 可执行基线

从工作区执行：

```bash
python3 qemu/zc_simulator/p0/run_p0.py --jobs 16
```

Python 3、numpy、GNU C/C++、make、dtc、Boost 开发库及 RISC-V 裸机工具链需要已安装。默认交叉工具前缀为 `/data/nuclei/gcc/bin/riscv64-unknown-elf-`，可用 `--tool-prefix` 覆盖。源码与 MoE 数据仍取当前工作区；它们的版本/差异/哈希随运行保存，不自动下载依赖。

每次创建 `evidence/run-<UTC时间>/`，重新 configure 并从空目录编译 Spike、firmware、dispatch、实验 MCU/NPU 程序与 standalone 测试。不会清理或覆盖原有 build。`--output` 必须指向尚不存在的目录；建议放在已忽略的 `evidence/` 下。首次构建耗时与磁盘占用以本机为准。失败和超时退出非零，失败步骤日志及结果仍保留；默认编译超时一小时，其余步骤十分钟。

运行顺序：源码快照 → 工具版本 → 新建 Spike/软件 → dispatch → MoE → mailbox 双向 → 双 MCU IRQ → 单实例连续任务/非法请求/STOP → standalone 五项 → 产物哈希 → 源码稳定性检查。

- `before/after-sources.json`：两个仓库 HEAD、工作区状态、patch 哈希；simulator 跟踪/未忽略文件及 QEMU 项目目录文件哈希。QEMU 上游源码由 HEAD 和完整差异定位。
- `before/after-*.patch`：相对 HEAD 的完整 tracked 差异，包括暂存改动。untracked 文件内容仍需随源码交付，哈希清单不能替代源码归档。
- `tools.json`：宿主及工具版本。
- `artifacts.json`：新构建执行文件、输入、配置及测试产物哈希。
- `results.json`：各步骤命令、目录、返回值、耗时及显式判据。
- `mock/results.json`：三笔任务、五笔拒绝、最后 STOP 的逐笔结果。

`local_baseline_pass=true` 表示本地软件回归通过；`p0_full_exit=false`、`real_driver_accepted=false`、`golden_accepted=false` 保留各自验收边界。MoE 必须匹配已冻结的旧路径哈希，golden 数值差异另报，不用宽松 checker 的退出码替代数值验收。

当前 QEMU 仅记录版本/差异，不重新构建或运行其历史阶段测试；P1 联合构建需将实际 guest kernel、驱动和 QEMU 配置纳入基线。

## 配置与后续实施

[启动与接口契约](启动与接口契约.json) 固定本轮实验镜像入口、核数、内部 mailbox 和内存地址，缺失字段使用 null/明确清单。该文件是登记契约，当前 runner 的工作流仍在 Python 中定义，不宣称已经由 JSON 驱动模型。

[P1适配设计与验收矩阵](P1适配设计与验收矩阵.md) 定义后续内存、消息、寄存器和生命周期的实施约束；[P0执行与验收记录](P0执行与验收记录.md) 区分已完成、设计完成和待外部资料关闭的项目。

运行日志、二进制、FIFO、构建目录等均在 `evidence/` 下忽略；脚本和契约进入版本管理。
