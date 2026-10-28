> 2026-10-28 P2扩展：已补 MCU 本地中断软件接线、NPU 可恢复交易错误、32 region inbound Memory 子集与 MCU 文件 IPC 消费者，并扩展故障重试矩阵。物理 IRQ 编号、硬件通道错误 ABI、生产 Tensor 文件解析及全部竞争矩阵仍单列，详细状态见 [P2扩展实现说明](p2/P2扩展实现说明.md)。

# 内部 PCIe 互联平台分阶段实现方案

> 2026-10-28 P2首轮实现：已新增function绑定的Host访问服务、HDMA子集、MCU寄存器代理、原NPU外部DMA及四组合/Linux驱动联合测试。完整P2尚未关闭，后续按本地IRQ、可恢复NPU错误、inbound/文件IPC及扩展矩阵顺序推进。代码入口与同批证据见 [P2执行与验收](p2/P2执行与验收记录.md)。

> 最新实现：三个BAR已对齐为64位，大小4MiB/32MiB/32GiB，前两个non-prefetchable、最后一个prefetchable。当前代码与旧实验profile不同；最新运行配置、P2准备项和证据见 [BAR对齐与P2准备](p1/P2开发前准备与BAR对齐验收.md)。下文旧大小及“尚未修改”描述仅保留为历史。

> 2026-10-28 BAR补充确认：用户提供硬件为三个64位BAR，BAR0=4MiB/non-prefetchable、BAR2=32MiB/non-prefetchable、BAR4=32GiB/prefetchable。当前代码仍是原P1实验布局，硬件profile尚未修改/复验；后续调整与历史启动panic说明见 [BAR属性确认](p1/BAR硬件属性确认与接入调整.md)。

> 2026-10-28 最新状态：P1会话服务、SCM_RIGHTS、配置/身份校验、超时与断连/reset恢复已实现；完整P1按单卡软件功能profile v1通过统一出口。详见 [P1闭环验收记录](p1/P1闭环验收记录.md)。下文P1-A“尚未关闭”及待实施描述保留为历史记录，以最新闭环记录为准。

> 2026-10-27 实施进度：P0 基线/设计已完成，P1-A 单卡共享内存接入已运行通过：Host → BAR/mailbox → MCU/NPU 原 Tensor → 共享结果，并验证 Host demo 驱动/应用发现、coherent mmap、MSI-X 和空闲 reset。完整 P1 仍需 P1-B 会话协议及通用恢复服务；P2 外部 DMA 未实现。详见 [P1 运行与验收](p1/P1执行与验收记录.md)，其中第 5 节列出剩余开发顺序。

> 2026-10-27 Host demo 增量：已收到 `simulator/host-demo`（Linux PCI驱动和应用）。P0基线/设计完成结论保留，Host侧参考更新为该demo；新增设备ID `16c3:abcd`、2MiB coherent DMA、MSI-X、BAR2/BAR4文件传输契约。旧文中“无独立Host驱动/仅stage1测试端”描述已由此补齐；demo尚未在来宾联合验收。接口细节、源码问题及P1/P2调整见 [Host-demo接入影响评估](p0/Host-demo接入影响评估.md)。

更新日期：2026-10-27。用途：内部联合仿真开发、任务拆分和验收。本文及后续 AI 辅助实验产物不用于 QEMU 上游贡献，遵循 [QEMU 内容来源政策](../docs/devel/code-provenance.rst)。

当前代码准入结论与本轮重跑证据见 [P0 阶段准入评估与 P 阶段开发方案](P0阶段准入评估与P阶段开发方案.md)：runtime 已完成硬件对齐；P0 可复现基线与适配设计已收敛完成，进入 P1 PCIe 接入。现有 Zebu `pcie-demo`/寄存器手册是 PCIe 参考，复用 stage1 来宾环境及接入成果，不等待额外 runtime 镜像或独立 Host 驱动。

## 1. 项目目的与依据

构建一个 Host/Root Complex、一个 PCIe switch、八个 NPU PCIe device 的功能仿真平台，让硬件平台对应的 Host Linux 驱动与现有 MCU runtime/NPU 任务在软件环境中联合运行，验证任务下发、数据搬运、地址隔离、跨卡访问、通知、错误和复位。平台用于驱动及设备软件联调，提供后续协议与性能模型的功能基线。

