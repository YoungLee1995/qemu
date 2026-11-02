# Ubuntu 八卡桌面 Demo

源码位于 `zc_simulator/desktop/`，可通过 Git 管理。QEMU 使用 q35/TCG 运行 Ubuntu 24.04，接入 P5 单 Switch 八卡后端；Ubuntu 提供 XFCE 桌面、xrdp 和交互命令行。

## 目录与依赖

| 路径 | 内容 |
| --- | --- |
| `desktop.py` | 启动 1/2/8 卡 Ubuntu，管理 QEMU、后端和 fabric 生命周期 |
| `control.py` | 状态检查、SSH 命令、正常关机 |
| `terminal.py` | 自动连接运行中的 Ubuntu 交互命令行 |
| `web_terminal.py` | 将命令行提供为浏览器终端，默认仅监听本机 7681 |
| `prepare.py` | 校验基础镜像，创建系统盘、密钥、cloud-init 和共享文件 |
| `paths.py` | 统一源码、仓库与本地运行数据路径 |
| `share/` | 驱动源码、自定义 Host 任务、使用文档 |
| `MIGRATION_HISTORY.md` | 同事原环境及迁移验证记录 |

默认运行数据目录为 `zc_simulator/desktop/state/`。新克隆按下面流程创建自己的数据目录。系统盘、凭据、密钥、seed、下载工具、日志和进程状态留在此目录，受 Git 忽略规则保护。可以用环境变量 `ZC_DESKTOP_STATE` 指定其他绝对路径，所有入口须使用同一配置。

需要已经构建的 `build/qemu-system-x86_64`、有效的 `zc_simulator/simu/current` 发布包以及 SSH 客户端。浏览器终端需要 PATH 中的 `ttyd`，或数据目录下的 `tools/local/ttyd`。这些外部依赖由使用者下载或编译，不纳入桌面源码目录。

## 新克隆后的准备流程

正式目录提供自己开发的源码和文档。QEMU、Ubuntu 镜像、ttyd、系统软件包均通过构建或下载获得，生成的系统盘、账号和日志保存在 `state/`，不需要原开发机的 evidence 文件。

### 0. 已有环境先检查，不要重复初始化或启动

如果这台机器之前已经启动过桌面，先执行：

```sh
python3 zc_simulator/desktop/control.py status
```

若能连接并显示 `T100_DESKTOP_READY`，直接运行 `python3 zc_simulator/desktop/terminal.py`，无需再执行 `prepare.py` 或 `desktop.py`。首次克隆尚无 `active.json` 时，继续第 1 步。

若 `prepare.py` 或 `desktop.py` 报 `BlockingIOError: [Errno 11] Resource temporarily unavailable`（新版会提示“桌面数据目录正在使用”），表示另一个桌面/初始化进程持有同一个 `desktop.lock`。这不是缺少文件，也不是依赖安装失败。优先检查并连接已有桌面；正在初始化时等待它完成。

确需修改共享文件、重新执行初始化或重启时，在宿主终端执行：

```sh
python3 zc_simulator/desktop/control.py stop
```

等待原来的 `desktop.py` 退出后，再执行 `prepare.py` 和启动命令。**不要删除 `desktop.lock` 绕过锁**：锁由活进程持有，文件留在磁盘上本身不会造成占用。若状态检查失败，用 `lslocks -o PID,COMMAND,PATH` 查看锁占用者，结合其进程和日志排查，不要直接重复启动。

### 1. 确认仓库与 QEMU 已就绪

所有宿主命令都在 **QEMU 仓库根目录**执行，也就是能看到 `zc_simulator/` 和 `build/` 的目录。仓库可以叫 `pcie_qemu`，不必改名为 `qemu`。终端 A、B 都必须进入这个目录；这些命令不是在 Ubuntu 来宾中运行。

按 [克隆后构建与运行简易指南](../克隆后构建与运行简易指南.md) 准备 Ubuntu 22.04 x86-64 环境、P5 运行依赖并构建本项目 QEMU。已有的 P5 发布包位于 `zc_simulator/simu/current`，它属于原项目交付内容。
如果双卡、八卡 `run_linux.py` 已通过，可以跳过 QEMU 构建，但仍要继续下面的 Ubuntu 镜像准备。自动测试结束就关机，不会创建 Ubuntu 桌面，也不会生成桌面连接记录。

