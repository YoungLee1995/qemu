# Host demo 对 P0 与 PCIe 后续开发的影响

日期：2026-10-27。输入为 `simulator/host-demo/ssv_test_driver.c` 和 `ssv_test_app.c`。本次执行源码核对、用户态应用编译和 `--help` 运行；未加载内核驱动，未连接 QEMU/硬件执行 DMA。

**结论：P0 已完成的结论保留，需要补充接口契约和 P1/P2 用例。** 现在已有具体 Linux Host 测试驱动和应用，之前“只有 Zebu 卡侧参考、独立 Host 驱动尚不存在”的描述更新为“已有简单 Host demo，尚未联合验证”。它提供 DMA 缓冲与测试 IPC，不要求另一套 runtime 镜像，也不能据此认定业务任务协议、HDMA 模式或完整真实驱动兼容已验收。

## 1. 新增的确定信息

| 项目 | 源码事实 | 对模型/验收的影响 |
| --- | --- | --- |
| PCI 身份 | VID `0x16c3`、DID `0xabcd` | 后续 demo 兼容 EP 必须匹配；保留 stage1 `1234:11f0` 的旧回归，不直接改掉历史实验设备 |
| 多设备 | driver 每设备独立状态/缓冲/节点；最多16节点；app按BDF或epN选择 | 可作为八卡Host侧基础；不能把minor或发现顺序当卡内chip/die ID |
| Host 缓冲 | `dma_alloc_coherent` 分配每卡2MiB；64位DMA mask，失败回退32位；`dma_mmap_coherent` 导出 | 这是来宾Host内存，不是memfd设备DDR；EP经源function的DMA AddressSpace/IOMMU访问 |
| 地址来源 | `phys_addr` sysfs实际返回 `dma_handle` | 名称虽为phys，语义是DMA地址；IOMMU启用时可为IOVA，不能当GPA或宿主指针 |
| MSI-X | probe必须成功申请1～32向量并request_irq，无INTx降级 | P1须有可用MSI-X capability/table/PBA；目标支持32向量以覆盖请求上限，实际获批数记录；尚未给业务向量含义 |
| BAR2 | FW窗口 `[0,0x200000)`；FW IPC从 `+0x200180` 开始 | 与Zebu的2MiB SRAM及相邻4KiB IPC窗口一致；完整窗口要求至少`0x201000`，标准power-of-two BAR至少4MiB，属性/实际大小仍非源码确认值 |
| BAR4 | 应用以BAR4基址加 `0x18103180` 访问传输寄存器 | 是BAR内偏移，不是分配后的总线绝对地址；访问到`+0x18103198`的4字节，最小标准BAR跨度512MiB（推导值），不能使用stage1的64KiB BAR |
| 输入传输 | `file-load`拷文件至coherent缓冲，写DMA地址、长度、CRC32及trigger | 给出Host→设备搬运测试入口；并未直接编程HDMA通道/LLI，不能由此推断必须使用哪种搬运引擎 |
| 输出传输 | `get-result`把同一coherent缓冲地址交给EP，轮询内存done标记 | 完成数据必须先可见再发布done；应用没有用MSI-X等待结果，IRQ验收需独立触发/计数 |
| 固件命令 | `fw-load/img-load`经BAR2复制带头固件，CMD=`0x12` | 属于可选Host下载/BootROM路径，仍不提升为首期启动门槛 |

BAR4跨度是应用实际地址运算的结果，不代表硬件BAR大小已经确认。实现可用稀疏MMIO映射，不应为512MiB窗口直接分配等量RAM。八卡仅BAR4最小就需4GiB地址资源，须重新验证64位BAR属性、Q35资源与桥窗口；旧八个64KiB EP枚举证据不能替代此项。

## 2. 传输接口与内部 mailbox 分开管理

`file-load` 与 `get-result` 共用 BAR4 内 `0x18103180` 起的槽位：

| 相对偏移 | file-load | get-result |
| --- | --- | --- |
| `+0/+4` | Host DMA地址低/高32位 | 接收缓冲DMA地址低/高32位 |
| `+8/+0xc` | 长度低/高32位 | 未写新值，不能据此沿用旧长度作为新协议 |
| `+0x10` | `0x5a5aa5a5`提交 | `0x72736c74`请求结果 |
| `+0x14/+0x18` | CRC32放入64位槽位，高32位为0 | 未写 |

结果缓冲格式：`u32 done=0x646f6e65`、`u32 payload_size`、随后payload；有效payload须不超过2MiB减8。后端应先写payload/长度，再以有序方式发布done，不能把收到trigger当传输完成。

这套寄存器不是 `riscv/mbox.cc` 的 `0x18019000` 内部mailbox。Zebu `pcie_ipc.h` 又把同一FW IPC起点的 `+0/+4` 定义为COMMAND/STATUS、`+8/+0xc`为DATA0/1；`pcie_ep_wait_host_target_addr`则从`+0/+4`读Host地址。必须区分BootROM阶段与文件传输阶段，或为软件实验显式选择profile；不能同时注册三套互相独立且声称同一寄存器的状态。

