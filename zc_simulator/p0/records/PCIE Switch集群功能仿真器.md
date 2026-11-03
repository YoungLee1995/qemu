# PCIE Switch集群功能仿真器

# QEMU \+ NPU DIE 多卡联合仿真开发方案

本文定义功能阶段的集成架构、开发范围和验收要求。单 die runtime 及内部驱动已与硬件对齐，直接作为软件基线复用。本文是开发方案，尚未完成联合仿真实现与验证。

## 建设目标与范围

搭建 8 个 NPU PCIe device、1 个 PCIe switch、1 个 Host/Root Complex 的联合仿真平台。

- QEMU 提供 Host CPU、Linux、Root Complex、PCIe switch 和 Host IOMMU。

- 新增 NPU PCIe endpoint、Synopsys PCIe/HDMA 功能模型及适配桥接。

- 现有 NPU simulator 提供 MCU、NPU CORE、NPU 指令 DMA、mailbox、中断和设备内存模型。

- Host 运行硬件平台对应的设备驱动，MCU 继续运行现有 runtime，NPU 执行现有任务程序。

- 首期实现配置访问、任务下发、数据搬运、通知、错误和复位等功能；链路性能与完整 PCIe 协议细节后续扩展。

本项目不实现 CCL、collective、通信算法或完整算子库。真实 ROM、Host 下载固件及完整芯片冷启动链不属于首期范围。

## 总体架构

整体以 QEMU 为系统仿真框架，每个 NPU PCIe endpoint 通过桥接连接一个独立 NPU simulator 后端进程。

```Plain Text
QEMU 进程
 ├─ Host CPU + Linux + Host 驱动 + 测试程序
 ├─ Root Complex + Host IOMMU + Host RAM
 └─ PCIe Switch
     ├─ NPU Endpoint 0
     │   ├─ 配置空间 / BAR / MSI或MSI-X
     │   ├─ Synopsys HDMA 功能模型
     │   ├─ outbound / inbound 地址转换
     │   └─ PCIe 访问服务 + 桥接入口
     ├─ NPU Endpoint 1
     ├─ ……
     └─ NPU Endpoint 7

每个 Endpoint 分别通过 IPC + 共享 DDR 连接卡侧后端：

Endpoint i
    ↕ IPC：寄存器、通知、外部读写请求和结果
    ↕ 共享 DDR：同一份设备数据
独立 NPU simulator 进程 i
 ├─ 适配桥接
 ├─ MCU：运行常驻 runtime
 ├─ NPU CORE：运行常驻任务服务
 ├─ NPU 指令 DMA
 └─ mailbox / 本地中断 / DDR及本地内存
```

一个 QEMU 进程管理八个 endpoint，分别连接八个后端进程。采用多进程隔离现有模型中的全局状态；PCIe device ID/BDF 与后端内部 chip/die/core ID 分开管理。

首期每卡启用一个 MCU 和一个 NPU。单卡多 die 沿用卡内模型并扩展对应路由，跨卡访问统一走 PCIe；配置必须明确实际创建和启用的核数。

### 2\.1 组件分工

HDMA 作为 endpoint 内部模块实现，PCIe function 划分按真实硬件保持一致。

## QEMU 开发基线

准备完整、可构建的 QEMU 源码仓库，固定 release/commit、machine、guest kernel 和工具链。新增设备模型随 QEMU 编译，现有 NPU simulator 保持独立运行。

Host 平台优先匹配实际 CPU 架构和软件环境。初期环境验证可使用 x86\_64 q35、Linux 和 intel\-iommu。

PCIe 功能拓扑可使用 QEMU 的 root port、xio3130 upstream/downstream 组合：

```Plain Text
Host / Root Complex
 └─ Root Port
     └─ Switch Upstream Port
         ├─ Downstream Port 0 ─ NPU Endpoint 0
         ├─ Downstream Port 1 ─ NPU Endpoint 1
         ├─ ……
         └─ Downstream Port 7 ─ NPU Endpoint 7
```

上下行端口共同构成一个 switch。通过 guest 的 lspci 和 QEMU monitor 检查拓扑、BAR 和桥窗口；实际端口组合在锁定的 QEMU 版本上验证。

QEMU EDU 可参考设备注册、MMIO、DMA 调度和中断实现。正式设备模型按目标 Synopsys IP 和 NPU 平台接口实现，使用对应真实驱动。

## MCU/NPU 启动与运行方式

