"""Opt-in experimental native API transport using a current-page SDK request."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime

from ..models import CHINA, Checkout, NeedsHuman, TaskConfig
from ..protection import AccessLimited, check_access
from .jd_api import SubmitSignal, parse_submit_response, submit_endpoint


@dataclass(frozen=True, repr=False)
class PreparedSubmit:
    # Secrets are memory-only, excluded from repr and diagnostic output.
    url: str
    body: str
    page_url: str
    checkout: Checkout
    headers: dict[str, str] = field(default_factory=lambda: {'content-type':'application/x-www-form-urlencoded'})
    prepared_at: float = field(default_factory=time.monotonic)


class DirectSubmitTrial:
    def __init__(self, browser, page, persist_intent):
        self.browser, self.page = browser, page
        self.persist_intent = persist_intent
        self.dispatched = False
        self.response = None
        self.now = lambda: datetime.now(CHINA)
        self.monotonic = time.monotonic

    async def submit(self, config: TaskConfig, prepared: PreparedSubmit, recheck) -> SubmitSignal:
        if self.dispatched:
            raise NeedsHuman('本次 API 请求已经发出或结果不明，禁止再次发送或回退点击')
        check_access(self.browser)
        if not submit_endpoint(prepared.url, 'POST'):
            raise NeedsHuman('API 试验仅接受已识别的京东下单接口')
        if set(prepared.headers) - {'content-type', 'x-api-eid-token'}:
            raise NeedsHuman('不接受手工 Cookie、身份或浏览器伪装请求头')
        if prepared.headers.get('content-type') not in {'application/x-www-form-urlencoded', 'application/json'}:
            raise NeedsHuman('API 请求格式未经校准')
        # Same page, amount, quantity and address must still hold immediately
        # before dispatch. The durable intent belongs to the calling engine.
        checkout = await recheck(config)
        if checkout != prepared.checkout or self.page.url != prepared.page_url:
            raise NeedsHuman('结算页或订单条件发生变化，未发送 API 请求')
        if (checkout.sku != config.product.sku or checkout.quantity != config.quantity
                or checkout.address != '已核对' or not config.price_allowed(checkout.total_cents)):
            raise NeedsHuman('API 提交前商品、数量、地址或金额校验失败')
        end = min(config.purchase_end, datetime.fromisoformat(config.product.sale_end)) if config.product.sale_end else config.purchase_end
        if not config.purchase_start <= self.now() < end:
            raise NeedsHuman('不在本场次时间窗口，未发送 API 请求')
        if not 0 <= self.monotonic() - prepared.prepared_at <= 2:
            raise NeedsHuman('当前请求参数已过期，未重放旧签名或旧订单请求')
        check_access(self.browser)
        # The caller must commit an intent before any irreversible network IO.
        # Failure to persist aborts the request; timeout never clears this intent.
        self.persist_intent()
        self.dispatched = True
        try:
            result = await self.page.evaluate("""async spec => {
                const controller = new AbortController();
                const timer = setTimeout(() => controller.abort(), 5000);
                try {
                    const response = await fetch(spec.url, {
                        method: 'POST', body: spec.body, headers: spec.headers,
                        credentials: 'include', redirect: 'error', signal: controller.signal
                    });
                    const data = response.headers.get('content-type')?.includes('application/json')
                        ? await response.json() : null;
                    return {status: response.status, data, retryAfter: response.headers.get('retry-after') || ''};
                } finally { clearTimeout(timer); }
            }""", {'url':prepared.url, 'body':prepared.body, 'headers':prepared.headers})
        except Exception:
            # The server may have committed even if fetch timed out/failed CORS.
            return SubmitSignal('unknown')
        self.response = result
        if result['status'] in (403, 429):
            protection = getattr(self.browser, 'protection', None)
            if protection is not None:
                retry = result.get('retryAfter', '')
                seconds = max(300, min(3600, int(retry))) if retry.isdigit() and len(retry) <= 6 else 300
                protection.pause('京东下单接口限制访问，API 试验已暂停', seconds=seconds)
            raise AccessLimited('京东下单接口限制访问；禁止 API 重试或回退点击')
        signal = parse_submit_response(result['data']) if result['status'] == 200 else SubmitSignal()
        if signal.kind == 'restricted':
            protection = getattr(self.browser, 'protection', None)
            if protection is not None:
                protection.pause('京东下单接口要求人工验证，API 试验已暂停')
            raise AccessLimited('京东下单接口要求人工验证；禁止 API 重试或回退点击')
        return signal