当前卡侧上传片段未找到与上述两个trigger及done格式完整对应的文件服务，因此只能确定Host端访问契约；不能宣称两份demo原样即可闭环。P2应实现明确的测试搬运服务或接入实际卡侧服务，再验证CRC/长度/数据。该服务不冒充业务runtime任务解析或HDMA实现。

## 3. 接入前要处理的具体问题

1. **IRQ回调的设备指针类型不匹配**：`request_irq(..., pdev)`传入`struct pci_dev *`，handler直接`dev_info(dev_id, ...)`按`struct device *`解释。应使用`&pdev->dev`等一致的上下文；否则触发中断时日志访问错误。当前不加载此模块来尝试IRQ。
2. **内核版本适配与构建文件缺失**：只有两个C文件，无Kbuild/Makefile。driver使用旧式`class_create(THIS_MODULE, CLASS_NAME)`；stage1来宾为Linux6.12，需要适配该内核API、模块配置和精确匹配的headers/vermagic。现有BusyBox/initramfs也须加入模块加载所需资源，不能用宿主内核产物代替来宾模块。
3. **无用hugepage成为启动依赖**：`ep_open`强制分配hugepage并经pagemap找物理地址，但当前`get-result`实际使用coherent缓冲。该额外缓冲应移除/设可选；不可把pagemap物理地址拿去绕过IOMMU。
4. **minor分配不适合任意热拔插**：用递增计数并在remove时减1，删除非末尾设备后再probe可能与仍在使用的minor冲突；应使用IDA等空闲编号分配。打开fd/mmap与remove间还缺少生命周期保护，八卡恢复用例前需补齐。
5. **输入/输出复用缓冲与寄存器，缺少传输完成握手**：`file-load`触发后立即返回；`get-result`会清零同一缓冲。若设备尚未读完输入，会覆盖输入。必须定义输入消费完成、buffer所有权及寄存器复用时点，禁止直接连续执行就认定正确。
6. **自动验收与边界检查不足**：FW等待可无限循环；get-result每次等待30分钟，超时继续循环，无有限任务数退出；只显示部分输出，超长结果不作为失败。接入回归需有限超时/次数、明确错误码、整段内容/CRC检查及BAR访问范围检查。
7. **MSI-X不等于任务完成协议**：handler只打印并返回，未定义设备源清除或向量到任务的对应；应用轮询done。测试必须分别核对MSI-X投递、mask/PBA及实际数据完成，不能靠日志一次出现判定业务完成。

sysfs路径虽较绕，app使用的是PCI父设备下class子设备的`device`链接；本次未在实际来宾验证，不能仅凭路径形状判为错误。后续应实际核对目录与权限。

以上列为适配待办，本轮保留工具链提供的源文件原样。

## 4. P1/P2 调整后的开发顺序

1. P1保留stage1实验回归，同时新增host-demo兼容设备配置；明确ID、BAR2/BAR4、MSI-X能力及与内部mailbox/FW IPC的归属。
2. P1准备匹配来宾内核的模块构建环境，处理demo的API/IRQ/hugepage等接入问题；先验收probe、2MiB coherent分配/mmap、多设备区分及MSI-X，不要求此时DMA已完成。
3. P2先用显式传输profile接通file-load/get-result双向搬运，补输入消费完成与有界结果验收，检查非恒等IOVA和IOMMU fault。由哪类引擎承担必须可追踪；不得用socket拷贝Host内存绕过QEMU DMA服务。
4. HDMA和NPU指令DMA仍各有独立状态机/完成/错误验收；host-demo不选择两者，不因此删除原P2两引擎矩阵。无HDMA启动的NPU外部DMA、P2P、八卡业务继续按原计划实现。

P0仅更新参考清单、接口表和验收交接，不重新打开“等待runtime镜像/Host驱动”门槛。

## 5. 本次验证与输入固定

在当前x86_64宿主执行：

```bash
gcc -std=gnu11 -O2 -Wall -Wextra -o qemu/zc_simulator/p0/evidence/host-demo-review/ssv_test_app simulator/host-demo/ssv_test_app.c
qemu/zc_simulator/p0/evidence/host-demo-review/ssv_test_app --help
```

编译及help均退出0；一个`print_buffer`未使用warning。未运行设备命令，未构建/加载内核模块：当前容器无`/lib/modules`匹配构建环境；上传也不含模块构建文件。无需为此重新编译simu，原单die回归证据仍有效。

输入SHA256：

- app：`59c52141f2c817b379d841dbe7aba586e6a05253c7ebec770473ee65be14c799`。
- driver：`4f5766008810882651822048b908b7e8c39d177891e750561da8c799cba7400e`。

本机编译产物和日志位于已忽略的 `p0/evidence/host-demo-review/`。
