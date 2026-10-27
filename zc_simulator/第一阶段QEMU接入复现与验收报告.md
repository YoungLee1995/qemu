# 第一阶段 QEMU 接入复现与验收报告

日期：2026-10-26。范围：内部平台、本地实验、需求方授权的假包与最小 MCU runtime。关联[总体方案](内部PCIe互联平台分阶段实现方案.md)、[simu 首轮报告](单die信息包闭环首轮执行报告.md)及[接口决策与待办](第一阶段接口决策与待办清单.md)。

> 2026-10-27 范围澄清：本文保留第一阶段实际验收事实。用户确认 simu 内部 runtime 已与硬件对齐，PCIe 软件参考为现有 Zebu `pcie-demo`；文中“生产 runtime/真实驱动待提供”不再作为 P0 前置。第一阶段已完成实验接入适配，HDMA/iATU/MSI 等 PCIe 模型仍按 P1/P2 实现。最新阶段边界见 [P0执行与验收记录](p0/P0执行与验收记录.md)。

## 1. 阶段结论与边界

**第一阶段的实验技术验证已完成：QEMU 已实际开发和运行，来宾 Linux 能通过实验 PCIe EP 提交信息包，驱动真实单 die NPU 模型执行并收到正确结果。** 本阶段已形成运行基线、接口决策、错误/复位验证、IOMMU 与 P2P 风险记录，以及外部资料缺口和关闭条件。

该结论使用已获授权的实验接口：假包、最小实验 runtime、实验 BAR/doorbell。真实硬件驱动兼容、生产 runtime、真实工具链包仍未验收。第二阶段要求的 HDMA 加载、Host IOVA DMA 和 ATU 尚未完成，不能把当前单卡 EP 原型当作首版八卡功能平台。

## 2. 实际执行链路

```text
来宾 x86 Linux 用户态测试程序
  → sysfs 配置空间与 BAR0 mmap / doorbell
  → QEMU zc-pcie-sim（挂于单 switch 下行端口）
  → 非阻塞 Unix socket / QEMU 主事件循环
  → 独立 Python 桥接进程
  → FIFO / 既有 sm_file MMIO
  → Spike MCU0（hart1）实验 runtime 接包和解析
  → NPU0（hart16）常驻执行器提交单笔 Tensor 指令
  → 原 scheduler / Tensor 功能模型执行
  → 增强 fence 确认真正完成，回传结果
  → EP 完整接收后置 done / INTx
  → 来宾读取结果并 ACK
```

来宾 Linux 是需求中的 Host OS。运行 QEMU 的物理主机和 Spike 的宿主地址不等同于来宾 GPA/IOVA；本轮提交走 CPU MMIO，尚无 EP Host DMA。

## 3. 构建环境与版本

| 项目 | 实际版本/配置 |
| --- | --- |
| QEMU 基线 | `9312ab185493eb5fb8faecccbcf85e9c6a8e4d54` 加本地实验改动；输出版本 11.1.50 |
| QEMU 构建 | `x86_64-softmmu`，TCG；`--disable-docs --disable-werror` |
| Python | 独立目录 CPython 3.12.12；系统原有 3.10 不变 |
| Linux 内核 | Alpine x86_64 `6.12.114-0-lts`，直接内核启动 |
| 根文件系统 | BusyBox static `1:1.30.1-7ubuntu3.1` 加静态用户态测试程序；Python 生成 newc initramfs |
| 来宾资源 | 单 vCPU、512 MiB、Q35、TCG、无显式 NUMA 拆分 |
| Host IOMMU | QEMU Intel IOMMU；来宾 `intel_iommu=on iommu.strict=1` |
| switch | `pcie-root-port → x3130-upstream → xio3130-downstream` |
| EP | 内部 ID `1234:11f0`，64 KiB BAR0，实验协议 v1 |
| simu | 本地源码构建的 Spike 与 firmware；功能模式；业务只用 core0 |

外部工具及内核的来源、版本和固定 SHA256 写入 `stage1/prepare_assets.py`，下载结果另记录在 `experiments/qemu_stage1/assets/sources.json`。内核地址可能随 Alpine 更新；内容发生变化会报哈希不符，需要重新确认版本，不能静默使用新文件。

## 4. 从当前工作区复现

