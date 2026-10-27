# 8 卡单 PCIe Switch Simulator 需求

## 1. 文档范围

本文只定义 PCIe simulator 的需求，不定义 CCL、collective、kernel 或通信库的实现需求。

目标拓扑如下：

```text
PCIe Device 0 ─┐
PCIe Device 1 ─┤
PCIe Device 2 ─┤
PCIe Device 3 ─┤── PCIe Switch ── PCIe Root Complex ── CPU
PCIe Device 4 ─┤
PCIe Device 5 ─┤
PCIe Device 6 ─┤
PCIe Device 7 ─┘
```

Simulator 需要验证 8 个 PCIe device 连接到同一个 switch、switch 再连接到一个
CPU/Root Complex 时，PCIe transaction、带宽、延迟、拥塞和错误行为是否符合配置。

## 2. 基本假设

- 设备数量为 8 个；
- PCIe switch 数量为 1；
- Root Complex 数量为 1；
- 8 个 device 分别连接到 switch 的下行端口；
- switch 通过一个上行端口连接 Root Complex；
- 设备侧不启用 SMMU；
- 主机侧启用 IOMMU；
- 设备访问主机内存时使用 IOVA，由主机 IOMMU 转换为主机物理地址；
- 设备访问设备显存时使用 PCIe P2P 路径，不经过主机 IOMMU；
- 每个 PCIe device 内包含一个 NPU 核和一个 MCU 核；
- NPU 核只需要支持 NI900 指令/执行模型和 DMA，不要求模拟其他 NPU 加速单元；
- MCU 核需要能够启动和运行 device runtime，负责设备初始化、队列和 DMA 管理；
- simulator 不需要模拟真实指令，只需要模拟 PCIe endpoint、NPU、MCU 和 DMA 行为；
- simulator 不负责实现 CCL，只提供通用 PCIe 仿真接口。

## 3. 需要模拟的对象

至少包含：

```text
RootComplex
HostIOMMU
PcieSwitch
PcieEndpoint[8]
HostMemory
```

每个 endpoint 至少包含：

```text
Endpoint {
  id;
  bus;
  device;
  function;
  linkWidth;
  linkSpeed;
  configSpace;
  barSpace;
  npuCore;
  mcuCore;
  dmaEngine;
  localMemory;
}
```

每个 PCIe device 内部至少包含以下组件：

```text
PcieDevice {
  PcieEndpoint endpoint;
  Ni900NpuCore npu;
  McuCore mcu;
  DmaEngine dma;
  DeviceMemory localMemory;
  DeviceRuntime runtime;
  CommandQueue queues;
  InterruptController interrupts;
}
```

### 3.2 NPU 核需求

NPU simulator 只需要实现 NI900 相关的最小执行模型和 DMA 访问能力，不需要模拟
完整 NPU SoC 或其他计算加速单元。

至少支持：

- NI900 指令或 NI900 抽象指令的加载、解码和执行；
- 指令流启动、停止、暂停和复位；
- NPU 本地寄存器和状态寄存器；
- NPU 对本地内存的读写；
- NPU 通过 DMA engine 访问 host memory；
- NPU 通过 DMA engine 访问其他 PCIe device 的目标地址；
- DMA 完成、错误和超时状态回报给 NPU；
- NPU 产生 doorbell、completion 和 interrupt；
- NPU 执行时间、DMA 等待时间和 stall 状态可被 trace 记录。

NPU 不要求支持：

- 真实硬件周期级流水线；
- 完整的 NPU 算子库；
- 其他 NPU core、Tensor/Vector/Matrix 单元；
- 其他 NPU 指令集或完整 NPU 算子库；
- NPU 内部 cache 的逐周期精确建模。

建议提供最小 NPU 抽象接口：

```text
npuLoadProgram(device, program, address)
npuStart(device, entryPoint)
npuStop(device)
npuReset(device)
npuReadRegister(device, reg, value)
npuWriteRegister(device, reg, value)
npuGetStatus(device, status)
```

### 3.3 MCU 核与 device runtime

