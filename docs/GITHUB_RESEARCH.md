# GitHub 参考项目调研

查询日期：2026-10-25（Asia/Shanghai）。使用 GitHub 搜索与公共 API，核对仓库元数据、默认分支最近提交、源码结构及公开 Issue。Star 为当时 API 快照，不是持续更新数值。

搜索覆盖“京东 抢购”“jd seckill”“京东 抢购 pushed:>2025-01-01”“淘宝 京东 抢购”，排除关键词堆积的无关仓库。不能用网页抓取时间、`updated_at` 或 `pushed_at` 代替默认分支代码维护时间。

| 仓库 | Star | 默认分支最近提交 / 维护证据 | API 识别许可 | 结论 |
|---|---:|---|---|---|
| [tychxn/jd-assistant](https://github.com/tychxn/jd-assistant) | 5258 | 2020-03-10，logic error 修复 | MIT | 功能划分参考；库存与下单接口有过期反馈 |
| [zas023/JdBuyer](https://github.com/zas023/JdBuyer) | 4217 | 2022-10-29，文档；同日新增定金模式 | 未识别 | 补货监听、GUI 与通知体验参考；不复制未明确授权代码 |
| [andyzys/jd_seckill](https://github.com/andyzys/jd_seckill) | 2453 | 2020-12-11，名称与入口调整 | 未识别 | 历史预约秒杀流程，不能视为当前可用实现 |
| [chao325/MaoTai_GUIT](https://github.com/chao325/MaoTai_GUIT) | 2139 | 2026-10-10，README 更新 | Apache-2.0 | 默认分支主要是文档、图片及地区表，未见完整核心源码 |
| [Rosons/seckill_tools](https://github.com/Rosons/seckill_tools) | 337 | 2024-04-24，驱动下载说明；主要业务提交为 2022 年 | 未识别 | 多平台入口参考，业务维护较旧 |
| [HanphoneJan/AutoBuy](https://github.com/HanphoneJan/AutoBuy) | 253 | 2026-10-24，购物车自动勾选与失败重试 | MIT | 近期业务流程首选参考，包含网页控制台与 Android 实现 |
| [JokerPeter/jd_seckill_new](https://github.com/JokerPeter/jd_seckill_new) | 245 | 2021-01-05，README；代码 init 同日 | GPL-3.0 | 历史茅台流程及失败结果讨论 |
| [ord-kivy/JD_maotai_quick_buy](https://github.com/ord-kivy/JD_maotai_quick_buy) | 241 | 2025-04-28，README；此前代码为 2024 年 | 未识别 | 维护证据较弱，不作为底座 |
| [GilHogan/JDTreasureGrabber](https://github.com/GilHogan/JDTreasureGrabber) | 211 | 2026-04-13 宣布暂停维护，已归档 | AGPL-3.0 | 夺宝岛竞拍业务，界面/任务列表可参考 |
| [kody-black/SecKill](https://github.com/kody-black/SecKill) | 190 | 2026-09-04，淘宝流程、校准与架构更新 | 未识别 | 平台适配器、调度、重试、测试结构参考；京东配置仍待实测校准 |
| [aa342138039/JD-SHOPPER](https://github.com/aa342138039/JD-SHOPPER) | 181 | 2021-11-23，图片目录修复 | GPL-3.0 | 旧版预约、库存与网页接口参考 |
| [ShuaiLeiLu/JDPanicBuying](https://github.com/ShuaiLeiLu/JDPanicBuying) | 64 | 2026-09-19 合并修复；2026-05-16 重构本地客户端 | GPL-3.0 | 本地网页控制台形态参考 |
| [kilbertert/JDauto](https://github.com/kilbertert/JDauto) | 37 | 2026-08-11 清理工作区；8 月有构建与测试维护 | 未识别 | 有工程维护，但不直接复用授权未明的实现 |
| [17862923747/Ticket-grabbing-assistant](https://github.com/17862923747/Ticket-grabbing-assistant) | 2 | 2026-05-16，配置、GUI 与首次发布 | MIT | 辅助参考，社区证据有限 |

## 关键证据与取舍

- AutoBuy [Issue #21](https://github.com/HanphoneJan/AutoBuy/issues/21) 与 2026-10-24 提交对应，证明维护者处理了购物车勾选需求。[Issue #22](https://github.com/HanphoneJan/AutoBuy/issues/22) 仍有卡在结算页反馈，维护者提到可能遇到验证码。活跃维护不等于所有场景可靠。
- AutoBuy 的 `seckill.py` 已加入订单列表交叉验证，但一条路径通过长数字串数量比较判断新订单。本项目改为核对具体订单号、时间、精确 SKU、数量、总额及待付款状态。
- SecKill [README TODO](https://github.com/kody-black/SecKill#todo) 明确京东选择器仍沿用旧版页面，模拟测试不等于京东真实验收。
- jd-assistant [Issue #231](https://github.com/tychxn/jd-assistant/issues/231) 在 2026 年仍报告提交接口 404，高 Star 不能证明旧接口有效。

本实现独立编写业务代码，采用多平台接口与本地网页控制台思路，不整体 fork 以上项目。不会复制未识别许可仓库的源文件。后续若引用第三方具体实现，需另行核对文件许可与声明。

## 目标商品的只读证据

用户提供短链 `https://3.cn/38KiR-NL`，解析到 `100327520619`。公开商品信息对应“雷鸟GT Max bilibili 限定版 / M码”。2026-10-25 登录后，电脑商品页显示已预约资格，电脑购物车中该 SKU 存在且勾选框禁用；开售前价格显示待发布。

当次读取时间为预约 2026-10-24 11:15 至 2026-10-25 09:55、抢购 2026-10-25 10:00 至 11:00。运行时需重新读取，不能将此快照写死为后续场次。

电脑购物车已采用 CSS Modules 类名；订单列表出现 `orderCard-*`、`orderTime-*`、`priceRow-*`、`countNum-*` 新结构。实现保留语义类名前缀支持，并在找不到明确证据时暂停。