依赖系统具备 GCC（含静态 libc）、g++、make、ninja、Git、pkg-config、QEMU 所需开发库、Python3，以及 `/data/nuclei/gcc/bin/riscv64-unknown-elf-*` 工具链。准备脚本通过 Ubuntu apt 元数据下载固定 BusyBox 包但不安装系统软件；新机器应先配置可获取该包的源。

### 4.1 构建与基线准备

```bash
cd /workspace/GIT/pcie_qemu
bash qemu/zc_simulator/stage1/build.sh
python3 experiments/single_die_packet/run_baselines.py
python3 simulator/riscv-tests/packet_runtime_test/run_mock.py
```

`build.sh` 下载并校验独立 Python、内核及 BusyBox，配置/构建 QEMU 和两个 IOMMU qtest，再构建 Spike 和裸机程序。上述脚本不使用 `rm -r *`，不覆盖原 MoE 示例输出。

基线脚本产生新运行目录；假包脚本生成本地实验记录。当前桥接程序复用假包实验生成的 `firmware.cfg`，Linux 测试复用旧模式基线结果，因此首次使用时必须按上述顺序准备。

如只想查看编译命令：

```bash
cd /workspace/GIT/pcie_qemu/qemu/build
bash ../configure \
  --python=/workspace/GIT/pcie_qemu/experiments/qemu_stage1/python-tool/python/bin/python3 \
  --target-list=x86_64-softmmu --disable-docs --disable-werror
ninja -j32 qemu-system-x86_64 tests/qtest/intel-iommu-test tests/qtest/iommu-intel-inv-test
```

构建配置存在时增量编译即可。实验依赖的完整 MoE SDK 尚未补齐，旧业务程序仍使用复制的 bin；Spike、firmware 和新增 MCU/NPU 常驻程序均由本地源码构建。

### 4.2 一键运行阶段验收

```bash
cd /workspace/GIT/pcie_qemu
python3 qemu/zc_simulator/stage1/run_all.py
```

脚本顺序运行八组检查并保存 `experiments/qemu_stage1/suite-*/results.json`。只有八组全部退出 0 才返回成功；具体命令、耗时及各组输出位于同目录。

### 4.3 单项运行和调试

```bash
cd /workspace/GIT/pcie_qemu
python3 qemu/zc_simulator/stage1/run_linux.py
python3 qemu/zc_simulator/stage1/run_linux.py --topology8
python3 qemu/zc_simulator/stage1/run_linux.py --fault disconnect
python3 qemu/zc_simulator/stage1/run_linux.py --fault wrong-id
python3 qemu/zc_simulator/stage1/run_linux.py --fault timeout
python3 qemu/zc_simulator/stage1/reset_probe.py
```

每次 Linux 运行生成独立目录，包含 `command.json`、`manifest.json`、`results.json`、`linux.log`、`bridge.log`、initramfs 和该次 rootfs；正常联调额外保存 `simu/spike.log`、`simu/trace.json` 和包字节。

`command.json` 是该次完整 QEMU 启动参数。启动时开放该目录内的 QMP Unix socket 供调试，正常退出后移除。测试不使用登录密码、磁盘镜像或网络服务。

## 5. 验收结果

| 检查 | 实际结果 | 说明 |
| --- | --- | --- |
| Linux 启动、PCIe 拓扑 | 通过 | 来宾输出 `STAGE1_LINUX_BOOTED`，EP BDF 为 `0000:03:00.0` |
| Host IOMMU | 通过启动与归组验证 | DMAR 初始化成功，EP 存在 IOMMU group；不是 EP DMA 验收 |
| BAR 映射 | 通过 | Linux 分配 64 KiB，用户态 mmap 后身份寄存器读写正常 |
| 单卡端到端 | 9 笔请求通过 | A/B/C 三次动作均输出 33792 字节，逐字节匹配旧模式 |
| runtime 非法请求 | 5 笔拒绝 | 重复 ID、ID=0、错误长度字段、错误 core、错误指令；不增加执行计数 |
| busy 再提交 | 9 次拒绝 | 原在途包未被覆盖 |
| INTx 状态与 ACK | 通过 | pending 和 PCI status interrupt 位先置位，再随 ACK 清除 |
| 后端断连 | 通过 | EP 返回 transport error=2；Linux 可 ACK 后退出 |
| 错误响应 ID | 通过 | EP 返回 protocol error=3，没有误完成当前命令 |
| 无响应超时 | 通过 | 30 秒实时时钟总超时，EP 返回 error=1 并关闭连接 |
| 在途 system reset | 通过 | 旧 socket 关闭，重新配置 BAR 后状态/IRQ/连接状态均清零 |
| 八端口资源实验 | 通过 | Linux 枚举八个 EP，各 BAR0 已分配 64 KiB |
| Intel IOMMU qtest | 2 项通过 | 8byte-access、stage-1 |
| IOTLB invalidation qtest | 12 项通过 | legacy/scalable，context/IOTLB invalidation 相关用例 |
| checkpatch | 0 errors、1 warning | 仅提醒新增文件的 MAINTAINERS 归属；内部实验不提交上游，也未增加 sign-off |

