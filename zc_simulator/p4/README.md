# P4 八卡功能平台

P4 复用 P1 会话、P2 两引擎与 P3 switch 路由，扩展为一个 QEMU、一个 RC/switch、八个独立后端。最终统一门禁已通过（`p4_full_exit=true`，40 项摘要一致），八卡专项 1964 项、Linux 专项 15 项及完整 P3/P2/P1 回归通过。详细验收状态见 [执行与验收记录](P4执行与验收记录.md)，覆盖与差异见 [需求覆盖与交付清单](P4需求覆盖与交付清单.md)。本目录用于内部软件功能实验。

任务从工具链到 QEMU、simu 的实际执行流程及三方分工，见 [P0—P4 工具链任务执行流程图](../P0-P4工具链任务执行流程与交互分工.md)。

## 构建与运行

从工作区根目录执行：

```bash
ninja -C qemu/build qemu-system-x86_64
python3 qemu/zc_simulator/p4/run_all.py --skip-build
```

统一门禁按顺序运行八卡原模型专项、Linux 原驱动八卡专项、完整 P3（内含 P2/P1），核对同一轮源码/产物摘要后才设置 `p4_full_exit=true`。生成的 evidence 已 ignore；不要在验收期间修改源码或重建正在运行的 QEMU/Spike。

分别开发可使用：

```bash
python3 qemu/zc_simulator/p4/run_cluster.py
python3 qemu/zc_simulator/p4/run_linux.py
```

常驻启动和配置生成：

```bash
python3 qemu/zc_simulator/p4/launch.py --cards 8 --work /tmp/zc-eight-live
python3 qemu/zc_simulator/p4/launch.py --cards 8 --work /tmp/zc-eight-plan --generate-only
```

`--work` 使用新目录。常驻入口启动 qtest 功能平台，等待八卡 ready、配置 inbound 后交出标准 QMP/qtest socket，保持八个后端，Ctrl-C/TERM 统一回收。它不启动 Linux；Linux 入口为 `run_linux.py`，该入口完成 guest 工作负载后关机。`--cards` 同时支持 1、2、8，默认 8；QMP/qtest 路径、启动命令、后端输入和日志位于工作目录。可以使用 `--duration 1` 做有界常驻启动检查。原 P1/P2/P3 复现入口保留。

## 自动生成的拓扑与资源

`cluster_platform.py` 中 `layout()` 生成计划，`Platform` 负责服务、QEMU、配置、清理、共享 backing 观察及资源采样。

| 项目 | 配置 |
| --- | --- |
| 拓扑 | q35 → root port（bus 1）→ upstream（bus 2）→ 八个 downstream（bus 3…10）→ 八个 EP |
| 后端 | 每卡独立 card ID、socket、service、Spike、DDR0/DDR1/mailbox memfd |
| QEMU BAR | BAR0 4 MiB / 64 位 non-prefetch，BAR2 32 MiB / 64 位 non-prefetch，BAR4 32 GiB / 64 位 prefetch |
| 手工配置 BAR0 | `0xc0000000 + card * 64 MiB` |
| 手工配置 BAR2 | BAR0 + `32 MiB`，按 32 MiB 对齐 |
| 手工配置 BAR4 | `0x1000000000 + card * 32 GiB`，八卡共占 256 GiB |
| 桥窗口 | 每个 downstream 独立的 64 MiB non-prefetch 窗口和 32 GiB prefetch 窗口，父桥聚合覆盖 |
| qtest peer 域 | `[0x1000000000, 0x5000000000)`，64…320 GiB，域外 Host IOVA |
| payload | 各卡 BAR4 + `0x20000000`，128 KiB inbound 映射到本卡 DDR0 `0x20010000` |
| 共享 backing | 每卡 DDR0 1 GiB、DDR1 256 GiB，稀疏 memfd；并非每卡常驻占用 257 GiB RAM |

Linux 由 BIOS/内核自动分配资源，使用 512 GiB PCI 64 位 hole，peer 域改为 `[4 GiB, 1 TiB)`；guest 必须检查实际 BAR4 完整落在该域、coherent Host IOVA 位于 4 GiB 以下。实际地址打印为 `P4_BAR` 并逐一检查大小及不重叠，不能用手工计划替代实际分配结果。

