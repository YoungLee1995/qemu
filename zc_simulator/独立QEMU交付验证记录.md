# 独立 QEMU 交付验证（2026-11-02）

## 环境与隔离方式

将 Git 已跟踪文件及本次新增的项目源码、固定输入复制至
`/tmp/zc-qemu-only-3prd0vfq/pcie_qemu`，该目录上级不存在 simulator、experiments 或 MoE 数据仓库。
不复制旧 evidence、desktop/state、Python 缓存或宿主工具。使用本项目已有构建的
`build/qemu-system-x86_64` 作为被测程序；本次未在全新操作系统重新构建整个 QEMU。
宿主系统动态库和工具依赖按简易指南准备。

## 已通过的检查

- `python3 zc_simulator/p5/run_all.py --skip-build`：9 项全部通过。
  协议 18 测试、虚拟链路校准、固定 Tensor、IRQ、fabric 故障恢复、双/八卡通信、双/八卡 Linux。
  结果：`zc_simulator/p5/evidence/all-1793601721836688069/results.json`，`passed: true`。
- `python3 zc_simulator/p5/run_linux.py --runtime source --cards 2`：在该隔离目录中
  使用 RISC-V 裸机 GCC 从本仓库源码重建 MCU/NPU 和来宾程序，发布新包，双卡全项通过。
  结果：`zc_simulator/p5/evidence/linux-2-1793601573675963978/results.json`。
- 新 desktop 数据目录：复用与官方下载字节一致的 Ubuntu 官方基础镜像及对应清单，
  未复制已安装桌面的旧系统盘、旧私钥或 seed；`prepare.py` 生成系统盘、凭据、共享源码和 cloud-init。
  qemu-img/genisoimage 使用本机已下载工具及匹配动态库，等价工具的系统安装方式见 README。
- 全新系统盘启动 8 卡，首次联网安装 XFCE/xrdp/内核头文件；cloud-init done、无 errors，
  `T100_DESKTOP_READY`、8 个设备节点、xrdp active；来宾基础测试退出 0，
  32 Host / 224 peer / 8 file IPC 全部通过。日志：隔离目录
  `zc_simulator/desktop/state/fresh-desktop-validation.log`。
- 当前桌面 Host 应用使用仓库 `host-demo/ssv_test_app.c` 在来宾中编译成功，8 卡 active；
  使用仓库 my-task.c 编译并执行卡 0、4096 字节、mode 2 成功。
- Python 语法及 `git diff --check` 通过；新增项目源码/fixtures 均可由 Git 跟踪。

## 范围

没有将历史 P0–P4 的外部工作区源码门禁计作本次通过项。
没有将原 Host demo 的 file-load/get-result 当作通过项。
新的桌面磁盘验证了 xrdp 服务和八卡任务；未再次从另一台用户电脑做 RDP 端到端验证。
可下载软件、Ubuntu 镜像、编译产物、私钥和运行日志不加入正式代码目录。