当前范围与架构以 [PCIE Switch 集群功能仿真器](PCIE%20Switch集群功能仿真器.md) 为准；[EMU demo 可行性分析](EMU参考demo转软件仿真可行性分析.md)补充已有源码/手册的利用方式和静态疑点。[群内对话技术总结](群内对话技术总结.md)及[工具链组需求清单](工具链组需求清单pci-switch-8card-simulator-requirements.md)保留需求来源，冲突处服从新架构。寄存器与协议的具体行为仍以锁定版本的真实软件和硬件资料为准。

### 1.1 本次明确的范围调整

| 旧方案中的要求或候选项 | 当前执行口径 |
| --- | --- |
| 首版每 die 四个 NPU 全部启用、八卡共 32 核 | 首期每卡一个 MCU、一个 NPU；明确实际创建和启用核数，保留已有单 die 模型及本地功能回归 |
| 进程组织、socket、共享内存后续决策 | 一个 QEMU、八个 endpoint、八个独立后端；Unix socket 控制/外部请求 + 每卡共享 DDR |
| HDMA/转换模型位置待定 | QEMU endpoint 持有 PCIe/HDMA/地址转换权威状态，后端代理 MCU 访问 |
| 先做 Host 下载固件/完整启动 | 沿用 firmware 配置和 dispatcher，固定入口实际初始化并发布 ready；真实 ROM、Host 下载固件、完整冷启动链不属首期 |
| 等待或新建生产 runtime、工具链包解析/单动作接口 | 单 die runtime/BSP/内部驱动/任务机制作为已对齐基线复用，不新增业务 ABI 或重写内部驱动 |
| HDMA 完成后再开展 NPU 外部 DMA | 两引擎共用 PCIe 访问服务，各自独立提交/完成；Host 分支可分别开发 |

新架构声明单 die 软件可复用，不等于真实 Host 驱动与 QEMU 联合仿真已经验收。第一阶段假包实验的结果及限制保持原记录，不追改为新架构已实现。

## 2. 首期交付边界

| 项目 | 首期要求与边界 |
| --- | --- |
| Host | 优先匹配实际 CPU 架构、Linux 和驱动环境；x86_64 Q35/TCG + intel-iommu 可用于初期验证，KVM 非门槛 |
| 拓扑与进程 | 一个 RC、一个 switch、八下行端口/EP；每卡一 die、独立 simulator 后端 |
| 运行软件 | 原硬件 Host 驱动；每卡一个 MCU 运行常驻 runtime、一个 NPU 运行常驻任务服务；任务结束不退出后端 |
| 任务协议 | 沿用既有 mailbox/通知/队列、参数和完成协议；工具链生成的任务按原接口提交，不自建 160 字节正式 ABI |
| DMA | Synopsys HDMA 与 NPU 指令 DMA；独立执行，共用访问服务，覆盖 Host、peer 和混合输入输出 |
| 地址 | 本 die、本卡其他 die、PCIe outbound 和非法地址明确分流；设备侧转换与 Host IOMMU 分级；P2P 不过 Host IOMMU，不经 staging |
| 内存 | 每卡对外 DDR 一份共享数据；容量可配置；L1、MCU SRAM 默认沿用本地模型，可达范围按硬件 |
| 通知 | 内部 mailbox/本地中断与 Host MSI/MSI-X 按硬件分别维护；pending/mask/ack 和完成可见性正确 |
| 错误与恢复 | 原请求收到地址/权限/IOMMU/描述符/断连错误；按通道、function、核、整卡区分复位范围 |
| 不包含 | CCL、collective、通信算法、完整算子库实现；真实 ROM/Host 下载固件/完整冷启动；完整 TLP、credit 与性能精度 |

已有通信软件可以作为外部工作负载运行；首期平台验收不以新开发通信库为前提。完整 ROM 加载、多 die 扩展、协议与性能均为独立后续工作。

## 3. 架构、状态归属与运行契约

### 3.1 联合架构

```text
一个 QEMU 进程
 ├─ Host CPU / Linux / 原硬件驱动 / 测试程序
 ├─ Root Complex / Host IOMMU / Host RAM
 └─ Root Port → Switch upstream → 八个 downstream
     └─ Endpoint i（i=0..7）
         ├─ 配置空间 / BAR / MSI或MSI-X
         ├─ Synopsys HDMA 功能状态机 / inbound-outbound转换
         ├─ 统一 PCIe 访问服务 / P2P 功能路由
         └─ 桥接入口
             ↕ Unix socket：控制、寄存器、通知、外部访问及结果
             ↕ 每卡共享 DDR：memfd/MAP_SHARED 等
           独立 NPU simulator 进程 i
             ├─ 适配桥接 / MCU 的 PCIe MMIO 代理
             ├─ MCU：现有 runtime
             ├─ NPU：现有常驻任务服务、指令 DMA
             └─ 内部 mailbox / 本地中断 / L1与MCU SRAM等
```

