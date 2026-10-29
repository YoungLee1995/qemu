# P5 Linux 的 simu 二进制运行包

本目录由 `p5/run_linux.py` 管理，用于随 QEMU 项目交付双卡/八卡 Linux 多卡互访实验，不包含 simu C/C++ 模型源码。包中保留 Python 生命周期/协议/fabric 服务。

## 生成与使用

在完整工作区根目录运行：

```bash
# 默认自动模式：检测到 simulator/riscv-isa-sim/spike_main/spike.cc 就编译并更新运行包，然后运行
python3 qemu/zc_simulator/p5/run_linux.py --cards 2

# 只编译、打包和校验，不启动 QEMU；会同时准备双卡及八卡镜像
python3 qemu/zc_simulator/p5/run_linux.py --prepare-only

# 即使本机有源码也可强制使用已有包
python3 qemu/zc_simulator/p5/run_linux.py --runtime prebuilt --cards 8
```

源码模式编译 Spike、firmware、P5 MCU/NPU runtime、适配 Host 驱动及双卡/八卡来宾程序；把内核、initramfs、配置和服务一并打包。**编译失败直接报错，不静默退回旧包。** 源码模式仍需要现有构建目录、交叉编译器、stage1 内核/rootfs 等输入，Host 模块首次构建可能联网；具体见上级使用手册。

无 simulator 源码时，默认自动使用 `current` 指向的已有包。预编译模式无需 simulator、experiments 目录，无需 RISC-V 编译器或 Host 模块编译；会校验包摘要，并使用本次安装路径重新生成 backend 配置/哈希，支持移动安装目录。

源码存在时自动模式每次都会执行构建命令；增量 make 决定是否重新编译。修改来宾测试或集成 runtime 后需重新生成包；预编译模式执行的始终是已打包的版本。

## 目录

```text
simu/
├── README.md
├── current -> runtime-时间戳
└── runtime-时间戳/
    ├── manifest.json
    ├── host-dependencies.txt
    ├── bin/spike
    ├── bin/sm_system.cfg
    ├── images/firmware.riscv
    ├── images/mcu.bin
    ├── images/npu.bin
    ├── service/service.py
    ├── service/protocol.py
    ├── fabric/fabric_service.py
    ├── fabric/fabric.py
    ├── fabric/gen4-eight.json
    ├── guest/vmlinuz-lts
    ├── guest/initramfs-2.cpio.gz
    └── guest/initramfs-8.cpio.gz
```

`spike` 复制后仅删除调试信息，不修改原始构建产物。每次构建生成新版本，校验成功后原子切换 `current`，失败保留旧包；旧版本不自动删除，确认无人运行后可自行清理。`manifest.json` 对运行载荷逐文件记录 SHA256；缺件、改动或无执行权限均在启动前报错。

## 交付 QEMU 项目

1. 在开发机运行 `--prepare-only`，确认成功生成 `simu/current`。
2. 交付 QEMU 项目目录，**包含 `zc_simulator/simu/current` 和它指向的 `runtime-*` 目录**。使用保留软链接和执行权限的 tar 等方式。
3. 对方先正常配置/编译本项目 QEMU，得到 `<QEMU目录>/build/qemu-system-x86_64`。
4. 对方在任意工作目录运行：

```bash
python3 /对方路径/QEMU目录/zc_simulator/p5/run_linux.py --cards 2
python3 /对方路径/QEMU目录/zc_simulator/p5/run_linux.py --cards 8
```

程序按脚本位置寻找 QEMU 和运行包，QEMU 目录不要求命名为 `qemu`。每轮证据保存到该项目 `zc_simulator/p5/evidence/linux-*`，需要目录可写。

**生成载荷已 ignore，普通 `git add . && git push` 不会上传它。** 如果通过 Git 交付源码，应额外交付运行包，例如在开发机工作区根目录执行：

```bash
tar -C qemu/zc_simulator -czf /tmp/zc-simu-runtime.tar.gz simu
```

对方解压到其 QEMU 项目的 `zc_simulator/`。这会包含本地所有保留的 runtime 版本；交付前可只保留使用中的版本。不要只传 `current` 链接，也不要只传 README。

## 运行环境和范围

- Linux x86-64、Python 3、memfd/Unix socket 和 `/proc`，使用普通本地运行环境。
- Spike 仍动态依赖宿主 glibc/libstdc++/OpenMP、Boost Regex 1.74、ICU 70 等；以 `host-dependencies.txt` 和接收机 `ldd bin/spike` 为准。二进制包不是跨所有发行版通用的静态程序。
- 对方可自行编译 QEMU；simu 不需要重新编译。QEMU 自身的 BIOS/ROM 等资源仍按其正常构建规则准备。
- 本次无源码运行支持 **P5 `run_linux.py` 双卡/八卡入口**，保留其 runtime、四 DMA 组合、Host/peer 搬运、文件 IPC、IRQ、软件通信队列及 P5 fabric 功能。不是把 P0–P5 所有历史构建/验收脚本改成了无源码工具；如 `run_tasks.py` 的任务生成输入及独立 Tensor golden 未打入该 Linux 包。
- 自动实验运行完成后关机，不新增交互式 Linux shell，也不新增任意用户程序加载接口。

实现与本次验证见 [二进制交付实现与验证记录](../p5/P5二进制交付实现与验证记录.md)。
