# P5 Linux 的 simu 二进制运行包

本目录由 `p5/run_linux.py` 管理，用于随 QEMU 项目交付双卡/八卡 Linux 多卡互访实验，不包含 simu C/C++ 模型源码。包中保留 Python 生命周期/协议/fabric 服务。

首次使用见 [克隆后构建与运行简易指南](../完整构建与运行指南.md)。

## 生成与使用

所有命令在克隆的 QEMU 仓库根目录运行，不需要同级 simulator 或 experiments 项目：

```bash
# 默认校验并使用随 Git 发布的运行包
python3 zc_simulator/p5/run_linux.py --cards 2
python3 zc_simulator/p5/run_linux.py --cards 8
# 只校验，不启动 QEMU
python3 zc_simulator/p5/run_linux.py --prepare-only
# 修改本仓库 MCU/NPU 或来宾 C 测试后才执行（需要交叉编译器）
python3 zc_simulator/p5/run_linux.py --runtime source --prepare-only
```

`auto` 和 `prebuilt` 均使用 current，不扫描同级源码。`source` 从仓库 tools/p5/runtime、p4/guest-cluster.c、p5/guest-communication.c 重建 runtime 和来宾程序，使用 tools/p1、tools/p5 的服务源码，复用包内 Spike、firmware、Linux 内核和适配驱动。失败不切换 current。

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

1. 在开发机运行 `--runtime prebuilt --prepare-only`，确认已有 `simu/current` 完整；重建需显式加 `--runtime source`。
2. 交付 QEMU 项目目录，**包含 `zc_simulator/simu/current` 和它指向的 `runtime-*` 目录**。使用保留软链接和执行权限的 tar 等方式。
3. 对方先正常配置/编译本项目 QEMU，得到 `<QEMU目录>/build/qemu-system-x86_64`。
4. 对方在任意工作目录运行：

```bash
python3 /对方路径/QEMU目录/zc_simulator/p5/run_linux.py --cards 2
python3 /对方路径/QEMU目录/zc_simulator/p5/run_linux.py --cards 8
```

程序按脚本位置寻找 QEMU 和运行包，QEMU 目录不要求命名为 `qemu`。每轮证据保存到该项目 `zc_simulator/p5/evidence/linux-*`，需要目录可写。

**当前已发布的运行包和 `current` 链接随 Git 仓库交付，克隆后无需另取压缩包。** 其他本地历史/新构建版本默认仍 ignore。以后重新生成包并准备发布时，必须同时提交新的目标目录和链接，不能只提交链接：

```bash
# 在 QEMU 仓库根目录执行；仅在确认要发布新包时使用
simu_release=$(readlink zc_simulator/simu/current)
git add -f "zc_simulator/simu/$simu_release" zc_simulator/simu/current
```

将 `simu/.gitignore` 中的已发布版本例外同步为新目录名即可使后续普通修改可跟踪。也可继续使用 tar 交付整个 `simu/` 目录。运行包摘要应在提交前通过 `run_linux.py --runtime prebuilt --prepare-only` 校验。

## 运行环境和范围

- Linux x86-64、Python 3、memfd/Unix socket 和 `/proc`，使用普通本地运行环境。
- Spike 仍动态依赖宿主 glibc/libstdc++/OpenMP、Boost Regex 1.74、ICU 70 等；以 `host-dependencies.txt` 和接收机 `ldd bin/spike` 为准。二进制包不是跨所有发行版通用的静态程序。
- 对方可自行编译 QEMU；simu 不需要重新编译。QEMU 自身的 BIOS/ROM 等资源仍按其正常构建规则准备。
- 当前独立入口覆盖 P5 Linux 双/八卡、Tensor fixtures、协议、链路校准、通信、IRQ 和 fabric 故障恢复，完整入口见主手册。历史 P0–P4 源码门禁不属于新克隆操作入口。
- 图形桌面和交互式命令行见统一教程《克隆后构建与运行简易指南.md》；外部 Ubuntu 镜像按说明下载，不捆绑大型系统盘。

实现与本次验证见 [二进制交付实现与验证记录](../p5/P5二进制交付实现与验证记录.md)。