QEMU root port、xio3130 upstream/downstream 组合表达一个 switch 的功能拓扑；在锁定版本上用 guest `lspci` 和 monitor 验证端口、BAR、桥窗口。它不自动提供目标 TLP/credit/性能模型。

每卡独立进程隔离既有模型全局状态。PCIe BDF、平台 card ID、后端 chip/die/core ID 分开配置，不把 demo 的四 chip IPC 编号当八卡编号。

### 3.2 唯一状态所有者

| 状态/模块 | 权威持有者 | 其他访问者如何接入 |
| --- | --- | --- |
| PCIe 配置、BAR、MSI/MSI-X、HDMA 通道与地址转换 | QEMU 对应 endpoint | MCU 内部 MMIO 经后端代理；Host BAR 若映射同一控制器，访问同一状态 |
| MCU/NPU 执行、内部 mailbox、本地中断 | 对应后端 | Host BAR 经适配访问相同 mailbox，通知通过桥接投递 |
| 对外设备 DDR | 每卡一份共享 backing | Host BAR、HDMA、NPU DMA、P2P 在合法范围内访问同一数据 |
| L1、MCU SRAM及其他本地状态 | 原单 die 模型 | 按硬件可达范围提供访问；不默认全部共享或暴露给 PCIe |
| Host DMA 映射与 IOMMU | guest 驱动/QEMU IOMMU | 使用源 endpoint 的 DMA AddressSpace，不自建脱离 guest 的页表 |
| 请求、会话、代次 | 双侧适配按契约维护 | 连接 request ID 关联原任务、DMA 引擎和请求；旧代次不能改变新状态 |

### 3.3 固定入口启动与连续任务

```text
建立连接/共享 DDR → 现有 firmware 配置加载 MCU/NPU 镜像
 → dispatcher 从固定入口启动
 → MCU/NPU 实际执行初始化、发布 ready
 → Host 通过既有 mailbox 下任务 → MCU runtime → NPU 任务服务
 → NPU/DMA 真正完成 → MCU 发布完成/触发 Host MSI或MSI-X
 → MCU/NPU 回到等待循环，继续处理下一任务
```

ready 必须由核上软件发布，不能由 QEMU 在连接建立时伪造。配置加载的是既有软件和常驻任务服务，不通过“每包重启后端或加载整个业务程序”替代运行期提交。任务粒度、描述符生成和程序执行方式沿用已有协议，仿真侧不重新设计编译器/runtime 动作接口。

Host mailbox 传任务参数或描述符地址，大块数据走 DMA。NPU→MCU、HDMA→MCU 是内部通知，Device→Host 使用硬件要求的 MSI/MSI-X；数据落地且满足可见性后，才发布完成和发中断。搬运成功不表示目标 runtime 已消费，消费同步使用已有硬件通知或 Host 协调。

### 3.4 桥接与一致性

内部连接协议覆盖版本/能力、设备标识、启动/ready/reset/退出、寄存器访问、mailbox/中断、NPU 外部读写/读响应/错误，以及请求 ID、会话、复位代次、地址域、长度与状态。保留原业务任务协议；内部标签用于检查与 trace，不要求原驱动发送硬件不存在的字段。

QEMU MMIO 回调不得阻塞等待 socket/FIFO。QEMU 状态由事件上下文更新；后端等待访问响应时仍能处理消息和中断。Host BAR 对后端 mailbox 的同步读语义须有一致访问视图及明确 side effect，不得用未同步缓存或阻塞 RPC 冒充寄存器访问。P1 必须评审这条路径，验证双向等待不死锁。

共享 DDR 从首期采用，不等待复制开销成为瓶颈。定义映射范围、容量、句柄归属及关闭/重建时序；同步保证“数据/描述符先发布、再通知，结果先可见、再完成”。跨 DMA 引擎的依赖由既有软件显式同步，避免 QEMU/后端重复执行外部复制。

## 4. 两类 DMA 与地址路径

