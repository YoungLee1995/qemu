> 扩展实现与运行接口见 [P2扩展实现说明](P2扩展实现说明.md)，本次结果见 [P2执行与验收记录](P2执行与验收记录.md)。已增加 MCU ISR、交易失败后同进程重试、inbound 和文件 CRC/回读；完整硬件 P2 不等同当前功能配置通过。

# P2 单卡 PCIe Host DMA 首轮实现

本目录实现并验收 P2 公共访问服务、HDMA 功能子集、原 NPU DMA 外部访存和四种输入输出组合。完整 P2 仍按 [执行与验收记录](P2执行与验收记录.md) 的剩余项逐项关闭；测试始终分别输出 `p2_first_implementation_pass` 和 `p2_full_exit`。

## 构建及运行

从 `/workspace/GIT/pcie_qemu` 执行：

```bash
python3 qemu/zc_simulator/p2/run_all.py
```

该入口顺序构建当前 QEMU、Spike 和既有 firmware，运行 HDMA/IOMMU 专项、真实 MCU/NPU 专项、Linux 原驱动联合测试、完整 P1 七组回归。已有最新构建时可用 `--skip-build`；测试 runtime 和来宾测试仍重建。依赖继承 P1，首次 Host 模块构建可能下载已固定 SHA256 的 Alpine 内核头等包。不要在验收运行中重链接 QEMU/Spike 或修改被固定的输入。

独立调试（均依赖最新 QEMU/Spike 构建）：

```bash
python3 qemu/zc_simulator/p2/run_hdma.py
python3 qemu/zc_simulator/p2/run_spike.py
python3 qemu/zc_simulator/p2/run_linux.py
```

日志、配置、实际命令、SHA256 manifest 和结果位于忽略的 `p2/evidence/`。测试失败抛异常、返回非零并保留结果，不把最后一条日志当作通过依据。生产 runtime 和两个原 demo 均保持原文件；`runtime/` 是可重复的独立测试夹具。

## 启用接口

- QEMU EP 增加 `p2=on`；默认关闭，保持 P1 接口。`p2-trace=on` 将每笔 Host 访问的 JSON 记录写入 QEMU stderr，runner保存在日志。
- 后端服务沿用 `simulator/tools/p1/service.py`，固定配置 JSON 添加 **`"p2": true`**；服务为 Spike 显式设置 `ZC_P2_PCIE=1`。默认显式设为0，避免误继承宿主环境。
- 会话 HELLO 增加 capability bit4，完整 caps 从15变为31；只有两端配置一致才接入。JSON摘要包含该开关，仍校验输入内容。协议帧版本仍为1。
- Spike在 `ZC_P2_PCIE=1` 时通过既有共享 mailbox FD 的保留空间建立同步访存代理；不重新启动进程来处理普通任务。
- BAR0 的 `+0x300000` 和 `+0x380000` 是本轮调试镜像，分别访问同一份 outbound iATU/HDMA 状态；镜像位置不代表已确认硬件 inbound BAR0 窗口。
- MCU内部访问 `0x21300000..0x21303fff`、`0x21380000..0x21380fff` 代理到同一状态。地址来自 Zebu demo `PCIE0_UNROLL_IATU_BASE/PCIE0_HDMA_BASE`。
- 外部访问仅识别显式设备窗口 `[0x18000000000,0x20000000000)`，窗口基址来自 demo 的 `PCIE0__BASE`。必须命中 enabled outbound region；本地未命中不自动转成 PCIe。

寄存器、共享代理格式、完成和复位约束见 [接口说明](P2接口与运行流程.md)。首轮实际证据与未完成内容见 [验收记录](P2执行与验收记录.md)。