MCU 核用于运行 device runtime，不要求模拟通用 CPU 的完整 ISA。Simulator 至少
需要提供一个可运行的 MCU 执行模型，支持：

- MCU 启动、复位、异常和停止；
- runtime image 加载；
- runtime 初始化 PCIe BAR、NPU、DMA 和 interrupt controller；
- 创建、销毁和轮询 NPU command queue；
- 提交 NPU program 和 DMA request；
- 处理 DMA completion、NPU completion 和错误中断；
- 管理 device state、queue state 和 runtime heartbeat；
- runtime 日志和 trace 输出。

MCU runtime 的典型启动顺序为：

```text
MCU reset
    ↓
加载 device runtime
    ↓
初始化 BAR / registers / DMA / interrupts
    ↓
初始化 NI900 NPU
    ↓
创建 command queues
    ↓
等待 host 或 NPU command
```

建议提供以下最小接口：

```text
mcuLoadRuntime(device, image)
mcuStart(device)
mcuReset(device)
mcuStop(device)
mcuStep(device, cycles)
mcuGetRuntimeState(device, state)
```

Simulator 可以使用解释器、基本块执行器或指令级模型实现 MCU；具体 MCU ISA
不属于本需求的重点，但 runtime 必须能够实际运行并驱动 NPU 和 DMA。

### 3.4 DMA engine 需求

NPU 和 MCU 共享或分别使用 device DMA engine 均可，但 simulator 必须明确 DMA
request 的 owner、来源和完成通知。DMA engine 至少支持：

```text
DmaRequest {
  requester;          // NPU / MCU
  sourceAddress;
  destinationAddress;
  addressType;        // DEVICE_PA / HOST_IOVA / DEVICE_BAR_PA
  bytes;
  direction;
  maxOutstanding;
  completionMode;
  interruptOnComplete;
}
```

DMA engine 必须支持设备 runtime 提交 request、NPU 发起 request、多个 outstanding
request、request 分片、completion、timeout 和 error interrupt。DMA request 仍然
必须遵守本文后续定义的 PCIe routing、IOMMU、credit、ordering 和权限检查。

### 3.5 PCIe 链路

每条 PCIe 链路至少支持以下配置：

```text
LinkConfig {
  generation;          // Gen3 / Gen4 / Gen5 / ...
  laneWidth;           // x4 / x8 / x16
  latency;
  maxPayloadSize;
  maxReadRequestSize;
  flowControlCredits;
}
```

Simulator 必须根据 PCIe generation 和 lane width 计算理论带宽，并允许配置协议
开销、传输延迟和带宽限制。

## 4. PCIe 协议功能

Simulator 至少支持：

- Memory Read；
- Memory Write；
- Completion with Data；
- Completion without Data；
- Configuration Read；
- Configuration Write；
- Message；
- 可选的 Atomic Operation。

每个 transaction 至少包含：

```text
PcieTransaction {
  id;
  type;
  requesterId;
  completerId;
  address;
  length;
  tag;
  trafficClass;
  attributes;
  payload;
  timestamp;
}
```

Simulator 必须检查 requester/completer、地址和长度、Completion 的 requester/tag
匹配、Memory Write ordering、Unsupported Request、非法地址和 Completion timeout。

## 5. PCIe Switch 功能

Switch 至少包含 8 个下行端口和 1 个上行端口：

```text
Switch {
  downstreamPorts[8];
  upstreamPort;
  routingTable;
  arbitrationPolicy;
  portBuffers;
}
```

Switch 必须支持：

1. 根据 Bus/Device/Function 或 routing 信息转发 transaction；
2. device 到 device 的 P2P 转发；
3. device 到 host memory 的上行转发；
4. host memory 到 device 的下行转发；
5. Completion 按原路径返回 requester；
6. 多端口并发发送；
7. buffer 满时 backpressure；
8. 共享带宽时体现端口竞争。

典型路径：

```text
Device 0 -> Switch port 0 -> Switch port 1 -> Device 1
Device 0 -> Switch port 0 -> Switch upstream -> Root Complex -> Host Memory
```

## 6. 地址、内存和主机 IOMMU