### 4.1 引擎职责

HDMA 位于 QEMU endpoint，按目标 IP/综合配置/真实驱动实现初始化、描述符获取、通道执行、停止、完成、错误、本地中断与复位。首个真实驱动闭环支持其实际描述符模式、地址宽度和通道配置；`pci_dma_read/write` 只承担底层访问，不替代 HDMA 状态机。

NPU DMA 保留原指令和描述符，在实际访存入口增加外部适配。命中 PCIe outbound 窗口才产生外部请求，本地请求走原模型；读数据/写完成/错误返回原引擎，贯通 busy、依赖/fence 和完成路径。不启动 HDMA 通道也必须能完成 Host/P2P 访问，两引擎属于同一 function 时采用同一 requester。

### 4.2 地址域与转换

| 地址域 | 含义及约束 |
| --- | --- |
| DEVICE_LOCAL / 本卡内部地址 | 本 die 或本卡其他 die 的合法地址；卡内走现有路由，跨卡统一走 PCIe |
| DEVICE_OUTBOUND | 指令/描述符可表达的设备出口窗口地址，经 outbound/ATE 转换 |
| HOST_IOVA | guest DMA API 为源设备建立的映射地址 |
| HOST_GPA | IOMMU 输出的 guest 物理地址，不是宿主指针 |
| PEER_BAR | 目标卡当前 PCIe BAR 总线地址及偏移，随分配/重定位更新 |

ATE 泛指设备侧转换表，实际名称与布局以 SoC/IP 为准。512 GiB 地址槽的解码及指令/描述符位宽需核实，不把本地未命中自动当 PCIe。重叠窗口的优先级、权限、边界与未命中结果必须冻结。

1. Host CPU MMIO → BAR decode → inbound/window → 设备寄存器/DDR。
2. NPU DMA 设备出口地址 → outbound/ATE → Host IOVA → switch/RC → 源 endpoint DMA AddressSpace → Host IOMMU 权限及转换 → Host RAM。
3. NPU DMA 设备出口地址 → outbound/ATE → peer 当前 BAR → switch P2P 功能路由 → 目标 BAR/inbound → 目标 DDR。读结果回源引擎指定的本地目标。
4. HDMA 按描述符地址域及集成接入位置决定是否经过设备侧转换，再进入 Host/P2P 公共服务；最终 PCIe 地址不重复转换。ATU_BYPASS 只绕过设备 iATU，不绕过 Host IOMMU。

### 4.3 P2P 与 IOMMU 并存

`pci_dma_*` 使用源设备 DMA 地址空间，不能假设传入 peer BAR 就自动实现 IOMMU 旁路。实现受限 P2P 服务，按当前拓扑、BAR 和桥窗口定位目标，检查源 Bus Master、目标 Memory Space、P2P enable、权限、边界和设备状态。

Host 分支经过 IOMMU；peer 分支不经过 Host IOMMU，也不经过 Host staging，禁用时返回错误，不静默改走 staging。BAR 重定位/禁用、窗口和转换表变化后重新校验；非法 peer 地址不得落入任意 Host GPA。

同时开启 IOMMU，验证非恒等 IOVA、跨页、domain 隔离、未映射、权限及映射撤销；无 peer IOVA 映射时合法 P2P 仍成功。撤销前在途请求按硬件契约处理，撤销及必要失效同步后的新请求必须失败。

### 4.4 错误与复位

地址错误、IOMMU fault、非法描述符、P2P 禁用、runtime 异常、断连和超时返回原请求，不能被吞掉或变为成功。区分 channel/function/核/整卡 reset：停止接收相关新请求，取消或排空旧访问，确认旧请求不再修改内存，再重置状态并重新初始化。

晚到响应按会话/代次丢弃，但这不替代停止旧写入；共享句柄按同一生命周期管理。失败的部分进度只报告可确认完成的字节，内部诊断仅在硬件定义了接口时暴露给驱动。

## 5. 分阶段实施与验收

阶段编号保留原第一至第四阶段，分别对应当前 P0—P4 的工作安排；第五/六阶段为后续扩展。它们是开发计划，完成状态以运行证据为准。

### 第一阶段 / P0：接口和软件基线冻结

历史记录：2026-10-26 假包、最小 MCU runtime、实验 BAR/INTx 的技术验证已完成，见[第一阶段验收报告](第一阶段QEMU接入复现与验收报告.md)。该实验未实现真实 ATU/HDMA/Host DMA；不能把它作为最新架构的软件兼容验收。

