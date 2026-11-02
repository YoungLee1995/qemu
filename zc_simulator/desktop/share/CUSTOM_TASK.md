# 自定义 Host 任务（本地实验）

在远程桌面的 Ubuntu 终端执行，复制到自己的可写目录后修改：

```sh
mkdir -p ~/task-dev
cp /mnt/hostshare/my-task.c /mnt/hostshare/guest-cluster.c ~/task-dev/
cd ~/task-dev
gcc -O2 -Wall my-task.c -o my-task
sudo ./my-task 0 4096 7 2
```

参数依次是卡号、字节数、输入 seed、DMA 模式。卡号按 PCI BDF 排序，为 0..7；长度 1..65535；seed 0..255；mode 1..4。mode 1=NPU/NPU，2=HDMA/HDMA，3=HDMA/NPU，4=NPU/HDMA，分别表示输入/输出引擎，运算均为参考 NPU 程序的逐字节 XOR 0x5a。

my-task.c 的输入循环和校验循环可改为自己的数据生成和结果检查。submit() 负责任务描述符写入，finish() 等待设备完成，两者定义于 guest-cluster.c。启动时从完成记录继承 task ID，可重复运行。示例不提供并发仲裁；请等其他 guest-cluster、p5-guest-test 或自定义程序结束后再运行。

成功输出 MY_TASK_PASS。这个例子改变的是现有 Device 运算的输入和任务参数。新增下载、装载、跳转或新算法，需要同时扩展 Device 协议处理，并重建固件、配置加载。不能只把 mode 改成一个任意新编号。

## 源码对应

- guest-cluster.c：当前基础八卡测试副本（来自 QEMU 仓库 zc_simulator/p4/guest-cluster.c）。
- driver/ssv_test_driver.c：当前 Ubuntu demo 驱动源码，驱动负责 PCIe 绑定、DMA buffer 和 mmap 等。
- P5 异步通信测试源码在仓库 `zc_simulator/p5/guest-communication.c`；头文件在 `zc_simulator/tools/p5/zc_comm.h`。使用 `run_linux.py --runtime source` 会从同仓库生成 `p4-base.c` 并编译，不依赖额外项目。
- 参考 MCU/NPU runtime 在仓库 `zc_simulator/tools/p5/runtime/`。修改源码后需要重新构建并发布运行包，直接编辑源码不会改变当前运行的设备行为。
- Host 命令行应用源码在仓库 `zc_simulator/host-demo/ssv_test_app.c`；通过 `desktop/host_demo.py` 安装到来宾。

## 本次验证

4096 字节的四种 DMA 模式通过；卡 7、65535 字节、mode 2 通过。尝试 65536 字节时出现完成成功但数据不匹配，故示例明确拒绝该长度，不代表基础接口所有边界都已验证。编译时引用原参考文件可能出现已有的 snprintf 截断警告。