```sh
pwd
test -x build/qemu-system-x86_64 && echo QEMU_OK
test -d zc_simulator/simu/current && echo BUNDLE_OK
```

### 2. 安装镜像与 SSH 工具

下面以普通用户为例使用 `sudo`；若已是 root 且没有 sudo，去掉命令前的 `sudo`。

```sh
sudo apt update
sudo apt install -y qemu-utils genisoimage openssh-client openssl curl ca-certificates
```

### 3. 下载 Ubuntu 镜像

首次使用下载 Ubuntu 官方 cloud image 与对应校验清单，保留官方文件名以校验；已有正确的基础镜像时不必重新下载。以下示例使用默认 `state/`，不要同时设置指向其他位置的 `ZC_DESKTOP_STATE`。

```sh
mkdir -p zc_simulator/desktop/state
cd zc_simulator/desktop/state
curl -fLO https://cloud-images.ubuntu.com/noble/current/SHA256SUMS
curl -fLO https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img
sha256sum --ignore-missing -c SHA256SUMS
mv noble-server-cloudimg-amd64.img ubuntu-24.04-base.qcow2
cd ../../..
```

官方 `current` 镜像会更新，建议在镜像下载前后核对清单，确保二者匹配。`prepare.py` 使用下载到数据目录中的官方清单校验镜像；清单不随源码提交。

### 4. 初始化本地系统盘

在仓库根目录运行；此时不要有使用同一系统盘的 VM：

```sh
python3 zc_simulator/desktop/prepare.py
```

`prepare.py` 使用系统 OpenSSL 生成密码摘要，运行脚本可使用 Python 3.10+；QEMU 构建使用 Python 3.12+。初始化生成本地密钥、随机密码、系统盘和 cloud-init。首次 Ubuntu 启动需要联网安装 XFCE、xrdp、内核头文件，编译本目录的驱动源码和仓库 `p4/guest-cluster.c`。这些安装工作发生在下一步 Ubuntu 启动之后；`prepare.py` 完成不代表 Ubuntu 已经运行。

桌面基础八卡测试程序会在初始化时编译，无需复制旧 evidence 中的二进制。文档提到的完整 `p5-guest-test-8` 属于历史测试产物，不是桌面启动所必需，也不会由这里的初始化脚本生成。

### 5. 终端 A：启动 Ubuntu

```sh
python3 zc_simulator/desktop/desktop.py --cards 8
```

此命令启动 QEMU、Spike 后端和 fabric，并生成 `state/active.json`，输出中包含 `qemu_pid`、`work`、SSH/RDP 端口。它默认在终端 A 前台运行；不要等它退出后才执行下一步。可以另开终端，也可以关闭启动终端，虚拟机继续运行。Ctrl+Z 会被忽略；Ctrl+C 仍会终止启动器，正常关机请使用 `control.py stop`。它的串口写入日志，不会在终端 A 显示 Ubuntu 登录 shell。

启动器会自动轮询真实 SSH 会话、就绪标记、指定数量的驱动节点和 xrdp 服务。等待期间每 30 秒提示一次；全部通过后，终端 A 明确打印：

```text
DESKTOP_READY：Ubuntu 已就绪（SSH、8 个设备节点、xrdp）。
请在另一个宿主终端执行：
  python3 zc_simulator/desktop/terminal.py
可关闭当前终端，虚拟机继续运行。
```

看到这条提示后即可登录。仅打印 PID/端口的 JSON 不代表就绪。检查失败不会关闭 VM，启动器会继续等待；SSH 检查输出保存在本次 `work/readiness.log`。该提示是启动阶段检查结果，之后仍可用 `control.py status` 检查实时状态。若使用自定义 `ZC_DESKTOP_STATE`，登录终端也必须设置相同值。

### 6. 终端 B：等待首次初始化完成

打开第二个宿主终端，进入同一个仓库根目录，执行：

```sh
python3 zc_simulator/desktop/control.py status
```

首次启动使用 TCG 并需要联网安装软件，耗时取决于机器和网络。刚启动时可能连接被拒绝、SSH 尚未就绪，或设备节点尚未生成；保持终端 A 运行，稍后重新执行状态检查。