P0 工作已按当前范围收敛，以下为已执行的基线与设计工作；PCIe 细节落实随 P1/P2 实现继续：

1. 固定 QEMU commit、machine、guest kernel、工具链、stage1 已验证的 Q35/Linux/用户态测试环境及 simulator 版本。
2. 复用已有 runtime/BSP/内部驱动、现有 firmware 配置、dispatcher 入口和任务测试夹具，保存本地启动/指令/DMA/mailbox 回归。
3. 核对目标 Synopsys IP 及综合配置、BAR 大小/属性、内部地址地图、HDMA 描述符和出口路径、mailbox/MSI/MSI-X 控制、复位范围。
4. 固定进程映射、状态归属、共享 DDR、外部请求协议、同步/超时/恢复与 trace 契约。
5. 将 EMU 片段中的疑点与实际使用版本核对；BootROM/FW IPC 的非首期问题单列，不阻塞运行期建模。

交付：版本/接口对照表、既有单 die 运行证据、任务与结果样本、地址和状态归属设计、差异待办。通过条件已满足：基线可构建/可启动、现有任务可复现、关键适配位置和设计足以开发。simu 内部 runtime 按已对齐现状复用；不要求另交镜像。完整 PCIe 寄存器兼容性在 P1/P2 验收。

### 第二阶段 / P1—P2：单卡真实软件联合闭环

**P1 基础连接与常驻任务：**

1. 实现一个 EP、真实配置/BAR/Bus Master/所需 MSI或MSI-X，连接一个独立后端。
2. 接入每卡共享 DDR，保留本地内存；实现 MCU MMIO 代理、mailbox BAR 访问与两向通知，验证 QEMU 不阻塞及访问一致性。
3. 沿用固定入口启动一个 MCU/NPU，软件实际发布 ready；现有来宾测试端下发任务；后续获得独立 Host 驱动再补兼容验证，MCU/NPU 结束后继续服务。
4. 实现基础断连/超时/会话/复位流程，回归本地 NPU 指令、DMA 和内部交互。

P1 交付与门槛：现有来宾测试端枚举/访问设备、按 Zebu 参考/手册接入 BAR/mailbox、ready、中断及连续两次以上任务通过；共享数据无副本串扰。启动加载沿用既有方式，无需 ROM/Host 下载固件。

**P2 访问服务和两类 DMA：**

1. 建立公共 PCIe Host 访问服务，保留源 function requester，使用 guest DMA API 的 IOVA 与 QEMU IOMMU。
2. P2-H：实现 endpoint HDMA 通道/描述符/停止/错误/本地通知，支持真实驱动使用的模式与地址宽度。
3. P2-N：在 NPU 原 DMA 访存处接外部窗口转换/请求/结果，保留 busy、fence 和失败处理；不启动 HDMA 即可独立 Host 双向访问。
4. 分别完成两引擎 IOVA、跨页、权限、撤销和复位回归；再验证四种输入输出组合：HDMA/HDMA、NPU DMA/NPU DMA、HDMA/NPU DMA、NPU DMA/HDMA。
5. 校验实际 NPU 任务结果、完成状态与 Host MSI/MSI-X，确认数据可见先于完成通知。

P2-H/P2-N 共用基础设施后可分别开发，无“HDMA 先完成”的强制依赖。交付：单卡实现、构建/启动配置、真实驱动联合回归、引擎及请求 trace、差异清单。通过条件：上述正常/故障用例均通过；只跑 CPU BAR 或 16 字节 demo 不等于真实 DMA 闭环完成。

### 第三阶段 / P3：双卡 P2P 与隔离

1. 增加第二 endpoint/后端和共享 DDR，接软件下发的动态 peer BAR 与转换表。
2. 实现 switch 功能路由及目标 inbound，检查拓扑、窗口、解码、权限和设备状态。
3. 按两引擎实际能力分别验证 A→B、B→A、read/write、双向并发、读响应和边界。
4. 同时发 Host IOVA 与 peer 请求，证明 Host IOMMU 与 P2P 无 staging 路径并存。
5. 覆盖 P2P 禁用、BAR 重定位/禁用、窗口越界/重配置、源/目标 reset 与断连，防止旧请求继续写目标。

