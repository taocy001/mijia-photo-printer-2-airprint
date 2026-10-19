# 米家照片打印机 2 · macOS 打印桥

让 Mac 通过 USB 连接**米家照片打印机 2（ZMRSHZP02HT）**，再让同一局域网的 Mac / iPhone 通过 AirPrint 打印 **6 寸照片**。

这是用户态 **AirPrint → USB 桥接程序**，不是小米官方驱动、内核驱动或苹果认证驱动。目前需要自行安装依赖、编译并手动启动；**没有已发布安装包、自动 CI 或默认开机服务**。

## 已支持什么

- 6 寸 4×6 英寸相纸，单页、单份；最多排队 20 个任务。
- JPEG、单页未加密 PDF、受支持的单页 URF / PWG Raster。
- 照片自动方向、等比铺满与最小裁切；不会拉伸或按白色像素盲裁。
- 持久队列、设备忙时等待、状态查询和重复请求保护。不确定是否已经提交的任务会暂停，避免自动重复出纸。

在一台 Apple Silicon Mac、实际打印机和 6 寸相纸上，使用者已确认测试过的 iPhone 照片工作流出纸验证通过；Mac 系统打印路径也完成过测试。**这不代表所有客户端、格式、系统版本或固件均已实物验证。** 当前离线回归为 82 项。

不支持 4 寸、多页、双面、纹理覆膜；不会自动修改系统打印机、其他打印队列或防火墙。

## 从零准备

需要 macOS、Xcode Command Line Tools、Python **3.10 或更新版本**、Git，以及能正常进行 USB 数据连接的打印机。当前主要验证平台是 Apple Silicon；构建脚本按本机架构编译，Intel 版本尚未做真机完整验证。最低兼容 macOS 版本尚未确定。

1. 如果还没有开发工具，先运行 `xcode-select --install`，在系统界面完成安装。
2. 确认 `python3 --version` 符合要求。Python 不随本项目提供。
3. 下载仓库并在独立虚拟环境中安装依赖：

```sh
git clone git@github.com:coverdh/mijia-photo-printer-2-airprint.git
cd mijia-photo-printer-2-airprint
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
./build.sh
```

仓库目前为私有，clone 需要你自己的 GitHub 访问权限。构建生成 `build/` 内的两个原生库与测试辅助程序，并运行全部离线测试。预期末尾显示 `Ran 82 tests` 和 `OK`；测试只使用合成数据及本机临时 HTTP 服务，不操作真实 USB，也不打印。

以后打开新终端，先回到仓库并运行 `source .venv/bin/activate`。

## 首次配置与启动

先连接 USB、装好 6 寸耗材。到“系统信息 → USB”找到打印机的 USB 序列号；也可以运行 `system_profiler SPUSBDataType` 查看。请勿把自己的序列号提交到仓库。

下面变量必须按自己的 Mac 和网络填写。`en0` 只是常见网卡名；可用 `networksetup -listallhardwareports` 核对。`LAN_CIDR` 是允许提交任务的局域网网段，不是单个 IP。

```sh
PRINTER_SERIAL='替换为打印机USB序列号'
LAN_INTERFACE='en0'
LAN_CIDR='替换为局域网CIDR，例如192.168.1.0/24'
MAC_HOST="$(scutil --get LocalHostName).local"
LAN_IP="$(ipconfig getifaddr "$LAN_INTERFACE")"

python3 run_candidate.py \
  --serial "$PRINTER_SERIAL" \
  --hostname "$MAC_HOST" \
  --host "$LAN_IP" \
  --additional-host 127.0.0.1 \
  --allow-network "$LAN_CIDR" \
  --allow-network 127.0.0.0/8 \
  --port 8632 --announce --enable-experimental-live-printing
```

将所有“替换为…”内容填好再启动。`LAN_IP` 必须非空且是本机私有地址。`--enable-experimental-live-printing` 会允许后续客户端任务实际消耗相纸。

预期终端显示 `Running on ...:8632`，Bonjour 名称为 **小米照片打印机 2**。终端必须保持运行。网络换地址后需要用新的配置手动重启；本项目尚无自动网络配置功能。

**IPv6 网络**：如果 Mac 的 `.local` 名称还解析到 IPv6，需要为本机相应地址添加 `--additional-host`，并为来源网段添加 `--allow-network`；链路本地地址还需网卡作用域。不要绑定不属于本机的地址。具体参数说明见 `python3 run_candidate.py --help`。

服务使用普通 IPP，没有登录认证或 TLS。允许网段内的设备能提交打印；只在可信局域网使用，不做公网端口转发。系统如询问局域网/入站连接权限，需要使用者自行决定是否允许，本项目不自动更改这些设置。

## 在 Mac / iPhone 打印