完整就绪应看到 `T100_DESKTOP_READY`、xrdp 的 `active`，以及 `/dev/test_dma_0` 到 `/dev/test_dma_7`。`active.json` 出现仅表示启动记录已写入，不表示系统初始化完成。

若长时间未就绪，在终端 B 查看本次串口日志：

```sh
python3 - <<'PYLOG'
import json
import os
from pathlib import Path
state = Path(os.environ.get('ZC_DESKTOP_STATE', 'zc_simulator/desktop/state'))
active = json.loads((state / 'active.json').read_text())
print(Path(active['work']) / 'serial.log')
PYLOG
```

对打印出的路径运行 `tail -n 100 路径`；SSH 可连接后还可查看来宾初始化日志：

```sh
python3 zc_simulator/desktop/control.py ssh 'sudo tail -100 /var/log/t100-desktop-setup.log'
```

### 7. 终端 B：进入可执行命令的 Ubuntu

```sh
python3 zc_simulator/desktop/terminal.py
```

该命令自动使用本地密钥、账号和转发端口连接 Ubuntu。看到 `t100@t100-eight-card:~$` 后，下面的命令才是在 **Ubuntu 来宾内**执行：

```sh
uname -m
lspci -nn -d 16c3:abcd
ls /dev/test_dma_*
sudo /opt/t100/guest-cluster
```

八卡测试应通过；`guest-cluster` 是八卡测试，本节单卡/双卡启动时不要用它作为验收。输入 `exit` 只断开 SSH，Ubuntu 和终端 A 仍继续运行。

`terminal.py` 只连接已有 VM。若提示找不到 `active.json`，回到第 5 步；不要手工创建该文件，也不要复制别人的运行记录。若终端 A 已退出，检查其错误和 `work` 下的 `qemu.log`，解决后重新启动。

## 后续使用：重新启动、检查和关闭

已经运行时不需要重复启动。检查状态：

```sh
python3 zc_simulator/desktop/control.py status
```

正常关机（通过 QMP 发送 ACPI 电源按钮事件，不依赖 Ubuntu 的 SSH）：

```sh
python3 zc_simulator/desktop/control.py stop
```

命令提示“已发送”表示请求已提交，并不表示关机完成；等待终端 A 的启动进程退出后再操作磁盘。

**Ctrl+Z 和关闭启动终端都不会关机。** 新版启动器忽略 Ctrl+Z；关闭终端后，QEMU、Spike、fabric 和监督进程继续运行，后续启动提示写到本次 `work/supervisor.log`。重新打开终端后可以直接执行 `control.py status`、`terminal.py` 或 `control.py stop`，不要重复启动第二台 VM。

这只处理终端挂断和 Ctrl+Z；停止容器、重启宿主机或强杀进程仍会终止虚拟机。Ctrl+C 仍保留终止启动器的行为，需要正常关机时请使用上面的 `control.py stop`。

更新仅对下次运行的启动器生效。旧版进程按 Ctrl+Z 可能暂停或关机；已暂停时在原终端用 `jobs -l` 和 `fg %作业号` 恢复，再正常关机后启动新版。

关机后等待原启动进程退出（关闭过终端时可检查 `active.json` 中的 `stopped` 为 true）。下次使用不用重新下载镜像或执行 `prepare.py`，直接重新启动（终端 A 前台保持运行），再在终端 B 检查状态并执行 `terminal.py`：

```sh
python3 zc_simulator/desktop/desktop.py --cards 8
```

支持 `--cards 1/2/8`、`--memory 4G`、`--ssh-port 2222`、`--rdp-port 13389`。默认监听 127.0.0.1；Docker 宿主机需要直接访问时，使用 `--bind 容器IP`，容器 IP 可通过 `hostname -I` 查看。串口和 QMP socket 使用短临时目录，避免工作区路径过长。

## 图形桌面

先完成上述就绪检查；`desktop.py` 使用 `-display none`，不会在宿主自动弹出图形窗口，需要通过 RDP 连接 Ubuntu。

在 VS Code「端口 / PORTS」中转发 13389，用电脑上的远程桌面客户端连接其本地转发地址。xrdp 选择 Xorg，用户名 `t100`。密码查看：

```sh
cat zc_simulator/desktop/state/credentials.json
```

自定义数据目录时改用对应路径。桌面中可打开终端、`PCIe devices` 或 `Eight-card DMA and P2P test` 快捷方式。首次使用快捷方式可能需要允许启动。共享目录为数据目录中的 `share/`，来宾挂载在 `/mnt/hostshare/`。