交付：双卡启动配置、路由/地址设计、混合访问与恢复回归。通过条件：数据、源/目标/引擎/代次归属正确，Host fault 可追溯，P2P trace 无 Host IOMMU/staging，失败明确且无重复完成/永久等待。

### 第四阶段 / P4：八卡首期功能交付

1. 自动生成八端口拓扑、BAR/桥窗口、BDF、后端 ID、共享内存和 socket；按真实 BAR 尺寸重新验资源分配。
2. 一个 QEMU 启动八个独立后端，每卡一个 MCU、一个 NPU，发布各卡 ready；清单记录创建/启用核数及卡内存容量。
3. 两类 DMA 按实际支持能力分别覆盖全部 56 个有向卡对、双向和八卡并发 Host DMA/P2P、通用 all-to-all 搬运；不要求实现 collective 算法。
4. 覆盖 domain 隔离、多请求归属、通道/设备 reset、runtime 异常、断连和恢复，检查其他卡不受无关状态污染。
5. 运行原 Host 驱动和既有任务的联合回归；外部通信库仅在已有软件及约定场景具备时接入，覆盖状态单独记录。
6. 固定构建/启动/回归入口，归档 trace、版本、种子、结果；测宿主内存/CPU/运行耗时作为容量成本。

交付：完整 QEMU 源码基线/补丁、后端适配、驱动构建配置、单/双/八卡脚本、共用测试程序、trace、需求覆盖报告、已知差异。通过条件：第 6 节首期矩阵通过，实际驱动/任务已验收；某软件场景未验收必须明示，不用流量发生器代替兼容性结论。

### 第五阶段：单卡多 die 与可选启动扩展

多 die 沿用卡内模型，先单卡扩 die/核数和内部路由，再扩多卡；跨卡始终走 PCIe。原八卡六十四 die/四核配置作为可选容量目标，实际 die/核数、软件场景、资源预算和运行时间另行冻结，不能据此改回首期 32 核门槛。

真实 ROM、Host 下载固件和完整冷启动链作为独立可选工作包，依据已确认引导协议接入并验证；不与多 die 互为依赖。需要时再处理 EMU FW IPC 和加载相关疑点。

### 第六阶段：PCIe 事务与性能模型

基于统一 PCIe 访问服务，依次扩展 TLP 分片、tag/read completion、MPS/MRRS、协议 ordering、credit/buffer/backpressure、仲裁和共享链路带宽/延迟，再校准错误及性能指标。posted write 不虚构 PCIe Completion，DMA 完成与协议 completion 区分。

通过条件：原功能回归保持通过，credit 守恒、恢复不死锁，结果可重复；时延/带宽与约定参考及容差一致。首期事件时间、socket 吞吐和 QEMU 墙钟耗时不作为真实 PCIe 性能。

## 6. 首期验收矩阵

| 用例 | 阶段 | 必需证据 |
| --- | --- | --- |
| 单/双/八卡配置、枚举、BAR/桥窗口、原驱动 probe | P1/P3/P4 | 拓扑、配置 dump、资源和软件版本 |
| 固定入口初始化、ready、mailbox、Host MSI/MSI-X、连续任务 | P1/P4 | 核上日志、任务/通知/ack trace、后端持续运行 |
| 原指令、本地 DMA、mailbox和核间交互 | P0—P4 | 既有单 die 支持配置的回归；首期联合配置每卡启用一个 NPU |
| HDMA Host 双向、真实描述符/通道/停止/错误 | P2-H | 全量数据、通道状态、STOP/ABORT、MCU 本地通知 |
| NPU DMA 独立 Host 双向，不启动 HDMA | P2-N | 原指令/描述符、外部请求、busy/fence、读数据和错误归属 |
| 非恒等 IOVA、domain 隔离、跨页、未映射、权限及撤销 | P2/P3/P4 | 源 requester、转换、真实 IOMMU fault 日志及请求结果 |
| 四种 DMA 输入输出组合、完成可见性 | P2/P4 | 实际任务结果，数据→状态→中断顺序及跨引擎同步 |
| 两类引擎 P2P read/write、双向、56 有向卡对 | P3/P4 | 按能力分别列覆盖，源/目标/路由/数据，无 Host staging |
| P2P 禁用、BAR 移动/禁用、窗口越界、转换重配 | P3/P4 | 明确失败、无 Host 误访问、无旧映射误投递 |
| 并发、通道/function/核/全卡 reset、runtime异常、断连/恢复 | P1—P4 | 无重复完成、无旧写入污染，恢复后可再执行任务 |
| 原驱动与既有任务集成、固定输入重复运行 | P4 | 模型测试/后端回归/guest 集成三层证据；建议至少连续三次正确性通过 |

