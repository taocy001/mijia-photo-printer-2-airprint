# 第三方与分发边界

本仓库不包含厂商Windows安装包、DLL、固件、反编译源码或用户照片。`native/` 是本项目自写辅助代码，`tests/fixtures/` 为合成测试数据。

运行时依赖通过 `requirements.txt` 单独安装，并未随源码捆绑：

- [Pillow及其许可证](https://github.com/python-pillow/Pillow/blob/main/LICENSE)
- [aiohttp及其许可证](https://github.com/aio-libs/aiohttp/blob/master/LICENSE.txt)
- Python运行时适用其自身许可证；本仓库不提供运行时。

macOS的IOKit、CoreGraphics、CoreFoundation和CUPS库由操作系统/SDK提供，未复制进仓库。以后若捆绑Python、原生依赖或制作安装包，应按实际版本收集完整第三方许可证和分发说明。

仓库当前没有为本项目自写代码声明开源许可证；第三方许可证不会自动成为本项目代码的许可证。需要再分发或公开发布时，应由权利人明确许可。
