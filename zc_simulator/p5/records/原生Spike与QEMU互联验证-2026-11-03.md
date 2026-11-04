# 原生 Spike 与 QEMU 互联验证（2026-11-03）

本次实际执行，工作区未提交。使用教程 1.6 的单卡 Ubuntu 原生命令；测试隔离使用 `/tmp/zc-tutorial-fresh-20261103/fixed-state` 系统盘、SSH 2226/RDP 13393，未启动默认 state 的桌面。

## 实现与交付

已直接修改开发机 `simulator/riscv-isa-sim/spike_main/spike.cc`，接入新增 `spike_pcie.h`。QEMU 正式目录 `zc_simulator/native/` 保存同一扩展头、幂等源码安装器、原生协议回归及 Host 测试源码。随 QEMU 交付的 `simu/current/bin/spike` 已重新编译并更新 manifest，普通运行不访问同级 simulator 源码。

新增参数：`--pcie-socket PATH`、`--pcie-config-sha256 HEX`、`--pcie-card N`、`--pcie-log FILE`。原生入口自行监听、继承 QEMU FD、启动同 ELF 模型子进程并响应心跳；不调用 Python 服务。配置摘要仅作两端身份，不是镜像签名。设备业务 ready 仍属于 Host/SCU/IPC 协议。

## 已执行验证

- 源码 `make -C simulator/riscv-isa-sim/build -j4 spike` 成功；增量编译日志 `/tmp/zc-native-spike-build.log`。
- Spike 先监听、QEMU 后启动：Ubuntu 内 `native/host-test.c` 连续三笔 1024 字节任务，所有结果校验通过。Host 实际操作 BAR0 inbound 与 BAR4 DDR/任务描述符，MCU/NPU 实际执行；三笔任务使用同一模型 PID。日志 `/tmp/zc-native-e2e/host-test.log`。
- QEMU 先启动，Spike 后接入：自动重连并完成相同三笔 Host 任务。日志 `/tmp/zc-native-e2e/qemu-first-host-test.log`。
- 最终发布 ELF 执行 `python3 zc_simulator/native/tests/test_session.py` 通过：坏帧、摘要不匹配、重复监听保护、真实 memfd/SCM_RIGHTS、心跳、quiesce/reset/start/stop、模型回收、socket 清理。
- `python3 zc_simulator/p5/run_linux.py --cards 2` 全部通过，含每卡单常驻 Spike。证据 `../evidence/linux-2-1793700102158064351/`。
- 最终 ELF 的发布包校验通过；transport、reload、PTY 测试通过；两个仓库 `git diff --check` 通过。

测试中修正过 Host 程序的字节 MMIO：模型要求 32 位 BAR 访问，最初逐字节写入导致结果失败，改用 32 位访问后通过。一次从错误 cwd 启动的模型无法找到相对 BIN，改为 cfg 所在目录启动；教程明确该要求。

测试 Ubuntu 已正常关机，原生监听已退出，未留下测试模型进程。

## 范围

本次 native 章节验证单卡单 die、默认 P5 MCU/NPU 本地 DDR 任务。Host 内存 DMA、多卡 fabric、自定义 BootROM 协议、native 入口 `-d` 未由本次测试覆盖。已有 Python backend 调试路径保留；不声称本节替代完整多卡管理平台。