长度、对齐、跨边界和并发数量按目标 DMA/IP 实际能力及错误定义设置。纯实验假包、INTx pending 轮询、无 DMA 的 IOMMU 归组或八个空 EP 枚举，均不能替代对应真实接口验收。

Trace 关联 run/task/DMA/request/session ID，记录源 card/die/core/引擎、目标、转换前后地址、长度、路径、状态、错误、映射版本和复位代次。区分本地、Host IOMMU、P2P、Host staging；部分进度只记录可证实字节，首期时间戳用于诊断。

## 7. 项目开发步骤总结

下表是实际执行清单；详细范围和门槛见第 5、6 节。先单卡验证共有基础，再做两类 DMA，最后扩卡。

| 顺序/工作包 | 主要修改与产物 | 前置依赖 | 完成标志 |
| --- | --- | --- | --- |
| 1 / P0 基线定位（已完成） | QEMU/来宾测试/后端版本、现有入口/任务及单 die 回归清单 | 既有已对齐软件 | 原任务可复现，版本可追溯 |
| 2 / P0 接口与设计收敛（已完成） | 来源/适配点/状态归属/内存与事件设计 | 现有 Zebu demo、手册、simu 与 stage1 | 可进入 P1；PCIe 未定义字段在对应实现中记录配置与边界 |
| 3 / P1a 共享与桥接 | memfd共享 DDR、socket消息、寄存器代理、会话/代次/事件处理 | 1、2 | 两侧同数据、消息可推进、同步读及通知不死锁 |
| 4 / P1b EP与常驻任务 | 配置/BAR/MSI或MSI-X、mailbox、固定入口启动、ready、原任务 | 3 | 来宾测试端可访问，连续任务，后端不退出 |
| 5 / P2 公共访问服务 | requester/IOMMU/地址分流/外部响应/错误归属 | 3、4及已冻结转换契约 | 非恒等 IOVA和 fault/隔离测试可用 |
| 6H / P2-H HDMA | QEMU通道/描述符/搬运/停止/中断/reset | 5 | 原驱动实际模式及双向/故障通过 |
| 6N / P2-N NPU外部DMA | 后端实际访存入口、窗口转换、busy/fence/结果回传 | 5；不依赖6H | 不启动 HDMA 的 Host 双向及失败通过 |
| 7 / P2-M 任务组合 | 四种输入输出组合、完成状态/MSI顺序、单卡恢复 | 6H、6N | 数据与完成正确，旧请求不污染 |
| 8 / P3 双卡 | peer路由、目标inbound、动态BAR、响应与故障 | 7 | 双向读写，两引擎能力覆盖，IOMMU/P2P并存 |
| 9 / P4 八卡 | 自动配置/启动、八后端、56卡对、并发隔离/恢复 | 8 | 首期矩阵通过、原驱动任务验收 |
| 10 / P4 交付归档 | 构建/启动/回归脚本、trace、覆盖报告、差异与资源成本 | 9 | 按清单可重复复现，未验收项明确 |

```mermaid
flowchart LR
    P0[P0 基线与接口] --> P1[P1 共享DDR/桥接/常驻任务]
    P1 --> S[P2 公共PCIe访问服务]
    S --> H[P2-H HDMA Host]
    S --> N[P2-N NPU DMA Host]
    H --> M[P2-M 混合任务与恢复]
    N --> M
    M --> P3[P3 双卡P2P]
    P3 --> P4[P4 八卡验收与交付]
```

一人投入时按上述工作包逐项落实，6H/6N 是独立分支，按依赖与调试便利选择先后，不同时铺开全部功能。任务拆分由代码职责决定，不要求新增 runtime、CCL 或算子开发。暂不据此承诺日历工期；先记录每包实际耗时及外部等待，再估算后续工作。

## 8. 团队分工与最小增量输入

仿真团队负责 QEMU EP/HDMA/转换/公共访问/P2P、双侧桥接、共享 DDR、NPU 外部 DMA 适配与联合回归。runtime 团队确认现有 mailbox 字段和完成路径并提供可复用任务；NPU 外部地址通过既有参数传入，不另写内部 DMA 驱动。

增量输入收敛为三组，已有代码中已包含的内容不重复交付：