沿用现有 firmware 配置加载和 dispatcher 启动方式，MCU/NPU 的初始化入口、镜像和加载地址可以固定。

```Plain Text
建立桥接连接和共享 DDR
 → 沿用现有方式加载 MCU/NPU 镜像
 → MCU 从固定入口初始化，运行 runtime
 → NPU 从固定入口初始化，进入任务等待服务
 → 完成 ready 握手
 → 等待 Host 任务并持续运行
```

MCU 等待 Host mailbox 任务，NPU 等待 MCU 通过内部 mailbox/既有调度机制下发任务。初始化代码在各自核模型上实际执行，ready 由软件发布。

单次任务完成后 MCU/NPU 返回服务循环，后端继续运行。任务完成不触发整卡退出；设备 reset 后重新初始化并处理后续任务。

现有单 die runtime、内部驱动和启动代码整体复用。仿真侧负责新增外围连接、外部 DMA、共享内存及生命周期适配。

## Host 与 Device 的任务及通知通路

### 5\.1 Host → Device：沿用 mailbox

```Plain Text
Host 驱动写 PCIe BAR 映射的 mailbox/通知入口
 → QEMU endpoint
 → IPC 桥接
 → 现有 mailbox 模型
 → MCU 中断或轮询
 → MCU runtime 处理任务
 → 内部 mailbox/调度通知 NPU
```

Host 与 MCU 访问同一份 mailbox 状态。BAR 偏移、消息格式、通知、pending/mask/ack 和清除行为沿用硬件平台已有实现。

mailbox 传递任务参数或描述符地址；大块输入输出由 HDMA 或 NPU DMA 搬运。已有协议使用内存队列时直接复用，不新增一套任务协议。

### 5\.2 Device → Host：完成状态与 MSI/MSI\-X

```Plain Text
NPU/DMA 完成
 → MCU runtime 按既有协议处理
 → 确认结果数据满足 Host 可见性要求
 → 发布完成状态/消息
 → 按硬件控制方式触发 PCIe MSI/MSI-X
 → QEMU 向 Host 投递中断
 → Host 驱动读取完成信息并处理
```

具体采用 MSI 还是 MSI\-X，以及由哪一侧触发，遵循真实硬件和驱动。MSI/MSI\-X 提供通知，任务详情和错误信息保存在原协议规定的 mailbox、状态区或完成队列中。

NPU→MCU、HDMA→MCU 使用设备内部通知/中断；Device→Host 使用 PCIe 通知机制。各中断源及其 mask/ack 状态按硬件分别维护。

## 两类 DMA 的职责与能力

两个引擎独立提交、完成和复位。NPU DMA 发起 PCIe 访问时不需要启动 HDMA 通道；两者共享 PCIe 访问服务和允许访问的设备内存。

### 6\.1 Synopsys HDMA 模型

以目标 IP 版本、综合配置、手册和实际驱动为基线，实现初始化、描述符获取、通道执行、完成、错误、中断及停止/复位。

首个真实驱动闭环即覆盖该驱动实际使用的描述符模式、地址宽度和通道配置。Host BAR 和 MCU 内部 MMIO 若映射同一控制器，必须访问同一份状态；控制权安排沿用硬件平台。

QEMU 的 `pci_dma_read()` / `pci_dma_write()` 是设备模型内部的数据访问函数。HDMA 通道和描述符状态机由本项目实现，再调用访问后端执行搬运。

### 6\.2 NPU 指令 DMA 外部访问

保留原指令和描述符格式，在实际内存访问处增加外部访问适配，将命中 PCIe outbound 窗口的访问提交给 QEMU，接收读数据和完成/错误结果。

原本地访问继续由现有模型处理。外部搬运只执行一次，并贯通原 DMA 的 busy、依赖/fence、完成和错误路径，避免外部访问失败后仍报告成功。

## 地址空间与 PCIe 数据路径

### 7\.1 地址划分

区分四类地址：本 die 内存、本卡其他 die 的内部地址、PCIe outbound 窗口、非法地址。

NPU 描述符填写设备侧 outbound 窗口地址，由地址转换表映射为 Host IOVA 或 peer BAR 地址。ATE 在本文泛指设备侧地址转换表，具体名称和格式按目标 IP/SoC 规格实现。

512 GiB 地址槽的实际解码按设备地址地图确定。不能把所有未命中本地内存的地址都自动视为 PCIe；窗口必须能由现有 NPU 指令及描述符的位宽表达。

### 7\.2 NPU DMA 访问 Host

