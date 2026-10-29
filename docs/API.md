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
| POST | `/api/tasks/{id}/reset` | JSON `{confirm_no_order:true}`，用户确认未下单且订单列表检查通过后归档提交记录并重置；不自动启动 |
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
  "ignore_price":false,
  "address_hint":"请替换为本人实际地址关键字",
  "window_seconds":60
}
```

金额为整数分。`ignore_price` 默认 `false`，此时最高金额不能省略或为零；只有显式设置为布尔值 `true` 时，才允许最高金额为空或省略，并跳过未知价格、预算上限检查。该选项不跳过商品、数量、地址、付款方式、时间及防重检查。以上预算仅为接口示例，不是用户购买授权；实际使用以用户填写并启用的任务为准。`product` 还包括预约及抢购的起止时间，开始时重新读取并确认场次。

`advance_ms` 是整数毫秒，范围 0–86400000，旧任务缺失时默认 0。`window_seconds` 为 1–86400 的整数秒。实际开始为 `scheduled_at - advance_ms`，截止为实际开始加 `window_seconds`；活动结束时间更早时以活动结束为准。创建、修改必须给出未来的实际开始时间。预约/登录准备可提前执行；购物车结算和提交有单独的开始时间下限保护。毫秒参数不等于浏览器或网络执行的精度保证。

删除是可恢复的列表移除，数据库保留订单核验及防重记录。核验中的提交会等待核验结束，30 秒内未结束则返回 409，请稍后再删除。对已删除、结果不明的同 SKU 提交，新任务仍不能绕过保护直接购买。

状态：`draft` → `preparing` → `reserving`（需要时）→ `preparing_cart` → `scheduled`（等待实际开始）→ `preparing_checkout`（提前准备）→ `scheduled` → `running` → `submitting` → `entering_payment` → `locking` → `verifying` → `succeeded`。无法提前准备时在购物车等待；任务启动已过实际开始时跳过准备等待。异常进入 `waiting_human`、`failed` 或 `stopped`。服务重启将运行中任务转为 `interrupted`。

v0.2.0-api.2 增加活动状态 `preparing_checkout`：实际开始前 20 秒准备结算，目标提前 10 秒完成，之后回到 `scheduled` 等待提交时刻。结算不可预先完成时保留购物车等待。详情见 [CHECKOUT_PREPARATION.md](CHECKOUT_PREPARATION.md)。

`OrderResult.outcome` 为 `created`、`unknown` 或 `failed`。新流程要求 `created`、`payment_entered=true`，并具备具体新订单号、目标 SKU、数量、金额及待付款证据，才显示“抢购成功”并输出订单号。订单已创建但未确认付款界面时保持 `waiting_human`，保留订单号，不再提交。付款界面出现订单号时，还须与订单列表核验一致。

v0.2.0-api.3 的 `ignore_price=true` 任务不以订单金额作为匹配条件，仍须有唯一新增订单号、匹配 SKU/数量/时间、待付款状态与付款页证据；订单金额可读时照常展示，读不到时显示“金额以京东订单为准”，不编造零元或沿用旧预算。旧任务不自动开启此选项。

点击前持久化 `submit_intent`、提交时间与订单基线；确认付款界面后，持久化 `payment_entered_at` 及可读到的 `payment_order_id`。恢复仅核验，保留已确认的付款步骤证据，不会重复提交。历史结果缺少 `payment_entered` 时不能推断已确认付款界面。

`OrderResult.payment_flow` 独立记录支付步骤：`not_attempted`、`entered`、`entered_and_exited`、`not_confirmed`。新版结算选择微信扫码；只在确认进入付款界面后退出支付并保留订单，随后显示订单列表。退出失败不抹去已核验的付款界面和订单证据，但消息要求人工检查退出状态。不执行扣款或取消订单。

准备检查的 `reservation_status` 为 `reserved`（页面明确已预约）、`reminder_set`（页面明确开抢提醒已设置）或 `null`。`reminder_set` 不代表京东已经确认购买资格，任务会在结算阶段继续检查。设置提醒后不会再强行查找“立即预约”按钮。

浏览器页和账号是单实例资源。运行任务时拒绝再启用另一任务，也拒绝打开额外商品解析/检查页面。公共分享短链只接受 HTTPS `3.cn` 和京东域名，逐次验证重定向目标；不接受任意 URL 抓取。

## 重置未成功的任务

任务详情提供“重置到抢购前”。用户须先核对京东订单列表并勾选确认未生成订单。服务使用与购买任务相同的串行执行位重新只读检查订单；已有目标待付款订单、订单无法读取、服务忙或任务已有明确订单号时拒绝重置。

成功后保留任务配置，原记录完整归档至本地 `task_resets`；清空当前提交意图、结果、支付证据、基线与最近截图引用，状态为 `stopped`。旧日志保留并推送重置事件。不自动购买、不取消订单；过期场次需修改后再启用。

`POST /api/account/login?open_only=true` 仅显示应用独立窗口，不导航至京东登录页，供人工诊断。默认行为仍为打开登录。账号状态可返回 `browser_network=system/direct` 及 `access_paused`；这些是本地状态，不代表订单创建成功。

## 提交 API 取证

任务新增只读字段 `submission_order_id`：当前任务页本次提交 API 返回的候选订单号，立即持久化，服务重启后仍用于核验。它不等同于已确认创建，不能绕过商品、金额与订单状态核对；API 号码与订单列表不一致时保持待人工处理。有此字段的任务不允许重置。

正式版 v0.1.2 使用页面提交和 API 响应取证。本 API 实验分支为任务增加 `submit_mode=dom/api`，默认 `dom`，只保存配置不会自行启用任务。`api` 使用当前页面 SDK 请求生成与单次原生 API 派发，详见 [预发布说明](RELEASE-v0.2.0-api.1.md)。历史只读试验见 [API_TRIAL.md](API_TRIAL.md)。
