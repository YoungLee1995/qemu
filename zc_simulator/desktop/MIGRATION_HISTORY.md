> 历史记录：文中的原开发机路径不作为当前操作入口。现行启动、构建和源码位置以 [README.md](README.md)、[操作教程.md](操作教程.md) 为准；当前使用不需要同级 simulator 仓库。

# 历史实验与迁移记录

本文保留原环境记录；当前目录结构及操作以 [README.md](README.md) 为准。文中的“本目录”在旧记录中指运行数据目录，不是源码目录。

# Ubuntu 24.04 八卡桌面实验

此目录是本地实验，位于 Git 忽略的 evidence 下。原 P5 runner 和已发布 simu 包未修改。

架构：当前项目 QEMU（TCG、q35）运行 Ubuntu 24.04，保留单 Switch 八卡及每卡独立 Spike 后端、Intel IOMMU、P5 fabric。Ubuntu 内运行 XFCE 和 xrdp，通过 RDP 操作桌面，无需 QEMU 的 VNC/GTK 显示后端。

## 启动和停止

在 QEMU 仓库根目录执行。系统盘已准备后，每次只启动一个实例：

```sh
python3 zc_simulator/desktop/desktop.py --cards 8
```

默认只监听当前运行环境的 127.0.0.1，SSH 为 2222，RDP 为 13389。启动器前台运行，另开终端操作。

如果 QEMU 运行在 Docker 容器内，需要让 Docker 宿主机能够连接容器端口，可以指定容器的 bridge IP，例如：

```sh
python3 zc_simulator/desktop/desktop.py --cards 8 --bind 172.17.0.6
```

这是本次检查时的容器 IP；容器重建后应重新检查。默认 1/2/8 卡选项复用同一系统盘，已有八卡测试程序只在八卡模式使用。

查看状态和正常关机：

```sh
python3 zc_simulator/desktop/control.py status
python3 zc_simulator/desktop/control.py stop
```

来宾执行 reboot 也会使当前 QEMU 退出（启动器使用 `-no-reboot`），需要重新运行启动命令。

正常关机后，启动器自动回收 QEMU、service、Spike 和 fabric。系统盘内的软件和文件保留。Ctrl-C 会强制结束 QEMU，优先使用上述正常关机命令。

## 连接桌面

从自己的电脑建立 SSH 隧道，保持该窗口打开。

QEMU 直接运行在 SSH 服务器上、使用默认监听地址时：

```sh
ssh -N -L 13389:127.0.0.1:13389 用户名@服务器
```

本次环境已使用 `--bind 172.17.0.6` 启动。从自己的电脑通过 SSH 入口 `10.28.10.101` 转发（该服务器需要能访问此 Docker 容器）：

```sh
ssh -N -L 13389:172.17.0.6:13389 你的SSH用户名@10.28.10.101
```

在电脑上用 Windows 远程桌面、Windows App 或 Remmina 连接 `127.0.0.1:13389`，xrdp 登录类型选择 Xorg，用户为 `t100`，密码见本目录 `credentials.json`。凭据文件为本地生成，权限 0600。在当前工作区终端执行以下命令查看：

```sh
cat zc_simulator/p5/evidence/desktop-dev/credentials.json
```

只填写来宾账号 `t100`，SSH 隧道中的用户名则填写你登录服务器的账号。如果远程桌面提示证书，请核对目标是这个本地隧道；当前 xrdp 使用实验环境自签名证书。SSH 隧道的端到端连接需要在你的电脑执行，上述地址按提供的服务器入口填写。

## 在桌面中使用

- 终端执行 `lspci -nn -d 16c3:abcd`，查看当前挂接的设备；数据库中显示的 Synopsys 名称来自 VID/DID，并不表示另一块实际设备。
- `ls -l /dev/test_dma_*` 查看 demo 驱动创建的节点。
- 桌面 `PCIe devices` 快捷方式显示 PCIe 设备和节点。
- 桌面 `Eight-card DMA and P2P test` 快捷方式执行基础八卡测试。首次打开 .desktop 文件可能需要允许启动。
- 也可在终端执行 `sudo /opt/t100/guest-cluster`。它检查 32 个 Host 任务、224 个 peer 任务和文件 IPC。
- 包内完整 P5 guest 测试位于 `/mnt/hostshare/p5-guest-test-8`，执行 `sudo /mnt/hostshare/p5-guest-test-8`；同一时刻只运行一个占用任务区/HDMA 的测试。

共享目录对应关系：

```text
运行 QEMU 的环境：本目录/share/
Ubuntu 来宾：    /mnt/hostshare/
```