```Plain Text
NPU DMA 的设备侧窗口地址
 → outbound/ATE
 → Host IOVA
 → PCIe switch → RC
 → Host IOMMU 翻译及权限检查
 → Host RAM
```

Host 驱动建立 DMA 映射并传递 IOVA，软件配置相应出口窗口。outbound 转换与 Host IOMMU 是两级不同转换，不能用 Host 物理地址替代 IOVA 来绕过 Host IOMMU。

QEMU 访问服务使用源 endpoint 的 DMA AddressSpace 执行 Host 访问。NPU DMA 和 HDMA 属于同一 function 时使用该 function 的 requester 身份。

### 7\.3 NPU DMA 访问其他卡

```Plain Text
NPU A DMA 的设备侧窗口地址
 → A outbound/ATE
 → B 当前的 PCIe BAR 地址
 → Switch P2P 功能路由
 → B BAR decode + inbound/window 映射
 → B DDR
```

P2P 不经过 Host IOMMU，也不经 Host staging buffer。读取结果返回源 NPU DMA，写入其指定的本地目标。

Host 动态分配 BAR 后，软件将 peer 地址传给设备并更新相应映射。BAR 重定位或失效后必须重新校验，不能硬编码各卡 BAR base。

### 7\.4 HDMA 的地址处理

HDMA 描述符地址域及其相对 outbound 的接入位置，按目标 Synopsys IP 和 SoC 集成资料实现。需要转换的路径先转换，已处于最终 PCIe 地址域的路径避免重复转换。

HDMA 访问 Host 时经过 Host IOMMU，访问 peer 时进入 P2P 路径。动态 IOVA/BAR 通过实际驱动规定的描述符或映射配置生效。

### 7\.5 功能 P2P 实现

QEMU 增加 P2P 功能访问服务，根据实际拓扑、BAR 和桥窗口定位目标，检查源 bus\-master、目标 memory\-space、P2P enable、权限、边界及设备状态，再访问目标共享 DDR。

该服务实现首期路由功能；完整 TLP、credit 和仲裁模型在后续阶段实现。P2P 禁用时返回明确错误，不静默改走 Host staging。

## 设备内存与适配桥接

### 8\.1 共享 DDR

每张卡的对外 DDR 通过 memfd/MAP\_SHARED 等方式提供给 QEMU 和对应后端，保持一份真实数据。NPU DMA、HDMA、Host BAR 和 P2P 在允许范围内访问该数据。

L1、MCU SRAM 等默认沿用现有本地模型，HDMA 可达范围按硬件确定。测试显存容量可配置，避免一次分配完整硬件容量。

共享映射之外，还要实现与真实软件协议一致的发布和完成同步：描述符/数据先发布，再通知；数据落地并满足可见性要求后再报告完成。跨 DMA 引擎的数据依赖由软件显式同步。

### 8\.2 桥接职责

同机采用 Unix domain socket 传输控制和外部访问消息，共享内存承载设备 DDR。桥接覆盖：

- Host 对 mailbox 的访问和 MCU 通知。

- MCU 对 PCIe/HDMA/地址转换寄存器的访问。

- HDMA 完成/错误到 MCU 的本地中断。

- MCU 触发 Host MSI/MSI\-X 的硬件控制路径。

- NPU DMA 外部读写请求、读数据和完成/错误返回。

- 初始化、运行、reset、退出和断连处理。

QEMU 维护 PCIe/HDMA/地址转换状态；后端维护 MCU/NPU、内部 mailbox 和本地中断状态。同一寄存器或表项只保留一个权威状态持有者。

所有请求关联设备、请求编号、连接会话和复位代次。后端等待外部结果期间，两侧继续处理消息和中断，避免跨进程相互等待。

### 8\.3 错误与复位

地址未命中、IOMMU fault、权限错误、P2P 禁用、非法描述符和后端断连必须返回相应原请求，不能被吞掉或转换为成功。

reset 按硬件区分 channel、function、核和全卡作用域。复位时停止接收相关新请求，取消或排空旧访问，确认旧请求不再修改内存，再重置状态并重新初始化；共享内存句柄按相同生命周期管理。

Trace 区分确认完成的数据量和无法确定的失败块进度。内部诊断信息仅在硬件定义对应接口时才对驱动可见。

## 典型任务流程

### 9\.1 HDMA 输入输出，NPU DMA 处理本地搬运

