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

默认运行数据目录为 `zc_simulator/desktop/state/`。当前工作区使用本地软链接复用已有 Ubuntu；软链接不会提交，新克隆会创建自己的数据目录。系统盘、凭据、密钥、seed、下载工具、日志和进程状态留在此目录，受 Git 忽略规则保护。可以用环境变量 `ZC_DESKTOP_STATE` 指定其他绝对路径，所有入口须使用同一配置。

需要已经构建的 `build/qemu-system-x86_64`、有效的 `zc_simulator/simu/current` 发布包以及 SSH 客户端。浏览器终端需要 PATH 中的 `ttyd`，或数据目录下的 `tools/local/ttyd`。这些外部依赖由使用者下载或编译，不纳入桌面源码目录。

## 直接进入命令行

以下命令都在 QEMU 仓库根目录执行：

```sh
python3 zc_simulator/desktop/terminal.py
```

这会在当前 VS Code 终端进入 Ubuntu，无需手动填写 SSH 账号。看到 `t100@t100-eight-card` 后可执行：

```sh
lspci -nn -d 16c3:abcd
sudo /opt/t100/guest-cluster
```

输入 `exit` 只断开命令行，不会关闭虚拟机。

## 浏览器终端

```sh
python3 zc_simulator/desktop/web_terminal.py
```

浏览器打开 <http://127.0.0.1:7681>。VS Code 远程容器环境在「端口 / PORTS」中转发 7681，点击浏览器图标；进入后自动连接 Ubuntu。已有服务占用 7681 时直接使用已有入口，不要重复启动。

## 启动、检查和关闭

已经运行时不需要重复启动。检查状态：

```sh
python3 zc_simulator/desktop/control.py status
```

正常关机：

```sh
python3 zc_simulator/desktop/control.py stop
```

关机后重新启动（前台保持运行）：

```sh
python3 zc_simulator/desktop/desktop.py --cards 8
```

支持 `--cards 1/2/8`、`--memory 4G`、`--ssh-port 2222`、`--rdp-port 13389`。默认监听 127.0.0.1；Docker 宿主机需要直接访问时，使用 `--bind 容器IP`，容器 IP 可通过 `hostname -I` 查看。串口和 QMP socket 使用短临时目录，避免工作区路径过长。

## 图形桌面

在 VS Code「端口 / PORTS」中转发 13389，用电脑上的远程桌面客户端连接其本地转发地址。xrdp 选择 Xorg，用户名 `t100`。密码查看：

```sh
cat zc_simulator/desktop/state/credentials.json
```

自定义数据目录时改用对应路径。桌面中可打开终端、`PCIe devices` 或 `Eight-card DMA and P2P test` 快捷方式。首次使用快捷方式可能需要允许启动。共享目录为数据目录中的 `share/`，来宾挂载在 `/mnt/hostshare/`。

## 新克隆后的准备流程

正式目录提供自己开发的源码和文档。QEMU、Ubuntu 镜像、ttyd、系统软件包均通过构建或下载获得，生成的系统盘、账号和日志保存在 `state/`，不需要原开发机的 evidence 文件。

1. 按 [克隆后构建与运行简易指南](../克隆后构建与运行简易指南.md) 准备 Ubuntu 22.04 x86-64 环境、P5 运行依赖并构建本项目 QEMU。已有的 P5 发布包位于 `zc_simulator/simu/current`，它属于原项目交付内容。
2. 安装镜像与 SSH 工具：

```sh
sudo apt update
sudo apt install -y qemu-utils genisoimage openssh-client curl ca-certificates
```

3. 下载 Ubuntu 官方 cloud image 与对应校验清单，保留官方文件名以校验：

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

4. 初始化并启动：

```sh
python3 zc_simulator/desktop/prepare.py
python3 zc_simulator/desktop/desktop.py --cards 8
```

`prepare.py` 使用 Python 的 `crypt` 模块，请用 Python 3.10–3.12；QEMU 构建可单独使用 Python 3.12。初始化生成本地密钥、随机密码、系统盘和 cloud-init。首次 Ubuntu 启动需要联网安装 XFCE、xrdp、内核头文件，编译本目录的驱动源码和仓库 `p4/guest-cluster.c`。用 `control.py status` 检查，出现 `T100_DESKTOP_READY` 表示桌面准备完成。

桌面基础八卡测试程序会在初始化时编译，无需复制旧 evidence 中的二进制。文档提到的完整 `p5-guest-test-8` 属于历史测试产物，不是桌面启动所必需，也不会由这里的初始化脚本生成。

5. 如需浏览器终端，下载上游 ttyd（也可以自行从上游源码编译）：

```sh
mkdir -p zc_simulator/desktop/state/tools/local
curl -fL https://github.com/tsl0922/ttyd/releases/download/1.7.7/ttyd.x86_64 \
  -o zc_simulator/desktop/state/tools/local/ttyd
chmod +x zc_simulator/desktop/state/tools/local/ttyd
python3 zc_simulator/desktop/web_terminal.py
```

ttyd 项目与构建说明：<https://github.com/tsl0922/ttyd>。浏览器终端不是桌面或命令行启动的必需依赖；`terminal.py` 使用系统 SSH 客户端即可。

修改 `share/my-task.c` 等自定义任务源码后，可重新执行 `prepare.py` 同步共享文件；不要在 VM 运行期间重建它正在使用的 seed 和镜像，请先正常关闭。

## 验证与开发

2026-11-02 已完成实际 RDP 登录，看到 XFCE 桌面；8 个 PCIe 设备和驱动节点正常，xrdp active。完整 P5 测试通过 32 Host DMA、224 peer、8 file IPC，以及连续 3 轮各 56 对异步通信。桌面基础测试也通过。

验证日志在数据目录的 `migration-p5-validation.log` 和 `migration-desktop-validation.log`。这些结果属于当前 demo 平台，修改后需重新验证。自定义任务见 [share/CUSTOM_TASK.md](share/CUSTOM_TASK.md)。设备侧继续使用发布包的 MCU/NPU runtime；桌面使用 TCG，速度不代表实机性能。