Simulator 必须维护全局主机物理地址空间，至少包含：

- 8 个 device local memory 区间；
- host memory 区间；
- PCIe BAR 区间；
- 可选的 MMIO 区间。

每个内存区间至少包含：

```text
MemoryRange {
  physicalBase;
  size;
  owner;
  readPermission;
  writePermission;
}
```

Simulator 还必须维护每个 device 的 IOVA 地址空间和主机 IOMMU 转换表：

```text
IommuMapping {
  deviceId;
  iovaBase;
  physicalBase;
  size;
  readPermission;
  writePermission;
  valid;
}
```

设备 DMA 访问主机内存时，路径必须是：

```text
Device DMA IOVA
        ↓
Host IOMMU 查表和权限检查
        ↓
Host physical address
        ↓
Host memory
```

必须检查：

- 地址范围不能重叠；
- IOVA 映射不能重叠；
- transaction 不能访问未映射或未分配地址；
- 读写权限必须匹配；
- transaction 不能越过映射或内存区间边界；
- IOMMU 映射失效后再次访问必须产生 fault；
- IOVA 到物理地址转换失败必须产生可追踪的 IOMMU fault；
- 设备侧不进行 SMMU 地址转换。

Simulator 不需要模拟 CPU 虚拟地址，但必须模拟设备 IOVA 到主机物理地址的转换。

## 7. P2P 访问

Simulator 必须支持两个 PCIe device 之间的 P2P Memory Read/Write：

```text
Device A
   │ Memory Read / Memory Write
   ▼
PCIe Switch
   ▼
Device B local memory
```

P2P 要求：

- transaction 不经过 CPU 执行数据拷贝；
- transaction 必须经过 switch 端口、路由、credit 和仲裁模型；
- 目标设备必须拥有对应的设备物理地址范围；
- P2P transaction 不经过主机 IOMMU；
- simulator 记录源设备、目标设备和 transaction 类型；
- 可以配置 P2P enable/disable；
- P2P 被禁止时返回明确错误。

Simulator 必须区分以下两种路径：

```text
Device A -> Device B：P2P，经过 PCIe switch
Device A -> Host Memory -> Device B：经过主机 IOMMU 的两段 DMA
```

## 8. DMA 模型

每个 device 至少有一个抽象 DMA engine：

```text
DmaRequest {
  sourceAddress;
  destinationAddress;
  addressType;       // DEVICE_PA / HOST_IOVA / DEVICE_BAR_PA
  bytes;
  direction;
  maxOutstanding;
  completionMode;
}
```

DMA engine 必须支持：

- 多个 outstanding request；
- request 分片为多个 PCIe transaction；
- 最大 payload size 和最大 read request size；
- completion 顺序和乱序；
- DMA 完成、超时和错误事件；
- 读写带宽统计。

当目标是主机内存时，地址类型必须为 `HOST_IOVA`，并执行 IOMMU 转换。当目标
是另一张卡的显存时，使用设备目标物理地址并执行 P2P 路由，不经过主机 IOMMU。

## 9. 流量控制和拥塞

Simulator 必须模拟：

- Posted、Non-Posted 和 Completion buffer；
- Header/Data credit 的消耗和返还；
- credit 不足时暂停发送；
- 接收端 buffer 满时 backpressure；
- 多设备竞争 switch 上行端口；
- 下行端口竞争；
- 端口仲裁延迟。

仲裁策略至少支持：

- round-robin；
- fixed priority；
- weighted round-robin。

## 10. Ordering 和 Completion

至少提供以下模式：

1. `strict`：严格按照提交顺序完成；
2. `pci_relaxed`：允许协议规定范围内的 relaxed ordering；
3. `delayed`：为 posted write、read completion 和 interrupt 增加可配置延迟。

必须验证 Memory Write 可见性顺序、Memory Read Completion 的 tag 匹配、Completion
不误投递、switch 不丢失 transaction，以及 transaction 不重复完成。

## 11. 配置需求

Simulator 应支持配置文件或命令行参数：

