# 第三方依赖与参考

本项目业务实现独立编写。以下仓库仅用于了解架构、产品流程和历史问题，没有复制其业务源文件：

- [HanphoneJan/AutoBuy](https://github.com/HanphoneJan/AutoBuy)：MIT，近期预约、购物车和订单核验问题参考。
- [ShuaiLeiLu/JDPanicBuying](https://github.com/ShuaiLeiLu/JDPanicBuying)：GPL-3.0，本地网页工作台形态参考。
- [kody-black/SecKill](https://github.com/kody-black/SecKill)：查询时未识别 LICENSE，仅参考平台适配器与调度分层思路。

直接依赖包括 FastAPI、Uvicorn、Playwright、Pydantic、HTTPX、websockets、tzdata、React、Vite、TypeScript、Lucide 与 qrcode.react。实际安装版本和传递依赖见两个锁文件；包中保留各自的许可证文本。图标由 Lucide 提供，使用 monochrome SVG，不使用 emoji 作为界面图标。

应用通过 Playwright 安装官方 Chromium；该浏览器及其组件遵循各自许可。若分发浏览器二进制或完整安装包，应一并提供对应第三方许可，而不是仅分发本文件。