- **iPhone**：连接同一局域网，在照片的分享菜单选择“打印”，选择“小米照片打印机 2”，确认 6 寸、1 份，再提交。
- **Mac**：在“系统设置 → 打印机与扫描仪 → 添加打印机”选择该 Bonjour 打印机，使用 AirPrint。若自动发现不可用，可在 IP 页面选 IPP，地址填 `你的Mac主机名.local:8632`，队列填 `ipp/print`；完整 URI 为 `ipp://你的Mac主机名.local:8632/ipp/print`。

添加系统打印队列是使用者的独立操作，启动桥本身不会添加。提交前确认没有误选其他同名设备。打印机可发现、预览正确或上传完成，都不等于已经正确出纸；以任务终态及实际成品为准。

## 方向与裁切规则

6 寸成品比例是横向 **3:2**、竖向 **2:3**，撕边不属于照片成品区域。

默认 `fill` 会比较 0°/90° 两种摆放，选保留面积较多的一种，再居中等比铺满。瘦长竖图通常裁上下；其他比例可能裁左右。`fit` 则保留整张照片，允许白边。客户端明确指定的方向和 fit 请求不会被默认值覆盖。

JPEG 先处理 EXIF 方向。能安全识别的单照片 PDF 按图像对象及变换边界取照片区域，再进行最小裁切；复杂 PDF、未知指令和 URF/PWG 保留整页版式，可能保留客户端已有白边。照片中的白墙、白衣等真实白色内容不会被当作边距裁掉。更多规则见 [开发说明](docs/development.md)。

## 数据、停止与卸载

运行数据位于**仓库根目录**，与启动终端的当前目录无关：

| 路径 | 内容 |
| --- | --- |
| `build/` | 本机编译产物，可重新构建 |
| `airprint-spool/queue.sqlite*` | 持久任务队列及状态 |
| `airprint-spool/*.jpg` | 等待发送的照片；终态后清理 |
| `airprint-spool/ipp-metadata.jsonl` | 有限的请求属性/错误/生命周期日志，不含照片标题或图片字节 |
| `airprint-spool/usb-audit.jsonl` | 有界 USB 协议审计日志 |
| `airprint-spool/layout-evidence/` | 仅加 `--retain-layout-evidence` 后保存原输入/最终图；最多20份、256MiB，达到上限拒绝新渲染 |
| `diagnostic-spool/` | 单独接收诊断工具的数据，默认不启动 |

按 **Ctrl-C** 停止前台服务并撤销本服务的 Bonjour 广告。停止进程不会撤回打印机已经接收的任务；重启时已确认上传的任务按既有设备 ID 查询，不重放。待处理任务可能继续执行。

没有后台安装或开机项需要卸载。停止服务、确认没有待处理或不确定任务后，可以移除仓库和虚拟环境；照片/诊断数据是否保留由你决定。若手动添加过系统打印机，也需在系统设置中单独移除。不要在任务状态不明时删除队列。

## 常见问题

- **找不到打印机**：检查终端服务是否仍在运行、同一网络、Bonjour是否被网络隔离，以及 `.local` 的IPv4/IPv6地址是否都正确监听。Mac睡眠或退出服务后不能继续充当打印桥。
- **端口占用/已有worker**：检查是否已启动另一实例。不要对同一打印机同时运行两个桥，也不要删除进程锁绕过检查。
- **USB打不开/一直等待**：核对型号、序列号、数据线、耗材和设备状态；其他应用的打印任务也可能占用设备。程序不会重置、强占或更新打印机固件。
- **白边仍在**：检查客户端是否请求fit、输入是否为复杂PDF，必要时在知情情况下启用布局证据保存。不要反复提交同一照片试图“修复”不确定任务；这可能多次出纸。
- **为什么相同照片有时合并**：可选 `--dedupe-content-window 秒数` 会在窗口内合并相同内容/选项的新请求，也会合并有意重复的照片；默认关闭。不需要时不添加该选项。
- **报库/依赖找不到**：先激活虚拟环境，再执行 `./build.sh`。不要将另一架构或另一台机器的构建产物直接视作已验证安装包。

## 仓库结构与开发

```text
src/             桥接、IPP、队列、图像处理和协议实现
native/          自写IOKit USB及CoreGraphics/CUPS渲染辅助库
tests/           离线回归及合成fixtures
tools/           启动、离线准备、构建与测试工具
docs/            架构、协议和诊断说明
build.sh         兼容构建入口
run_candidate.py 兼容启动入口
run_capture.py   兼容诊断入口（不会自动启动）
prepare.py       兼容离线JPEG准备入口（不打印）
```

只重新测试可运行 `./tools/test.sh`（需已构建）。工具的 `--help` 不会连接打印机。协议细节见 [协议说明](docs/protocol.md)，第三方边界见 [第三方说明](NOTICE.md)。本仓库未声明新的开源许可证，不应据此假定已授予任意再分发许可。