```Plain Text
Host 准备 buffer/IOVA，通过 mailbox 下发任务
 → MCU runtime 驱动 HDMA，将输入搬到设备 DDR
 → MCU 通知 NPU
 → NPU 用内部 DMA 搬运并执行任务
 → 输出放到 HDMA 可达的设备内存
 → MCU 驱动 HDMA 写回 Host
 → 发布完成状态，发送 MSI/MSI-X
```

### 9\.2 NPU DMA 直接输入输出

```Plain Text
Host 建立输入/输出 DMA 映射，通过既有任务协议提供参数
 → 配置 NPU outbound 窗口
 → MCU 通过 mailbox/调度下发 NPU 任务
 → NPU 执行 DMA 指令，经 PCIe + Host IOMMU 读取输入
 → NPU 执行任务
 → NPU 执行 DMA 指令，写回 Host 输出
 → NPU 确认完成并通知 MCU
 → 发布完成状态，发送 MSI/MSI-X
```

该流程不启动 HDMA 通道。另需覆盖 HDMA 输入/NPU DMA 输出，以及 NPU DMA 输入/HDMA 输出两种组合，按完成依赖衔接。

### 9\.3 P2P 搬运

源卡 NPU DMA 或 HDMA 发起访问，经源端地址处理、switch 功能路由和目标 inbound 读写目标 DDR。

搬运完成表示数据达到规定可见性，不代表目标 runtime 已消费。需要目标消费时，通过硬件支持的通知方式或 Host 协调完成。

## 团队分工与最小输入

单 die runtime、BSP、MCU/NPU 内部驱动、mailbox 和本地 DMA 直接沿用现有版本。驱动及软件团队的增量输入收敛为以下三组，已有代码中包含的内容无需重复交付。

Runtime 软件团队配合确认现有 mailbox 任务字段和完成路径，提供可复用的任务用例。NPU 外部地址通过既有参数机制传入，不另写 NPU DMA 驱动。

仿真团队负责 QEMU endpoint/HDMA/地址转换/P2P 模型、双侧桥接、共享 DDR、NPU 外部 DMA 适配和联合回归。

实施前锁定目标 IP/驱动版本、Host 平台、BAR/内部地址地图、HDMA outbound 路径及中断触发方式。启动平台差异集中在明确的适配层，运行期接口保持硬件一致。

## 开发步骤与验收

P2 中 HDMA 与 NPU DMA 的 Host 分支可基于共享访问服务分别开发，不要求先完成 HDMA 才开始 NPU 外部 DMA。

功能阶段至少覆盖：

1. 单卡、双卡、八卡枚举，配置空间、BAR 和桥窗口正确。

2. 固定入口启动、ready、mailbox、中断及连续任务；任务结束不退出后端。

3. 原 NPU 指令、本地 DMA 和核间交互回归。

4. HDMA Host 读写、真实描述符模式、通道完成与错误。

5. 不启动 HDMA 时，NPU 指令 DMA 独立完成 Host 读写和错误回传。

6. 非恒等 IOVA、设备 domain 隔离、跨页、未映射、权限及映射撤销。

7. 两类 DMA 的混合输入输出，数据可见性及完成通知顺序。

8. P2P read/write、双向、8×7 个有向卡对；按两类引擎及实际能力分别验证。

9. P2P 禁用、BAR 重定位、窗口越界和转换表重配置。

10. 多卡并发、通道及设备 reset、runtime 异常、后端断连和恢复，无旧请求污染。

测试包含设备模型测试、后端回归和 guest 真实驱动集成。数据长度、对齐和边界用例按目标 DMA/IP 的合法能力及错误定义设置。

## Trace、交付物与后续扩展

Trace 关联任务、DMA 请求和桥接消息，记录源设备/核/引擎、目标、转换前后地址、长度、路径、状态、错误、映射版本及复位代次。Host IOMMU fault 关联其真实日志，部分进度只记录可证实的数据。

功能路径区分本地访问、Host IOMMU、P2P 和 Host staging。采用受控调度验证相同输入下功能结果可重复；首期事件时间不作为真实硬件时延。

交付完整 QEMU 源码基线及补丁、NPU simulator 适配代码、驱动构建配置、单卡/双卡/八卡启动脚本、共用测试程序、Trace 和需求覆盖报告。

后续在统一 PCIe 访问服务下扩展 TLP 分片、tag/completion、协议 ordering、credit、buffer、backpressure、仲裁、带宽和延迟模型。首期验证功能正确性，后续协议与性能按独立指标验收。