正常联调的成功标志：

```text
STAGE1_PASS commands=9 tensor=3 rejected=5 busy_rejected=9
GUEST_RESULT=0
```

八端口检查的成功标志为 `STAGE1_PASS topology8 endpoints=8`。此时只有一个 EP 启动真实 simu，其他七个只是资源枚举端点。

用户态测试读取 pending/PCI status 来验证设备中断状态，未实现来宾内核 IRQ handler 或 MSI/MSI-X；不能把这个结果解释为生产驱动中断处理已完成。

## 6. 验收证据索引

本仓库内另保存[验收结果快照](stage1/evidence/summary.json)、[验收摘要](stage1/evidence/验收摘要.log)、[单卡运行哈希清单](stage1/evidence/linux-manifest.json)及[simu 命令 trace](stage1/evidence/simu-trace.json)，便于随代码保存。完整内核日志、构建产物、下载资源和每次实验目录仍位于工作区 `experiments/`；只提交 QEMU 仓库不会自动包含外部 simulator 源码及这些原始产物。

最终统一验收结果及各单项日志见[阶段验收汇总](../../experiments/qemu_stage1/suite-1793005370866225254/results.json)。汇总目录中的 `linux-simu.log`、`topology8.log` 和故障日志记录了每次运行目录；进入对应目录读取完整内核日志和 simulator trace。

补充的首次独立验证记录：

- [单卡 Linux 联调](../../experiments/qemu_stage1/run-1793005288721508614/results.json)。
- [八 EP 枚举与 BAR 分配](../../experiments/qemu_stage1/run-1793005035877886186/results.json)。
- [在途复位](../../experiments/qemu_stage1/reset-1793005035819709470/results.json)。
- [外部资源来源和哈希](../../experiments/qemu_stage1/assets/sources.json)。

首次内核下载未完成时启动曾无串口输出，该次验证失败、日志保留。已改为启动前强制核对内核 SHA256，后续验收均使用完整校验后的内核。失败记录不计入通过结果。

## 7. 第一阶段交付项对应关系

| 原计划交付 | 当前对应产物 | 状态 |
| --- | --- | --- |
| 软件/模型运行基线 | 本地构建、simu 基线、Linux 联调及哈希清单 | 已完成实验验证 |
| 接口对照表 | 实验 BAR、假包、响应及状态语义；真实接口缺口列表 | 实验接口冻结，真实协议待提供 |
| 集成决策 | 每卡 simulator/桥接进程，QEMU 非阻塞 chardev | 当前实验已实现并验证 |
| P2P 风险记录 | requester/IOMMU 源码入口及双卡最小实验设计 | 分析/设计完成，功能在第三阶段实施 |
| 关键风险 | 断连、超时、错误 ID、在途复位、八 EP BAR 资源 | 已验证所列实验场景 |
| 待确认事项 | 正式协议、生产 runtime、HDMA/ATU、barrier、数值容差 | 已列对接角色和关闭条件 |

按照需求方允许假包的实验范围，第一阶段可以结束并进入第二阶段。正式硬件软件一致性验收仍受资料依赖约束，不能标为已实现。

## 8. 进入第二阶段的任务

优先实现 EP 的 Host DMA/IOVA 路径及加载通路：明确数据缓冲/描述符所有权、主机 IOMMU 映射和错误处理，再依据硬件协议接入 HDMA、inbound/outbound ATU。随后替换真实包与 runtime，并补生产驱动中断链路。

当前 EP 没有 ATU、HDMA 和 Host DMA，因此三次矩阵结果经 BAR 响应窗口回传，输入仍由 firmware 预加载。这是阶段一的功能探路，后续必须验证真实 PCIe 数据路径，不能让宿主侧文件复制长期替代它。
