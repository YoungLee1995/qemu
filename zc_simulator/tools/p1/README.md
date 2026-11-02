# P1 单卡后端服务

这是内部软件功能 profile v1 的常驻服务。QEMU 持有共享 DDR 与 PCIe function，服务接收 FD、验证配置、启动和回收 Spike；共享 mailbox/ECLIC 继续由模型线程推进。没有改变业务 runtime 的指令或 NPU 本地访存行为。

```bash
python3 simulator/tools/p1/service.py \
  --socket /tmp/zc-card0.sock \
  --config /absolute/path/backend.json \
  --work /absolute/path/backend-logs
```

配置 JSON 的 `spike/firmware/cfg/cwd` 为绝对路径，`isa` 为现有 ISA 配置，`sha256` 是所有镜像、配置、输入文件及 Spike 的内容哈希表。服务打印的摘要是整个 JSON 原始字节的 SHA256；QEMU 用 `config-sha256` 校验同一配置。自动验收生成配置样例，入口见 [P1 文档](../../../qemu/zc_simulator/p1/README.md)。应使用固定路径和内容，不在服务运行中编辑输入。

QEMU 参数：

```text
-device zc-pcie-p1,backend-socket=/tmp/zc-card0.sock,config-sha256=<64位hex>,card-id=0
```

`--card` 默认 0，必须与 QEMU 的 `card-id` 一致。一个监听服务同时只允许一个 QEMU 会话拥有该卡，其他连接立即关闭。监听路径已存在时拒绝启动；正常退出删除自己的 socket，SIGKILL 后需确认旧进程已退出再删除遗留路径。可启动独立路径/独立 card 的服务，但本轮只验收单卡业务链路。

## 协议与状态

`protocol.py` 定义 48 字节小端帧头：magic/version/type/payload_len/card/session/epoch/request/flags/reserved，最大 payload 64KiB。request 单调递增非零；事件 request=0/flags=1；普通应答与请求对应。未知版本、身份、保留位、长度、重复请求、未协商能力、错误状态转移均失败。完整帧一次 seqpacket 发送，不截断后执行。

当前 capabilities=15，只支持共享 DDR、共享 mailbox、reset、stop。消息为 HELLO/ACK、MEM_SETUP/READY、START/SOFTWARE_READY、PING/PONG、QUIESCE/QUIESCED、RESET/ACK、STOP/STOPPED、ERROR。MEM_SETUP 必须带三个独立 sealed FD，区域描述符 `<QQII>` 指定 base/size/FD index/flags；精确检查大小、顺序、索引和禁止 resize 的 seals。映射失败关闭已接收 FD。映射成功后仍需 START 和实际 MCU/NPU ready，不能以连接成功表示软件 ready。

`HELLO → MEMORY → MEMORY_READY → STARTING → READY`；QUIESCE 回收旧进程，返回 outstanding=0，才允许新 epoch RESET/START。STOP 与任务完成分开。模型正常 STOP 发事件；异常退出、超时、断连先停止并回收旧进程，再释放映射和会话。版本/配置不匹配不启动 Spike。连接内握手阶段有期限、ready 默认 20秒、ready 后30秒未收到 heartbeat 会终止会话；QEMU 每秒 heartbeat、默认请求超时30秒，均有有限等待。

QEMU 非阻塞 socket 收发在设备计时器回调内完成，不在 BAR read/write 等待 RPC。服务只管理进程和生命周期字段，不直接触碰 hart/ECLIC。SPIKE 的 `ZC_P1_PARENT_PID` 可选 guard 使用 Linux `PR_SET_PDEATHSIG`，父服务异常退出会终止 Spike；未设置时保留原独立启动路径。QEMU 断开后的新会话分配全新 backing；同会话 reset 复用 backing 前必须已回收旧进程。

REG RPC、HDMA/NPU 外部 DMA、引擎 continuation 等能力本轮没有协商，收到未知操作会拒绝，按 P2 实施。当前 PCIe/DDR映射是明确的软件 profile，不能冒充已确认的硬件 BAR 地址布局。

## 可选 P2 访问代理

配置JSON添加布尔 `"p2": true`，并在QEMU同一function添加`p2=on`，HELLO额外校验capability bit4。服务显式设置后端`ZC_P2_PCIE=1`，未启用时设置0。P2代理通过既有mailbox FD的保留槽访问QEMU控制器/Host DMA服务；普通任务复用同一Spike，失败目前会话级终止，完整语义与验证见 [P2文档](../../p2/README.md) 和 本仓库 P2 接口实现。
