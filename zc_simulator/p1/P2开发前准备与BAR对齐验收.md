# P2 开发前准备与 BAR 对齐验收

2026-10-28。目的：在P1会话、共享DDR和常驻任务基线上，落实用户确认的硬件BAR属性，避免P2建立在原推导的小BAR窗口上。当前版本名称为 `single-card-functional-confirmed-bar-layout-v2`；协议帧版本仍是1，BAR0诊断`+4`的实验profile版本更新为2。

## 已完成的代码调整

| 项目 | 修改及作用 |
| --- | --- |
| BAR0 | 4MiB、64位、non-prefetchable，占BAR0/1；诊断和MSI-X table/PBA偏移保留 |
| BAR2 | 32MiB、64位、non-prefetchable，占BAR2/3；现有前2MiB共享窗口保留，其余访问明确拒绝 |
| BAR4 | 32GiB、64位、prefetchable，占BAR4/5；mailbox及实验服务区offset保留，未定义范围不映射DDR |
| Linux平台资源 | 显式`q35-pcihost.pci-hole64-size=64G`，单卡64位prefetchable窗口由固件和Linux分配，保留root port/switch拓扑 |
| 来宾验证 | 检查三个资源真实size、配置空间64位类型及prefetch位、BAR4高位地址；驱动原BAR2/BAR4 mmap保持可运行 |
| qtest | BAR4基址调整为32GiB对齐的`0x800000000`；所有访问地址、高位和低位配置联动更新；双function隔离测试用互不重叠的4MiB/32MiB BAR |
| 新增BAR专项 | `run_bar_layout.py`验证三个探测掩码、末端非法范围、Memory Space Enable开关、BAR0跨4GiB重定位及旧地址不再解码 |
| 持续任务 | 新增显式PID证据：连续普通任务前后service的spawn列表不变，原后端PID仍存活；保留reset/故障时才重建的语义 |

BAR aperture是PCI资源地址跨度，不等于宿主RAM分配，也不意味着这片地址全部支持访问。来宾任务测试只mmap实际使用的子范围，Host demo仍按resource文件实际大小mmap；二者都经过真实BAR资源分配。QEMU不硬编码用户提供的Host物理基址。MSI-X位于BAR0，BIR仍为0，table=`0x2000`、PBA=`0x3000`。

实测单卡Linux分配BAR0=`0xfc000000`、BAR2=`0xfa000000`、BAR4=`0xf000000000`；最后一项与用户机器不同是合法资源重分配，不应强制等同`0x7000000000`。

## 复现与验收

```bash
python3 qemu/zc_simulator/p1/run_full.py
```

完整入口现在包含七组：原P1-A/Host demo、socket会话/恢复/五次Linux、坏响应、BAR专项、真实共享内存边界、FD/共享IRQ/双function隔离、stage1回归。任何组失败都保持`p1_full_exit=false`。新增字段`confirmed_bar_attributes=true`只表示已确认并验证BAR宽度/大小/预取属性；`hardware_bar_compatibility=false`仍保留，表示完整硬件寄存器语义尚未验收。`host_dma_accepted=false`保持不变。

本轮新增代码只在qemu仓库，simulator源码和原Host demo文件未修改，无需重新解释既有业务runtime。

## 进入P2时应沿用的边界

- 普通命令复用同一Spike和已初始化内存。空闲轮询沿用当前用户接受的实现，P2不按每笔命令重新加载firmware。
- 配置空间BAR属性已经明确；BAR内部inbound/outbound映射、HDMA与iATU/ATE寄存器语义仍需按Zebu demo和手册实施。32GiB BAR4不直接等同整个DDR1。
- 先实现function绑定的PCIe访问服务，明确Host IOVA/设备地址/PCIe总线地址，再接Host文件IPC与HDMA；NPU外部DMA保持独立引擎路径，不借HDMA代算。
- 对coherent缓冲的真实设备访问必须走该function的DMA AddressSpace，不从宿主直接把IOVA当指针。跨页、未映射、只读写入、撤销和错误完成列为P2门禁。
- Host demo文件IPC与HDMA描述符分别验收；BootROM/FW加载协议与内部mailbox不能同址混作一个协议。
- P1已验证空闲reset与故障重启；P2新增外部在途事务后，必须重新实现和验证QUIESCE排空/取消及旧写屏障，不沿用“进程已结束所以outstanding=0”的简化证明。
- 32GiB BAR4的八卡地址资源至少256GiB；当前64GiB hole仅为单卡验收，八卡需扩大aperture并专门验收桥窗口，不据stage1小BAR八卡回归宣布大BAR八卡通过。
- 继续固定TCG `-icount shift=3,align=off,sleep=off`和IOMMU配置；默认计时历史panic的根因未修复，不要求工具链同事补资料。

## 最终运行证据

最终固定构建后的运行命令为 `python3 qemu/zc_simulator/p1/run_full.py --skip-build`。此前已完成当前QEMU构建；此参数仅跳过Spike/QEMU/firmware重建，runtime及来宾测试仍由runner构建。运行期间未替换源码或二进制。

汇总证据：[full-1793157438286545608/results.json](evidence/full-1793157438286545608/results.json)，结果为 `p1_full_exit=true`；对应manifest记录的25个源码文件与验收后工作区SHA256一致。证据目录由ignore排除，需在本机查看或重新运行生成。

| 验收组 | 本次结果 | 证据目录（相对本目录） |
| --- | --- | --- |
| P1-A及原Host demo | 21项通过 | `evidence/run-1793157438318618062` |
| socket会话与生命周期 | 102项通过，含连续任务PID不变、5次Linux启动 | `evidence/session-1793157461433470931` |
| 坏响应/对端故障 | 24项通过 | `evidence/peer-faults-1793157518988570715` |
| BAR属性/边界/重定位 | 14项通过 | `evidence/bars-1793157519853563658` |
| DDR共享内存边界 | 通过 | `evidence/memory-1793157519917335869` |
| FD/隔离/共享IRQ | 11项通过 | `evidence/faults-1793157520485043197` |
| 第一阶段回归 | 8个场景通过 | 工作区 `experiments/qemu_stage1/suite-1793157523196615723` |

其中 `ordinary_tasks_same_spike_pid=true` 和 `ordinary_tasks_backend_alive=true`直接验证连续普通任务复用存活的后端；reset和故障恢复时允许重建进程。当前Tensor输出仍按既有单die参考字节验证；历史golden对比存在322/1122个BF16差异、最大绝对误差1.0，本轮不声称修复数值精度。

据此，P1单卡功能与BAR属性准备项已完成，可以进入P2。P2先落实function访问服务及地址域契约，再实现文件IPC、HDMA与IOMMU错误路径；本节通过结论不覆盖这些待开发功能。

历史失败也保留：初版专项测试错误假定未解码区域一定返回全1，而BAR0原址位于可能由其他系统区域响应的地址；已改用设备magic是否消失判定是否仍解码。测试验证的是设备解码撤销，不依赖总线未映射读值。


Host测试同步也已补充：驱动probe只证明PCI资源和IRQ注册完成，不能代替后端软件ready。`host-probe.c`现在先有界等待BAR0软件ready，再做MSI-X注入。早期重复启动中的`HOST_PROBE_FAIL actual MSI-X handler`导致init退出panic，属于测试过早提交，与历史IO-APIC定时器初始化panic不同，保留失败证据供核对。

复测过程中另有一次由开发操作引起的`PermissionError`：验收启动QEMU时并行重链接了同一路径，属于构建/验收交叉造成的失败。该次不计为功能通过；最终证据使用构建固定后的顺序运行，不在验收过程中编辑或替换二进制。