```text
numDevices = 8
numSwitches = 1
numRootComplexes = 1
deviceSmmuEnabled = false
hostIommuEnabled = true
iommuPageSize = 4096
iommuFaultOnUnmapped = true
p2pBypassHostIommu = true
p2pEnabled = true
npuCountPerDevice = 1
npuModel = ni900
npuDmaEnabled = true
mcuCountPerDevice = 1
mcuRuntimeEnabled = true
switchUpstreamWidth = x16
switchDownstreamWidth = x16
linkGeneration = Gen4
maxPayloadSize = 256
maxReadRequestSize = 512
arbitration = round_robin
ordering = pci_relaxed
```

每个端口的 generation、lane width、latency、buffer size 和 credit 都应能单独配置。

## 12. 测试场景

至少提供以下测试：

1. 配置空间读写；
2. MCU 加载并启动 device runtime；
3. runtime 初始化 BAR、NI900 NPU、DMA 和中断；
4. MCU 提交 NI900 program 并读取 NPU completion；
5. NI900 NPU 发起本地 DMA；
6. device 通过 IOVA 写 host memory；
7. device 从 host memory 读取数据；
8. IOVA 映射命中；
9. IOVA 未映射、越界和权限错误；
10. Device 0 到 Device 1 的单向 P2P；
11. Device 0 到 Device 1 的双向 P2P；
12. 8 个 device 的两两 P2P；
13. 8 个 device 同时访问 host memory；
14. 8 个 device 同时进行 all-to-all traffic；
15. 多设备竞争 switch 上行端口；
16. credit 耗尽与恢复；
17. Completion timeout；
18. 非法地址和越权访问；
19. 禁用 P2P 后的错误行为；
20. switch 端口阻塞和 backpressure；
21. IOMMU 映射撤销后仍有 DMA 请求的 fault；
22. MCU reset、runtime crash 和 NPU DMA timeout。

## 13. 日志和验收标准

每个 transaction 至少记录：

```text
transactionId
startTime
endTime
requesterId
completerId
address
addressType
translatedPhysicalAddress
bytes
type
route
status
errorCode
iommuFault
```

Simulator 至少输出：

- 每条链路的 transaction 数量；
- 每个 NPU 的 NI900 指令执行、DMA request、stall 和 completion；
- 每个 MCU 的 runtime 启动、reset、queue 操作和中断；
- 每个端口的带宽利用率和 buffer occupancy；
- credit stall 时间；
- transaction 平均和最大延迟；
- P2P transaction 数量；
- 经 IOMMU 转换的 DMA 数量；
- IOMMU fault 数量；
- Completion timeout 和 Unsupported Request 数量；
- 非法地址访问数量。

基础验收条件：

1. 能创建 8 卡、1 switch、1 CPU/Root Complex 和 1 个 Host IOMMU；
2. 每个 device 都能创建一个 NI900 NPU、一个 MCU 和至少一个 DMA engine；
3. MCU 能加载并运行 device runtime；
4. runtime 能初始化并驱动 NI900 NPU 和 DMA；
5. 能完成合法的配置访问、Memory Read、Memory Write 和 Completion；
6. 能正确转发 device-to-device P2P transaction；
7. 能正确执行 device IOVA 到 host physical address 的转换；
8. 能检测非法地址、非法权限、IOMMU fault、Unsupported Request 和 timeout；
9. 能模拟 credit、buffer、backpressure 和端口仲裁；
10. 能体现 8 卡共享 switch 上行资源时的带宽竞争；
11. 能导出 PCIe、DMA、MCU 和 NPU 的完整 trace；
12. 相同配置和输入下结果可重复；
13. 不依赖真实 NPU 或 CCL 即可运行基础测试。

## 14. 明确不属于本需求的内容

以下内容不属于本 simulator 需求：

- CCL communicator；
- CCL transport selector；
- Ring、Tree、AllReduce 等 collective 算法；
- 完整 NPU kernel 或算子实现；
- CCL proxy 线程或 daemon；
- NPU 设备间的专用 IPC 实现；
- CCL 的 chunk、channel 和 protocol 设计。
