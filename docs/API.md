# 本地 API 与状态

服务默认同源提供静态前端与 `/api`，通过 `HttpOnly; SameSite=Strict` 会话 Cookie 认证。除登录与健康检查外均需认证；写请求和 WebSocket 校验 Origin；仅接受本机、主机名及检测到的局域网 Host。访问码不放在二维码或 URL 中。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/health` | 版本与存活状态 |
| POST | `/api/auth/login` | JSON `{code}`，8 次错误/来源/分钟限制 |
| POST | `/api/auth/logout` | 撤销当前控制台会话 |
| GET | `/api/overview` | 任务、近 300 条日志、账号提示、局域网地址、服务器北京时间 |
| POST | `/api/account/login` | 在电脑打开应用专用京东登录浏览器 |
| GET | `/api/account/status` | 浏览器与登录会话提示，不返回 Cookie |
| POST | `/api/account/close` | 空闲时关闭应用浏览器并保存会话 |
| POST | `/api/products/resolve` | JSON `{source}`，解析公开商品及预约/抢购场次 |
| POST | `/api/products/preflight` | JSON `{source}`，只读检查预约、购物车、订单页结构 |
| GET/POST | `/api/tasks` | 列表 / 创建草稿 |
| GET/PUT/DELETE | `/api/tasks/{id}` | 查询 / 更新无提交记录的空闲任务 / 停止并从列表删除任务（保留核验记录） |
| GET | `/api/tasks?deleted=true` | 已删除任务列表 |
| POST | `/api/tasks/{id}/restore` | 恢复已删除任务到列表，不自动启动 |
| POST | `/api/tasks/{id}/start` | 启用任务 |
| POST | `/api/tasks/{id}/stop` | 停止后续购买动作；已有提交仍完成订单核验 |
| POST | `/api/tasks/{id}/resume` | 无提交记录则重新检查条件；有记录则仅核验订单 |
| GET | `/api/tasks/{id}/screenshot` | 认证后读取该任务的最近截图 |
| GET | `/api/events?task_id=...&after=...` | 持久日志与断线补取 |
| WS | `/ws` | `task`、`log`、`deleted` 事件与 15 秒心跳 |

## TaskConfig

```json
{
  "product": {"platform":"jd", "sku":"100327520619", "title":"由解析接口返回", "spec":"由解析接口返回", "url":"https://item.jd.com/100327520619.html"},
  "quantity":1,
  "scheduled_at":"2026-10-25T10:00:00+08:00",
  "advance_ms":0,
  "max_total_cents":269900,
  "address_hint":"请替换为本人实际地址关键字",
  "window_seconds":60
}
```

金额为整数分，不能省略或为零。以上预算仅为接口示例，不是用户购买授权；实际使用以用户填写并启用的任务为准。`product` 还包括预约及抢购的起止时间，开始时重新读取并确认场次。

`advance_ms` 是整数毫秒，范围 0–86400000，旧任务缺失时默认 0。`window_seconds` 为 1–86400 的整数秒。实际开始为 `scheduled_at - advance_ms`，截止为实际开始加 `window_seconds`；活动结束时间更早时以活动结束为准。创建、修改必须给出未来的实际开始时间。预约/登录准备可提前执行；购物车结算和提交有单独的开始时间下限保护。毫秒参数不等于浏览器或网络执行的精度保证。

删除是可恢复的列表移除，数据库保留订单核验及防重记录。核验中的提交会等待核验结束，30 秒内未结束则返回 409，请稍后再删除。对已删除、结果不明的同 SKU 提交，新任务仍不能绕过保护直接购买。

状态：`draft` → `preparing` → `reserving`（需要时）→ `preparing_cart` → `scheduled`（停留购物车等待）→ `running` → `submitting` → `entering_payment` → `locking` → `verifying` → `succeeded`。异常进入 `waiting_human`、`failed` 或 `stopped`。服务重启将运行中任务转为 `interrupted`。

`OrderResult.outcome` 为 `created`、`unknown` 或 `failed`。新流程要求 `created`、`payment_entered=true`，并具备具体新订单号、目标 SKU、数量、金额及待付款证据，才显示“抢购成功”并输出订单号。订单已创建但未确认付款界面时保持 `waiting_human`，保留订单号，不再提交。付款界面出现订单号时，还须与订单列表核验一致。

点击前持久化 `submit_intent`、提交时间与订单基线；确认付款界面后，持久化 `payment_entered_at` 及可读到的 `payment_order_id`。恢复仅核验，保留已确认的付款步骤证据，不会重复提交。历史结果缺少 `payment_entered` 时不能推断已确认付款界面。

`OrderResult.payment_flow` 独立记录支付步骤：`not_attempted`、`entered`、`entered_and_exited`、`not_confirmed`。新版结算选择微信扫码；只在确认进入付款界面后退出支付并保留订单，随后显示订单列表。退出失败不抹去已核验的付款界面和订单证据，但消息要求人工检查退出状态。不执行扣款或取消订单。

准备检查的 `reservation_status` 为 `reserved`（页面明确已预约）、`reminder_set`（页面明确开抢提醒已设置）或 `null`。`reminder_set` 不代表京东已经确认购买资格，任务会在结算阶段继续检查。设置提醒后不会再强行查找“立即预约”按钮。

浏览器页和账号是单实例资源。运行任务时拒绝再启用另一任务，也拒绝打开额外商品解析/检查页面。公共分享短链只接受 HTTPS `3.cn` 和京东域名，逐次验证重定向目标；不接受任意 URL 抓取。
