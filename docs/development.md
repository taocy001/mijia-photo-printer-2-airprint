# 开发与诊断

## 代码入口

`tools/run_candidate.py` 解析命令行，初始化 `src/queued_bridge.py` 持久队列，通过 `src/bridge.py` 提供IPP/HTTP，再由 `src/native_transport.py` 调用自写USB库。根目录同名脚本仅作兼容转发。

`src/offline.py` 处理JPEG/EXIF、等比布局、IPP编解码和设备任务参数；`src/documents.py` 加载 `build/libmijia_render.dylib`。PDF/URF/PWG在独立 `src/render_worker.py` 进程渲染，限制输入32MiB、解码4000万像素、30秒进程超时及20CPU秒；这些限制不等于已全面防御任意恶意文档。

源码模块保持单层布局，由工具入口添加 `src/` 到导入路径，没有安装Python包或修改全局PYTHONPATH。原生库位于 `build/`，运行数据仍位于仓库根目录，不写入源码目录。

## 构建和测试

在已激活的虚拟环境中：

```sh
./build.sh       # 编译本机架构、生成合成raster fixtures、运行全部测试
./tools/test.sh  # 使用现有build重新测试
```

可以通过 `PYTHON` 环境变量选择测试解释器，例如 `PYTHON=.venv/bin/python ./tools/test.sh`。构建需要系统clang、SDK和CUPS头文件，脚本使用zsh。测试含临时loopback HTTP服务，不调用真实USB。测试fixtures均为人工合成，不含实际照片；没有CI工作流或安装包。

主要覆盖：FIFO、busy/8006等待、上传ACK、连接故障、不确定创建不重放、重启恢复、取消范围、IPP两种提交方式、HTTP分块传输、JPEG/EXIF、PDF旋转和页框、URF/PWG方向。

## 照片PDF识别

单页、未加密PDF才可处理。自动fill的照片识别只接受平衡q/Q、矩阵cm、单次图像Do，以及四种标准颜色呈现意图ri。图像须轴对齐、位于页面内、居中并覆盖完整一个页面维度，另一维度至少覆盖半页。

注释、蒙版、文字、矢量绘制、clip、Form、inline image、多图像或未知指令会回退整页fit，不破坏文档布局。白色像素永远不是裁切依据。原PDF图像边界归一化后，显式等比映射PDF点到像素，正确处理页框原点和90/270度旋转；再由照片布局选择最小裁切方向。

真实iPhone PDF曾包含 `/Perceptual ri`，早期严格识别器拒绝它导致回退fit、保留页内白边。此问题已经修复，并在后续实际照片工作流得到使用者出纸确认；不代表所有复杂PDF都可自动去除页内边距。

## 布局证据与接收诊断

正常服务默认只记录有限布局属性和状态，不存原文档。知情情况下可在现有启动命令附加 `--retain-layout-evidence`，保存原始输入和最终JPEG；最多20份、256MiB，上限后拒绝新渲染而不丢证据。可能包含私人照片，禁止提交到Git。

另有独立接收工具，不向USB发送数据：

```sh
python3 run_capture.py --help
```

实际启用需同时指定本机私有地址、主机名、允许网段及 `--enable-diagnostic-reception`。它使用8633并广播 `Mijia Layout Capture - NO PRINT`，数据位于 `diagnostic-spool/`，不会提升到真实打印队列，也不随正常服务启动。

分析白边应依次核对：IPP属性 → PDF对象/变换 → 渲染输出 → 最终1216×1824 JPEG → 设备任务参数 → 物理成品区域。不要将撕边算作照片区域；如果最终JPEG已满幅而实物仍有边距，应研究设备映射，不能继续猜测按白像素裁切或随意增加overscan。
