# BAR 硬件属性确认与接入调整

> 最新实现：三个BAR已对齐为64位，大小4MiB/32MiB/32GiB，前两个non-prefetchable、最后一个prefetchable。当前代码与旧实验profile不同；最新运行配置、P2准备项和证据见 [BAR对齐与P2准备](P2开发前准备与BAR对齐验收.md)。下文旧大小及“尚未修改”描述仅保留为历史。

2026-10-28。来源：用户提供的实际设备 Region 输出。本文确认硬件 BAR 宽度、预取属性和默认大小，供 P2 接入及 P1 profile 更新使用；本次仅更新文档，没有修改或重验当前 QEMU 设备。

## 确认信息

| Region | 配置空间占用 | 类型 | Prefetchable | 默认大小 | 用户提供的当前分配地址 |
| --- | --- | --- | --- | --- | --- |
| BAR0 | BAR0/BAR1 | 64-bit Memory | 否 | 4MiB (`0x400000`) | `0xfc000000` |
| BAR2 | BAR2/BAR3 | 64-bit Memory | 否 | 32MiB (`0x2000000`) | `0xfa000000` |
| BAR4 | BAR4/BAR5 | 64-bit Memory | 是 | 32GiB (`0x800000000`) | `0x7000000000` |

三个64位BAR各占两个配置空间BAR槽，Region编号为0、2、4，BAR1/3/5分别是高32位。分配地址由Host固件/OS及桥窗口配置决定，不能将这一次输出的地址硬编码为仿真固定基址。输出中的`[disabled]`表示资源当前未启用，不代表BAR不存在或大小为零；启用时须结合PCI Command Memory Space等配置核对。

## 对当前实现的影响

| Region | 已验收的P1软件profile | 硬件目标 |
| --- | --- | --- |
| BAR0 | 64KiB、32位non-prefetchable | 4MiB、64位non-prefetchable |
| BAR2 | 4MiB、32位non-prefetchable | 32MiB、64位non-prefetchable |
| BAR4 | 512MiB、64位prefetchable | 32GiB、64位prefetchable |

P1共享内存、会话、常驻进程及恢复结果仍是原profile的有效证据，但不能据此宣称上述硬件BAR布局已通过。后续需要修改`zc-pcie-p1.c`的region大小及BAR0/BAR2类型，联动来宾大小断言、qtest BAR高低位编程、MSI-X BIR和偏移校验，并重新跑完整验收。BAR大小是地址窗口跨度，不要求预分配同等宿主RAM；现有设备用MMIO回调实现，未支持范围应保持明确拒绝。

单卡BAR4需要32GiB且基址按32GiB对齐。Q35的64位PCI MMIO aperture、root port/switch bridge prefetchable窗口和来宾资源分配必须容纳它；八卡仅BAR4合计256GiB，还需为拓扑和其他设备保留资源。BAR0/BAR2虽64位但non-prefetchable，其经过桥的可分配地址范围还受桥窗口能力限制，不能仅凭64位BAR就假定可放任意高地址。

用户资料确认的是BAR整体属性，**没有定义BAR内部offset到设备地址的转换**。不能据此把整个32GiB BAR4直接等同DDR1的某段，也不能推断HDMA/iATU寄存器位置改变；应按Zebu demo、手册及后续窗口配置核对。

## 历史启动 panic 的归属

本地证据：`p1/evidence/run-1793096081305220405/linux.log`，来宾在约0.42秒报：

```text
Kernel panic - not syncing: timer doesn't work through Interrupt-remapped IO-APIC
panic_if_irq_remap
setup_IO_APIC
```

这是QEMU Q35/TCG中运行的x86 Linux，在初始化IO-APIC与IOMMU中断重映射相关的定时器路径时停止启动。发生在`/init`、Host demo驱动/应用运行之前；当时没有执行工具链命令包。该日志不能归因为工具链或Spike任务错误，也不能仅凭调用栈确定QEMU、来宾内核或计时调度哪一处是根因。

P1统一验收用显式`-icount shift=3,align=off,sleep=off`，保留IOMMU与中断重映射，连续五次启动及任务验收通过。这是已验证运行配置，不是已完成默认计时配置下panic根因修复。此项由本项目联合仿真环境侧排查，不需要工具链同事提供另一套runtime或解释x86启动故障，也不阻塞按当前固定配置开发P2。
