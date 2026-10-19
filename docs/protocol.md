# 已实现的协议范围

本项目实现米家照片打印机2（USB VID 302C / PID 3008）的已验证子集。USB接口2使用自写用户态IOKit transport；不是WinUSB DLL移植、内核扩展或官方驱动。接口打开不强占、不重置，不更新固件。

## 图像与任务

最终输入为1216×1824 sRGB JPEG，编码目标不超过512KiB，设备参数 `media-size=5012`、`job-type=0`、`file-type=jpg`、`sharpenmode=0`。6寸Windows配置的全幅模式为此画布、边距0；不把共享库内其他机型功能自动归属本机型。

控制请求使用 `cmd json\n` 加紧凑JSON。创建成功可能省略error-code，仅返回result.job_id；显式错误仍为失败。上传每块最多8192图像字节，头为 `cmd data EXTLEN=N+4\n`，后跟设备任务ID的小端32位整数和图像字节。已验证ACK含长度后的 ` OK`，接收端处理碎片和异步事件。上传确认与实际打印完成是不同阶段；设备状态9为Completed、7为Aborted、8为Canceled。

## 队列与恢复

SQLite使用WAL/FULL同步，单一worker执行USB。提交前查看设备任务列表及整体状态；无外部任务且idle/sleep、error=0才可创建。8006拒绝可安全等待后再尝试；创建/上传结果不确定时停止自动重试，防止重复出纸。

启动时处于creating/uploading的任务标记unknown，暂停队列等待只读对账。已确认上传的任务只查询原设备ID。完成任务不重放。取消仅针对本桥创建的任务，不会取消其他应用任务。

相同完整IPP请求持久去重。可选内容去重窗口能合并客户端换请求ID的重试，也可能合并有意重复打印；默认关闭。接收支持Print-Job，或Create-Job加一次最终Send-Document，不支持多文档。

## 限制和验证

普通IPP，没有认证和TLS，只按允许CIDR限制来源。一个请求最多32MiB；最多20个非终态任务，准备图像spool上限256MiB。JPEG EXIF和显式方向支持有独立测试；PDF/URF/PWG仅覆盖已实现的单页子集。

离线测试使用合成图像和脱敏协议回复，不能证明全部固件、纸张、物理分辨率或边缘机械精度兼容。没有官方驱动认证或全面AirPrint认证声明。协议的更改须先回归，真实打印另行得到授权。