Linux 使用一个 Host socket、四个逻辑核、一个 RC 和一个 IOMMU。原驱动每卡申请 32 个 MSI-X 向量；首轮单逻辑核无法容纳八卡向量，保留原驱动行为后改用四核验证，不把资源耗尽误当成设备模型故障。

## 实际创建与启用核数

既有 `--runtime-func` 根据 `SM_RUNTIME_HARTID_DEFAULT` 每后端创建 7 个 processor：`0,1,2,16,17,18,19`。八后端共创建 56 个 processor 对象，其中 firmware hart 0 每卡一个，模型提供两个 MCU hart、四个 NPU hart 的容量。

本次 firmware 配置 `pc_num:2`，仅启动 MCU hart 1、NPU hart 16 的 runtime：八卡共启用 8 个 MCU、8 个 NPU；另有 8 个 firmware hart 负责已有引导/调度，32 个未选 runtime hart 不计作已启用算力。ready 和任务均由这两个指定 runtime hart 实际执行，不能将“创建 56 个对象”写成“56 核业务任务通过”。

## 任务与并发流程

1. 启动八个常驻 service；QEMU 为每卡分配独立共享 DDR/mailbox，通过原会话和 SCM_RIGHTS 绑定对应 Spike。
2. 原 firmware/MCU/NPU 初始化并发布 ready；配置完整三组 BAR、桥和 inbound/outbound。
3. Host 或测试端准备每卡独立数据，写入源卡原 P2 集成任务区。四种模式分别为 NPU/NPU、HDMA/HDMA、HDMA/NPU、NPU/HDMA。
4. 按 `target=(source+shift)%8`、shift=1…7 分七轮，每轮八卡同时提交，覆盖 56 个有向卡对。每卡仍只有一个运行中的集成任务，不宣称单卡七队列同时执行。
5. 同一 IOVA 在八个真实 VT-d requester/domain 中分别映射到不同 GPA；peer 请求直接访问目标共享 DDR，绕过 Host IOMMU 和 Host staging。
6. 八个 NPU peer 任务与八个 HDMA Host 请求并存，完成数据检查后再发下一轮。原 Tensor 也在八个后端中按四组合执行并逐字节比对既有 standalone 参考。
7. 任务完成后进程继续常驻；仅 reset、runtime 崩溃、超时或服务断连才回收和替换故障卡。资源回收保持 reap-before-reuse。

## P4 在途隔离

新增 endpoint 参数 `p4-isolation=on`，要求同时启用 P3。P3 默认仍保留原全 fabric 取消策略，以保持历史复现行为。

P4 下普通 HDMA 在 doorbell 时保存已转换的远端地址；NPU 任务固定源 outbound 可达的 peer 集合。签名只纳入源及相关目标的会话、epoch、ready、mapping version、BAR decode 代次和路径桥配置。MemoryListener 的 region add/del 把实际 BAR decode 变化归属到对应卡，短暂移动再恢复也不能让旧请求复活。无关卡断连或重配不再取消这些普通在途请求。

关联目标发生故障时，旧请求明确失败，不自动重放已完成前缀。linked descriptor chain 可能跨多个动态目标，仍保留全 fabric 保守取消；本次 56 对和八卡并发任务使用既有 runtime 的普通通道模式，未宣称链表多目标的最小影响集隔离。

P4 不更改 Spike 模型、service 协议、mailbox/FD 格式或已对齐的本地 runtime。NPU 任务关联继续使用原 P2 集成任务编号，不把该 fixture 协议声明为生产多队列任务 ABI。

## 证据与成本

每轮输出 `platform.json`、backend 配置/摘要、QEMU 命令、配置空间、三次重复输入摘要、覆盖矩阵、八份服务日志、实际 DMA/路由 trace、结果和 `resources.json`。

资源记录包括墙钟耗时、已回收子进程 CPU 时间、进程 RSS/VmSize/线程点采样和最大单子进程 RSS。共享映射会重复记入不同进程 RSS，不能把简单相加当成独占物理内存；这些数值只用于宿主容量预算，不是 PCIe 带宽、时延或周期精度。