把 Host 程序、image、配置放到 `share/`，即可从来宾访问。共享区文件通常由宿主 root 拥有，在来宾可以用 sudo 拷贝到 `/home/t100/`，再按需要调整属主。

当前 demo 驱动已按 Ubuntu 内核重新构建。自己的驱动需要按来宾 `uname -r` 对应内核构建；Host 应用需为 x86-64 Linux，并准备所需依赖。

## 实验边界

- 设备侧仍运行发布包内的集成 MCU/NPU runtime，没有替换成用户 BootROM。
- 图形桌面为 Linux 通用操作环境；尚未安装用户自有的图形化下载工具或业务程序。
- 没有 /dev/kvm 时使用 TCG；桌面和编译速度不代表实机性能。
- `ubuntu-24.04-desktop.qcow2` 使用本目录 base 镜像作为 backing file，移动时需保留 base 并调整 backing 路径。
- `runs/` 保存每次启动的命令、日志、backend/fabric 配置；`active.json` 记录当前或最后一次进程状态。
- 首次安装过程记录在来宾 `/var/log/t100-desktop-setup.log`；`/var/lib/t100-desktop-ready` 表示安装、驱动构建均已完成。

## 本次实测记录（2026-10-30）

- Ubuntu 24.04.5，Linux 6.8.0-147-generic，八卡枚举、驱动自动加载和 8 个 `/dev/test_dma_*` 节点正常。
- 来宾内核已启用 `intel_iommu=on iommu.strict=1 pci=realloc=on`，DMAR/IOMMU 和中断重映射启用。
- 完整 P5 测试退出 0：32 个 Host DMA 任务、224 个 peer 任务（56 个有向卡对 × 4 种模式）、8 卡文件 IPC/MSI-X；异步 C 通信库连续 3 轮，每轮 56 对通过。详见 `p5-ubuntu-validation.log`。
- 桌面基础测试程序在完整 P5 测试后连续运行两次，每次上述 32/224/8 项通过。详见 `desktop-repeat-validation.log`。同一时刻只运行一个测试程序。
- 使用 RDP 客户端完成密码认证、Xorg 会话和 XFCE 桌面显示验证。测试客户端已断开，来宾保持运行，用户可重新连接。
- 重启后 `cloud-init status --long` 为 done、errors 为空，DNS 解析正常。

这些测试确认当前 demo 平台上的运行结果；自有 BootROM、固件、驱动和应用需要分别移植与验证。

## 自定义任务

共享目录新增 `my-task.c` 和 `CUSTOM_TASK.md`，可在 Ubuntu 内复制到自己的目录并编译。它复用现有任务通道，支持选卡、长度、输入 seed 和四种 DMA 模式，设备运算仍为现有 XOR 0x5a。详见 `/mnt/hostshare/CUSTOM_TASK.md`。验证日志为 `my-task-validation.log`。

## 迁入当前工作区（2026-11-02）

完整桌面实验目录已从 `/data/simulator/qemu` 复制到当前 QEMU 仓库；系统盘 backing file 已改为同目录的相对路径 `ubuntu-24.04-base.qcow2`。原目录未修改。上面的实测记录为同事原环境结果，迁移后尚未重新启动桌面验证。`runs/` 和 `active.json` 保留原始历史记录，启动后会生成新的运行状态。此目录仍受 evidence 忽略规则影响，不会自动进入 Git 提交。

## 当前工作区跑通记录（2026-11-02）

已修复长工作区路径导致串口/QMP Unix socket 超过 108 字节的问题，socket 改放独立临时目录，退出时自动清理。

已启动八卡 Ubuntu 桌面，当前 Docker 容器监听地址为 `172.17.0.62`，SSH 2222，RDP 13389。重新启动命令（先确认容器 IP）：

```sh
python3 zc_simulator/desktop/desktop.py --cards 8 --bind 172.17.0.62
```

在自己的电脑执行（服务器需能访问该容器）：

```sh
ssh -N -L 13389:172.17.0.62:13389 你的SSH用户名@服务器地址
```

再用远程桌面连接 `127.0.0.1:13389`，选择 Xorg，用户名 `t100`，密码在本目录 `credentials.json`。已实际完成 RDP 登录并看到 XFCE 桌面，验证客户端已断开，虚拟机保持运行。

验证结果：8 个 PCIe 设备、8 个驱动节点、xrdp active；完整 P5 测试 32 Host DMA / 224 peer / 8 file IPC 通过，异步通信连续 3 轮各 56 对通过。桌面基础 guest-cluster 测试亦通过。日志：`migration-p5-validation.log`、`migration-desktop-validation.log`。