## 可选：浏览器终端

先完成上述启动和就绪检查，再下载上游 ttyd（也可以自行从上游源码编译）：

```sh
mkdir -p zc_simulator/desktop/state/tools/local
curl -fL https://github.com/tsl0922/ttyd/releases/download/1.7.7/ttyd.x86_64 \
  -o zc_simulator/desktop/state/tools/local/ttyd
chmod +x zc_simulator/desktop/state/tools/local/ttyd
python3 zc_simulator/desktop/web_terminal.py
```

运行后浏览器打开 <http://127.0.0.1:7681>。VS Code 远程容器环境在「端口 / PORTS」中转发 7681，再打开本地转发地址。此服务保持前台运行，请另开宿主终端执行；它同样要求 Ubuntu VM 已经启动。

ttyd 项目与构建说明：<https://github.com/tsl0922/ttyd>。浏览器终端不是桌面或命令行启动的必需依赖；`terminal.py` 使用系统 SSH 客户端即可。

修改 `share/my-task.c` 等自定义任务源码后，可重新执行 `prepare.py` 同步共享文件；不要在 VM 运行期间重建它正在使用的 seed 和镜像，请先正常关闭。

## 验证与开发

本仓库 `zc_simulator/host-demo` 可通过 `python3 zc_simulator/desktop/host_demo.py` 在运行中的 VM 内编译并检查八卡，追加 `--interactive` 进入命令行。原版 `pcie-demo` 目前缺少 SDK 与构建文件，未生成替代 MCU；本次文件往返测试超时。具体证据、产物位置和运行方法见 [原版 demo 接入实测](DEMO_VALIDATION.md)。

登录 Ubuntu 后的逐步操作见 [操作教程](操作教程.md)；工具链同事编译 RISC-V 裸机 `mcu.bin` 的目录结构、Makefile、地址约定和加载验证见 [MCU 编译教程](MCU编译教程.md)。

2026-11-02 已完成实际 RDP 登录，看到 XFCE 桌面；8 个 PCIe 设备和驱动节点正常，xrdp active。完整 P5 测试通过 32 Host DMA、224 peer、8 file IPC，以及连续 3 轮各 56 对异步通信。桌面基础测试也通过。

验证日志在数据目录的 `migration-p5-validation.log` 和 `migration-desktop-validation.log`。这些结果属于当前 demo 平台，修改后需重新验证。自定义任务见 [share/CUSTOM_TASK.md](share/CUSTOM_TASK.md)。设备侧继续使用发布包的 MCU/NPU runtime；桌面使用 TCG，速度不代表实机性能。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| `terminal.py` 找不到 `state/active.json` | 尚未运行 `desktop.py`，或两个终端的 `ZC_DESKTOP_STATE` 不一致；按首次流程第 5 步启动 |
| `desktop.py` 提示缺少 qcow2 / seed | 先下载基础镜像并执行 `prepare.py`，不是直接执行连接脚本 |
| SSH banner exchange 超时，QEMU 的 `ps` 状态为 `T` | 旧版进程可能被暂停；在原终端用 `jobs -l` 和 `fg %作业号` 恢复。新版 `desktop.py` 忽略 Ctrl+Z，关闭终端后也继续运行 |
| SSH connection refused / 超时 | 检查终端 A 是否仍运行；首次启动等待 SSH 就绪，再检查本次 `serial.log` 和 `qemu.log` |
| SSH 能登录，但驱动节点或桌面服务缺失 | 首次安装尚未完成或失败；检查 `/var/log/t100-desktop-setup.log` 与来宾网络 |
| 端口占用或磁盘被锁定 | 检查是否已有 VM 使用相同磁盘和端口；已有 VM 直接连接，不要重复启动 |
| P5 双卡/八卡测试通过，但无法连接 Ubuntu | 两套入口生命周期独立；自动测试不创建常驻 Ubuntu，仍需完成本文首次准备流程 |

本文的 `terminal.py` / `control.py` 用于 `desktop.py` 管理的 VM。若使用克隆指南中的 `cli/qemu.py --guest desktop` 分离启动方式，按该节提供的原生 SSH 命令连接；该入口不生成这里的 `active.json`。