| 输入组 | 最小内容 | 关闭条件 |
| --- | --- | --- |
| Host/PCIe 接口 | Host平台/真实驱动构建、IP版本和综合配置、BAR/内部地址、HDMA出口、中断/reset | 锁定版本接口表和配置 dump，可以实现驱动实际访问 |
| 单 die 软件与启动 | 已有runtime/BSP/镜像/入口/配置定位，mailbox任务/完成路径 | 固定入口启动与现有任务运行证据，平台差异集中在适配层 |
| 联合验证样例 | 既有任务输入/输出/参数、DMA合法能力/错误预期 | 模型/后端/guest三层用例及参考结果可重复 |

启动平台差异不应改变运行期硬件 ABI。测试驱动可辅助早期调试，最终仍以硬件平台对应驱动和既有任务验证兼容性。

## 9. 关键风险与关闭条件

| 风险 | 处理及关闭条件 |
| --- | --- |
| 架构的软件基线未定位或本地依赖缺失 | P0保存真实版本和运行证据；只补定位出的差异，不默认重写runtime |
| 将BootROM demo布局当正式任务协议 | 以运行期驱动/硬件契约冻结；FW IPC与生产mailbox分开核对 |
| QEMU/后端双持HDMA/转换/mailbox状态 | 明确唯一所有者，代理访问同一状态；同步读视图和side effect在P1验证 |
| 共享DDR与原本地内存各存一份 | 接入唯一共享backing，保留本地范围；回归所有访问者的数据可见性 |
| NPU外部DMA返回过早或失败仍完成 | busy/fence/错误贯通原引擎，不用HDMA冒充；无HDMA独立测试 |
| IOMMU与P2P分类错误/ATU重复转换 | 按窗口、描述符和请求上下文分类，转换trace及混合访问验证 |
| BAR/桥窗口不足、位宽不能表达出口地址 | 按真实大BAR重测八卡分配，核对指令/描述符位宽及512GiB槽位解码 |
| IPC互等或无界轮询 | 消息泵持续推进、有界等待、故障回传，QEMU MMIO不得阻塞 |
| reset仅丢响应而旧DMA继续写 | 停止新请求、排空/取消并确认无旧写，再重置/换代/释放共享句柄 |
| 两类DMA共享数据缺少依赖 | 由现有软件显式同步，四种输入输出组合验证通知顺序 |
| 八后端容量不足/全局状态串扰 | 单卡及八卡测资源、每卡独立进程，配置容量和启用核数 |
| 性能指标混入功能验收 | 宿主成本、功能事件与硬件时延分开报告，性能独立校准 |

## 10. 代码入口与历史证据

| 方向 | 入口及用途 |
| --- | --- |
| 历史单 die/假包实验 | [实施指南](单die信息包执行闭环实施指南.md)、[首轮执行报告](单die信息包闭环首轮执行报告.md)：保留历史参考，不作为重新设计runtime的当前任务 |
| 第一阶段 QEMU 实验 | [验收报告](第一阶段QEMU接入复现与验收报告.md)、[接口决策](第一阶段接口决策与待办清单.md)、[实验EP](../hw/misc/zc-pcie-sim.c)、[桥接](stage1/simu_bridge.py)：保留独立回归及错误/非阻塞处理经验 |
| EMU与寄存器参考 | [可行性分析](EMU参考demo转软件仿真可行性分析.md)、[demo](../../simulator/pcie-demo)、[手册](../../simulator/寄存器手册)：运行期契约需与实际版本核对 |
| QEMU 拓扑 | `docs/pcie.txt`、`hw/pci-bridge/gen_pcie_root_port.c`、`xio3130_upstream.c`、`xio3130_downstream.c` |
| EP/DMA/IOMMU API | `hw/misc/edu.c`、`include/hw/pci/pci_device.h`、`hw/pci/pci.c`、`hw/i386/intel_iommu.c` |
| 内存与模型测试 | `docs/devel/memory.rst`、`tests/qtest/pci-test.c`、`tests/qtest/intel-iommu-test.c`、`tests/qtest/libqos/` |

当前第一项工作按第 7 节步骤 1、2 定位已复用的软件基线、冻结运行期接口，再进入 P1。原第一阶段假包/bar/INTx限制继续有效，新的共享 DDR、真实 HDMA/NPU外部DMA、P2P 和八卡联合能力需逐项取得新证据。
