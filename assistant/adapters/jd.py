from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from playwright.async_api import expect, TimeoutError as PlaywrightTimeoutError

from ..models import CHINA, Checkout, NeedsHuman, NotReady, OrderResult, Product, TaskConfig
from ..protection import AccessLimited, check_access, check_page_access
from .jd_api import SubmitProbe
from .jd_api import SubmitSignal
from .jd_submit_bridge import PageSignedSubmit
from .checkout_proof import CheckoutProof


@dataclass
class JDUrls:
    product: str = "https://item.jd.com/{sku}.html"
    cart: str = "https://cart.jd.com/cart_index"
    orders: str = "https://order.jd.com/center/list.action"


# Keep page-specific selectors in this one place. A missed/ambiguous selector
# stops the workflow; selectors are never replaced with unscoped text clicks.
SELECTORS = {
    "add_cart": '[data-testid="add-cart"], .page-right-RightBtns',
    "cart_quantity": '[class*="_cart-number_"] input, input[aria-label="商品数量"], input[aria-label="数量"], input[data-testid="cart-quantity"], input[type="number"], input[name="quantity"], input[name="num"], .quantity-form input.itxt, input[min][max]',
    "total": '[data-testid="total"], #sumPayPriceId, #sumPayPrice, #payPriceId, .trade-foot-detail .price-num, .payment-action-total .priceInfo .price',
    "address": '[data-testid="shipping-address"], .consignee-item.item-selected, .consignee-cont .addr-detail, #sendAddr, [class^="consignee-item-"][class*="active-"]',
    "goods": '[data-testid="checkout-goods"], .goods-list, .goods-items, #goodsList, .sku-item',
    "quantity": '[data-testid="quantity"], .p-num, .goods-number, [class^="countNum-"]',
    "submit": '[data-testid="submit-order"], #order-submit, #submitOrder, .checkout-submit, .payment-action button.btn-primary',
    "reservation_action": '[data-testid="reservation-action"], .page-right-RightBtns .first-row',
    "order_cards": '[data-order-id], tbody[id^="tb-"], .order-tb, [class^="orderCard-"]',
    "order_time": '[data-testid="order-time"], .dealtime, .create-time, [class^="orderTime-"]',
    "order_total": '[data-testid="order-total"], .amount .ftx-13, .amount span, [class^="priceRow-"]',
    "payment_dialog": '[class^="scantoPay-"]',
    "payment_close": '[class^="scantoPayHead-"] img[class^="close-"]',
}


def compact(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def reservation_state(text: str) -> str | None:
    if re.search("已预约|预约成功", text):
        return "reserved"
    if re.search("已设置开抢提醒|开抢提醒已设置|已设开抢提醒", text):
        return "reminder_set"
    return None


def money_cents(text: str) -> int:
    text = text.replace(",", "").strip()
    match = re.fullmatch(r"(?:应付总额[:：]?\s*)?[¥￥]?\s*(\d+(?:\.\d{1,2})?)(?:元)?", text)
    if not match:
        raise NeedsHuman("应付总额未知或格式无法确认，请检查结算页")
    try:
        return int(Decimal(match.group(1)) * 100)
    except InvalidOperation:
        raise NeedsHuman("无法读取应付总额") from None


def parse_product(html: str, sku: str) -> Product:
    match = re.search(r"(?:window\.)?_itemOnly\s*=\s*\(?\s*(\{)", html)
    if not match:
        raise NeedsHuman("无法读取京东商品信息，可能需要登录、页面校准或人工验证")
    try:
        item = json.JSONDecoder().raw_decode(html[match.start(1):])[0]["item"]
        if str(item["skuId"]) != sku:
            raise NeedsHuman("返回的商品编号与目标不一致")
        variant = next((v for v in item.get("newColorSize", []) if str(v.get("skuId")) == sku), {})
        spec = " / ".join(str(variant[k]) for k in ("size", "spec") if variant.get(k))
        title = str(item["skuName"])
        # The public mobile HTML sometimes shortens skuName. Keep explicit
        # variant fields so the requested edition/size remains visible.
        yuyue = re.search(r'"yuyue"\s*:\s*(\{[^{}]*\})', html, re.S)
        reservation = json.loads(yuyue.group(1)) if yuyue else {}
        def stamp(key):
            raw = reservation.get(key)
            if not raw:
                return None
            return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CHINA).isoformat()
        return Product(sku=sku, title=title, spec=spec,
                       url=f"https://item.jd.com/{sku}.html",
                       reservation_start=stamp("yueStime"), reservation_end=stamp("yueEtime"),
                       sale_start=stamp("qiangStime"), sale_end=stamp("qiangEtime"),
                       reservation_required=bool(reservation))
    except (ValueError, KeyError, TypeError):
        raise NeedsHuman("京东商品信息格式发生变化，需要校准解析器") from None


def desktop_time(text: str, suffix: str, now: datetime | None = None) -> str | None:
    now = now or datetime.now(CHINA)
    date_part = r"(?:(?:\d{4}[-/])?\d{1,2}[-/]\d{1,2}|(?:\d{4}年)?\d{1,2}月\d{1,2}日|今天|明天|后天)?"
    match = re.search(rf"({date_part})\s*(\d{{1,2}}:\d{{2}}(?::\d{{2}})?)\s*{re.escape(suffix)}", text)
    if not match:
        return None
    date, clock = match.groups()
    if date in {"", "今天", "明天", "后天"}:
        day = (now + timedelta(days={"":0,"今天":0,"明天":1,"后天":2}[date])).date()
    else:
        normalized = date.replace("年","-").replace("月","-").replace("日", "").replace("/","-")
        parts = [int(part) for part in normalized.split("-")]
        if len(parts) == 2:
            parts.insert(0, now.year)
        day = datetime(*parts).date()
    hour, minute, *second = [int(part) for part in clock.split(":")]
    return datetime(day.year, day.month, day.day, hour, minute, second[0] if second else 0, tzinfo=CHINA).isoformat()


async def desktop_product(page, sku: str) -> Product:
    await page.locator(".page-right-information .sku-title-name").wait_for(state="visible", timeout=15000)
    panel = page.locator(".page-right-information")
    await page.wait_for_function("""() => {
      const p=document.querySelector('.page-right-information');
      return p && /截止预约|开始抢购|立即购买|加入购物车|到货通知|售完|售罄/.test(document.body.innerText);
    }""", timeout=15000)
    if sku == "100327520619":
        await page.wait_for_function("""() => {
          const specs=[...document.querySelectorAll('.page-right-information .specification-item-sku--selected')].map(e=>e.innerText);
          return specs.some(t=>t.includes('限定版')) && specs.some(t=>t.includes('M码'));
        }""", timeout=10000)
    title = (await panel.locator(".sku-title-name").inner_text()).strip()
    specs = [(await node.inner_text()).strip() for node in await panel.locator(".specification-item-sku--selected").all()]
    text = await panel.inner_text()
    if not title:
        raise NeedsHuman("无法确认电脑商品页的标题")
    if sku == "100327520619" and not any("限定版" in spec for spec in specs):
        raise NeedsHuman("电脑商品页尚未确认限定版本，请检查已选规格")
    return Product(sku=sku,title=title,spec=" / ".join(specs),url=f"https://item.jd.com/{sku}.html",
                   reservation_required=bool(re.search("截止预约|立即预约|已预约|预约中",text)),
                   reservation_end=desktop_time(text,"截止预约"),sale_start=desktop_time(text,"开始抢购"))


def allowed_url(url: str) -> bool:
    parts = urlparse(url)
    host = (parts.hostname or "").lower()
    return (parts.scheme == "https" and not parts.username and not parts.password
            and parts.port in (None, 443)
            and (host == "3.cn" or host == "jd.com" or host.endswith(".jd.com")))


async def resolve_source(source: str) -> str:
    source = source.strip()
    if re.fullmatch(r"\d{8,16}", source):
        return source
    match = re.search(r"https?://[^\s「」<>]+", source)
    if not match:
        raise ValueError("请粘贴京东分享文案、商品链接或 SKU")
    url = match.group().rstrip("，。；,;)")
    async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
        for _ in range(6):
            if not allowed_url(url):
                raise ValueError("仅支持 HTTPS 京东商品链接和 3.cn 分享短链")
            direct = re.search(r"/(?:product/)?(\d{8,16})\.html", urlparse(url).path)
            if direct:
                return direct.group(1)
            response = await client.get(url)
            if response.status_code not in {301, 302, 303, 307, 308}:
                break
            url = urljoin(url, response.headers.get("location", ""))
    raise ValueError("短链没有解析到有效商品，请复制商品页完整链接或 SKU")


async def inspect_product(source: str, browser=None) -> Product:
    if browser is not None:
        check_access(browser)
    sku = await resolve_source(source)
    if browser is not None:
        context = await browser.ensure()
        page = await context.new_page()
        keep_open = False
        try:
            await page.goto(f"https://item.jd.com/{sku}.html", wait_until="domcontentloaded")
            await check_page_access(browser, page)
            if any(marker in page.url for marker in ("risk_handler", "captcha", "passport", "plogin", "/common/limit.html")):
                await page.bring_to_front()
                raise NeedsHuman("京东要求登录或人工验证，请在应用浏览器中处理后重新解析")
            if urlparse(page.url).path != f"/{sku}.html":
                raise NeedsHuman("电脑商品页发生非预期跳转，请人工确认目标 SKU")
            return await desktop_product(page, sku)
        except NeedsHuman:
            keep_open = True
            await page.bring_to_front()
            await check_page_access(browser, page)
            folder = browser.data / "diagnostics"
            folder.mkdir(parents=True, exist_ok=True)
            html = await page.content()
            diagnostic = {"host":urlparse(page.url).hostname, "path":urlparse(page.url).path,
                          "title":await page.title(), "html_size":len(html),
                          "has_item_data": "_itemOnly" in html,
                          "security_prompt":bool(re.search("安全验证|滑动验证|访问过于频繁", await page.locator("body").inner_text()))}
            (folder / "product.json").write_text(json.dumps(diagnostic,ensure_ascii=False,indent=2),encoding="utf-8")
            await page.screenshot(path=str(folder / "product.png"),mask=[page.locator("#addrArea,[class*='addr']")])
            raise
        except Exception:
            try:
                await check_page_access(browser, page)
            except AccessLimited:
                keep_open = True
                await page.bring_to_front()
                raise
            raise
        finally:
            # Leave a verification/login page visible for the user.
            if not keep_open and not any(marker in page.url for marker in ("risk_handler", "captcha", "passport", "plogin")):
                await page.close()
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
        response = await client.get(f"https://item.m.jd.com/product/{sku}.html")
        if response.is_redirect:
            raise NeedsHuman("京东要求浏览器访问，请先打开应用登录浏览器再解析商品")
        response.raise_for_status()
    return parse_product(response.text, sku)


class JDAdapter:
    def __init__(self, browser, data: Path, urls: JDUrls | None = None):
        self.payment_flow = "not_attempted"
        self.payment_entered = False
        self.payment_order_id = None
        self.payment_wait_seconds = 15
        self.payment_pages_before = set()
        self.cart_ready = False
        self.cart_attempts = 0
        self.last_cart_refresh = float('-inf')
        self.cart_refreshes = 0
        self.browser, self.data = browser, data
        self.urls = urls or JDUrls()
        self.page = None
        self.submitted_at: datetime | None = None
        self.submit_probe = None
        self.expected_order_id = None
        self.on_submit_order = None
        self.on_diagnostic = None
        self.before_api_dispatch = None
        self.api_bridge = None
        self.api_signal = SubmitSignal()
        self.checkout_open = False
        self.checkout_proof = None
        self.checked_checkout = None

    async def _wait_product_ready(self):
        # Text locators can pick an earlier hidden promotion element. Wait on
        # rendered body text instead, including human-intervention messages.
        await self.page.wait_for_function("""() => /已预约|立即预约|开抢|截止预约|加入购物车|立即购买|到货通知|售完|售罄|安全验证|滑动验证|请登录|访问过于频繁|操作过于频繁|当前页面异常/.test(document.body.innerText)""", timeout=15000)

    async def probe(self, product: Product):
        """Read-only preparation check; no reservation, selection or checkout."""
        check_access(self.browser)
        self.probe_step = "打开商品页"
        self.page = await self.browser.task_page()
        await self.page.goto(self.urls.product.format(sku=product.sku), wait_until="domcontentloaded")
        self.probe_step = "等待商品页预约状态"
        await self._wait_product_ready()
        text = await self._guard()
        booking = await self._reservation_status(text)
        reserved = booking == "reserved"
        self.probe_step = "打开电脑购物车"
        await self.page.goto(self.urls.cart, wait_until="domcontentloaded")
        link = self.page.locator(f'a[href*="/{product.sku}.html"]').first
        self.probe_step = "定位购物车目标商品"
        cart_found = True
        try:
            await link.wait_for(state="visible", timeout=6000)
        except Exception:
            cart_found = False
        await self._guard()
        found = selectable = False
        cart_quantity = None
        if cart_found:
            _, row, checkbox = await self._cart_target(product.sku)
            found = await checkbox.count() == 1
            selectable = await checkbox.is_enabled() if found else False
            quantity = await self._cart_quantity_input(row)
            value = (await quantity.input_value()).strip()
            if re.fullmatch(r'\d+',value):
                cart_quantity = int(value)
        self.probe_step = "读取新版订单列表"
        orders = await self._orders()
        return {"sku": product.sku, "reserved": reserved, "reminder_set":booking == "reminder_set",
                "reservation_status":booking, "cart_found": cart_found,
                "checkbox_found": found, "cart_selectable": selectable,
                "cart_quantity": cart_quantity,
                "order_list_readable": True, "checked_at": datetime.now(CHINA).isoformat(),
                "message": "只读检查完成；未加购的商品将在启用任务时自动加入购物车。"}

    async def _guard(self):
        text = await check_page_access(self.browser, self.page)
        url = self.page.url.lower()
        if any(mark in url for mark in ("captcha", "verify", "pc-frequent", "reason=403")) or re.search("滑动验证|安全验证|请完成验证|访问过于频繁|拖动滑块", text):
            raise NeedsHuman("京东要求人工验证或限制访问，请在电脑浏览器处理后恢复")
        login_pattern = r"你好[，,]?\s*请登录|请先登录"
        if re.search(login_pattern, text) and "passport.jd.com" not in url and "plogin.m.jd.com" not in url:
            try:
                await self.page.wait_for_function("() => !/你好[，,]?\\s*请登录|请先登录/.test(document.body.innerText)",timeout=3000)
                text = await self.page.locator("body").inner_text()
            except Exception:
                pass
        if "passport.jd.com" in url or "plogin.m.jd.com" in url or re.search(login_pattern, text):
            raise NeedsHuman("京东登录已失效，请在电脑浏览器登录后恢复")
        if re.search("未获得抢购资格|没有抢购资格|请先预约",text):
            raise NeedsHuman("京东尚未确认当前场次抢购资格，请先处理预约要求")
        return text

    async def _reservation_status(self, text: str | None = None):
        nodes = self.page.locator(SELECTORS["reservation_action"])
        if await self.page.locator(".page-right-RightBtns").count():
            await nodes.first.wait_for(state="visible",timeout=10000)
        labels = [await node.inner_text() for node in await nodes.all() if await node.is_visible()]
        return reservation_state("\n".join(labels) if labels else text or await self._guard())

    async def prepare(self, config: TaskConfig) -> Product:
        check_access(self.browser)
        self.page = await self.browser.task_page()
        await self.page.goto(self.urls.product.format(sku=config.product.sku), wait_until="domcontentloaded")
        await self._wait_product_ready()
        text = await self._guard()
        session = await self.browser.status()
        if not session["logged_in"]:
            raise NeedsHuman("未检测到有效京东登录，请打开登录浏览器完成登录")
        # Re-read fresh public metadata. Desktop pages may render all product
        # data from XHR rather than embed mobile _itemOnly.
        if self.urls.product.startswith("https://"):
            if urlparse(self.page.url).path != f"/{config.product.sku}.html":
                raise NeedsHuman("商品页跳转与目标 SKU 不符")
            product = await desktop_product(self.page, config.product.sku)
        else:
            product = parse_product(await self.page.content(), config.product.sku)
        if product.sku != config.product.sku or (config.product.spec and compact(product.spec) != compact(config.product.spec)):
            raise NeedsHuman("商品规格与保存的任务不一致，请重新解析并确认任务")
        if config.product.sku == "100327520619" and not ("限定版" in product.spec and "M码" in product.spec):
            raise NeedsHuman("目标必须为 bilibili 限定版 M 码，请重新确认 SKU")
        return product

    async def reserve(self, config: TaskConfig):
        if not config.product.reservation_required:
            return "not_required"
        # Re-read the current action area instead of a cached preflight text.
        text = await self._guard()
        state = await self._reservation_status(text)
        if state:
            return state
        now = datetime.now(CHINA)
        if config.product.reservation_end and now >= datetime.fromisoformat(config.product.reservation_end):
            raise NeedsHuman("预约已截止，当前账号未确认预约资格")
        if config.product.reservation_start and now < datetime.fromisoformat(config.product.reservation_start):
            raise NeedsHuman("预约尚未开始，请在预约开放后恢复")
        names = {"立即预约", "预约抢购", "设置开抢提醒", "设置提醒"}
        candidates = [node for node in await self.page.locator(SELECTORS["reservation_action"]).all()
                      if await node.is_visible() and (await node.inner_text()).strip() in names]
        if not candidates:
            for label in names:
                candidates.extend([node for node in await self.page.get_by_text(label,exact=True).all()
                                   if await node.is_visible()])
        if len(candidates) != 1:
            raise NeedsHuman("未找到唯一可见的预约或开抢提醒入口，请检查京东页面")
        await candidates[0].click()
        await self.page.wait_for_function("() => /已预约|预约成功|已设置开抢提醒|开抢提醒已设置|已设开抢提醒/.test(document.body.innerText)", timeout=12000)
        await self._guard()
        state = await self._reservation_status()
        if not state:
            raise NeedsHuman("预约动作后未取得明确状态，请检查京东页面")
        return state

    async def baseline(self, config: TaskConfig) -> list[str]:
        orders = await self._orders(config)
        if any(order["skus"] == {config.product.sku} and order["pending"] for order in orders):
            raise NeedsHuman("订单列表已有该商品的待付款订单，请先核对并付款")
        if any(not order["skus"] and order["pending"] for order in orders):
            raise NeedsHuman("已有待付款订单但无法确认其商品，请先人工核对订单列表")
        return [order["id"] for order in orders]

    async def _cart_target(self, sku: str):
        link = self.page.locator(f'a[href*="/{sku}.html"]').first
        # New JD cart puts checkbox/title in _good-info, but quantity in a
        # sibling column. The complete SKU row is identified by data-rowgid.
        row = link.locator('xpath=ancestor::*[@data-rowgid][1]')
        if not await row.count():
            row = link.locator("xpath=ancestor::*[.//*[@role='checkbox' or @type='checkbox'] and .//input[not(@type='checkbox') and not(@type='hidden')]][1]")
        if not await row.count():
            row = link.locator("xpath=ancestor::*[.//*[@role='checkbox' or @type='checkbox']][1]")
        return link, row, row.get_by_role("checkbox")

    async def _cart_quantity_input(self, row):
        nodes = row.locator(SELECTORS['cart_quantity'])
        try:
            await nodes.filter(visible=True).first.wait_for(state='visible',timeout=5000)
        except Exception:
            raise NeedsHuman("购物车目标数量控件尚未出现，请检查商品行；未继续结算") from None
        visible = [el for el in await nodes.all() if await el.is_visible()]
        if len(visible)!=1:
            raise NeedsHuman("购物车目标数量无法唯一确认，已暂停")
        return visible[0]

    async def prepare_cart(self, config: TaskConfig):
        """Add only if absent, verify the exact SKU/quantity, then stay in cart."""
        check_access(self.browser)
        await self.page.goto(self.urls.cart, wait_until="domcontentloaded")
        link, _, _ = await self._cart_target(config.product.sku)
        try:
            await link.wait_for(state="visible", timeout=6000)
        except Exception:
            await self._guard()
            await self.page.goto(self.urls.product.format(sku=config.product.sku), wait_until="domcontentloaded")
            await self._wait_product_ready()
            await self._guard()
            if self.urls.product.startswith("https://"):
                current = await desktop_product(self.page, config.product.sku)
                if current.sku != config.product.sku or compact(current.spec) != compact(config.product.spec):
                    raise NeedsHuman("加购前商品规格发生变化，已暂停")
            scope = self.page.locator('.page-right-RightBtns')
            candidates = []
            if await scope.count():
                candidates = [el for el in await scope.get_by_text("加入购物车", exact=True).all() if await el.is_visible()]
            if not candidates:
                candidates = [el for el in await self.page.get_by_text("加入购物车",exact=True).all() if await el.is_visible()]
            if len(candidates) != 1 or not await candidates[0].is_enabled():
                raise NeedsHuman("当前商品没有唯一可用的加入购物车入口，预约状态已保留，请检查页面")
            await candidates[0].click()
            # Never repeat the add click on a delayed response. Re-read cart.
            for attempt in range(3):
                await self.page.goto(self.urls.cart, wait_until="domcontentloaded")
                link, _, _ = await self._cart_target(config.product.sku)
                try:
                    await link.wait_for(state="visible",timeout=5000)
                    break
                except Exception:
                    await self._guard()
                    if attempt==2:
                        raise NeedsHuman("加购后仍未在购物车确认目标 SKU，请检查页面；未重复加购") from None
        await self._guard()
        _, row, checkbox = await self._cart_target(config.product.sku)
        if await checkbox.count() != 1:
            raise NeedsHuman("购物车目标行无法唯一定位，已暂停")
        quantity = await self._cart_quantity_input(row)
        if (await quantity.input_value()).strip() != str(config.quantity):
            if not await quantity.is_editable():
                raise NeedsHuman("购物车数量与任务不符且不能调整，已暂停")
            await quantity.fill(str(config.quantity))
            await quantity.press("Tab")
            await expect(quantity).to_have_value(str(config.quantity),timeout=10000)
        await self.page.locator('#global-loading').wait_for(state="hidden",timeout=15000)
        self.cart_ready = True
        self.cart_attempts = 0
        self.last_cart_refresh = time.monotonic()
        self.cart_refreshes = 0

    def cart_refresh_due(self):
        interval = min(5, 1.5 ** min(max(self.cart_refreshes-1, 0), 4))
        return time.monotonic() - self.last_cart_refresh >= interval

    async def refresh_cart(self):
        check_access(self.browser)
        await self.page.reload(wait_until='domcontentloaded')
        self.last_cart_refresh = time.monotonic()
        self.cart_refreshes += 1
        await self._guard()

    async def _cart_settle_button(self, config: TaskConfig, *, preflight=False):
        # JD can replace “去结算” with “领券结算”. The count also updates
        # asynchronously after selection. Ignore hidden responsive copies.
        pattern = re.compile(r"^(?:去结算|领券结算)\s*[（(]\s*1\s*[)）]$")
        until = config.purchase_end
        if config.product.sale_end:
            until = min(until, datetime.fromisoformat(config.product.sale_end))
        wait_seconds = min(5, max(0, (until-datetime.now(CHINA)).total_seconds()))
        if wait_seconds <= 0:
            raise NotReady("本场次结算窗口已结束，未点击结算")
        entries = self.page.get_by_text(pattern).filter(visible=True)
        try:
            await entries.first.wait_for(state='visible',timeout=max(1,int(wait_seconds*1000)))
        except Exception:
            await self._guard()
            raise NotReady("购物车单件结算入口尚未就绪，等待勾选及金额刷新") from None
        visible = await entries.all()
        if len(visible)!=1:
            raise NeedsHuman("存在多个可见的单件结算入口，请检查购物车")
        button = visible[0]
        if not await button.is_enabled() or await button.get_attribute('aria-disabled')=='true':
            raise NotReady("结算入口尚不可用，等待购物车刷新")
        if datetime.now(CHINA) < (config.checkout_prepare_at if preflight else config.purchase_start):
            raise NotReady("尚未到实际抢购时间，未点击结算")
        if datetime.now(CHINA) >= until:
            raise NotReady("本场次结算窗口已结束，未点击结算")
        return button

    async def checkout(self, config: TaskConfig) -> Checkout:
        return await self._checkout(config)

    async def prepare_checkout(self, config: TaskConfig) -> Checkout:
        try:
            return await self._checkout(config, preflight=True)
        except (NotReady, PlaywrightTimeoutError) as exc:
            await self._guard()
            self.checkout_open = False
            if not self.page.url.startswith(self.urls.cart):
                await self.page.goto(self.urls.cart, wait_until='domcontentloaded')
                await self._guard()
            if isinstance(exc, PlaywrightTimeoutError):
                raise NotReady('开售前结算页尚未准备好') from None
            raise

    async def _checkout(self, config: TaskConfig, *, preflight=False) -> Checkout:
        check_access(self.browser)
        if datetime.now(CHINA) < (config.checkout_prepare_at if preflight else config.purchase_start):
            raise NotReady("尚未到实际抢购开始时间，不进入购物车结算")
        if self.checkout_open:
            await self._guard()
            return await self.recheck_checkout(config, allow_pending_price=preflight)
        if not self.cart_ready:
            await self.prepare_cart(config)
        if not preflight:
            self.cart_attempts += 1
        if not self.page.url.startswith(self.urls.cart):
            await self.page.goto(self.urls.cart,wait_until="domcontentloaded")
        elif not preflight and self.cart_attempts > 1 and self.cart_refresh_due():
            await self.refresh_cart()
        link, row, checkbox = await self._cart_target(config.product.sku)
        try:
            await link.wait_for(state="visible", timeout=12000)
        except Exception:
            await self._guard()
            raise NeedsHuman("购物车中未找到目标 SKU，请在应用浏览器中将该规格加入购物车") from None
        await self._guard()
        if await checkbox.count() != 1:
            raise NeedsHuman("无法唯一定位目标商品勾选框，需要校准购物车")
        if not await checkbox.is_enabled():
            if not preflight and self.cart_attempts == 1:
                await self.refresh_cart()
                link, row, checkbox = await self._cart_target(config.product.sku)
                await link.wait_for(state="visible",timeout=10000)
            if not await checkbox.is_enabled():
                raise NotReady("预约商品尚未解锁勾选")
        # Store/all checkboxes cascade into the item rows. Operate on product
        # rows only, and wait for JD's async selection/price refresh each time.
        async def set_item(box, checked):
            await self.page.locator('#global-loading').wait_for(state="hidden", timeout=15000)
            if await box.is_checked() != checked:
                await box.click()
                if checked:
                    await expect(box).to_be_checked(timeout=15000)
                else:
                    await expect(box).not_to_be_checked(timeout=15000)
            await self.page.locator('#global-loading').wait_for(state="hidden", timeout=15000)

        # A product can have image/title links and hidden copies. Read the
        # selected SKU candidates in one DOM round trip, then revalidate each
        # actual checkbox before changing it. Unselected rows need no action.
        selected_skus = await self.page.locator('a[href*="item.jd.com/"]').evaluate_all(r"""links => {
            const selected = new Set();
            for (const link of links) {
                const match = link.href.match(/\/(\d{8,16})\.html/);
                if (!match) continue;
                let row = link.closest('[data-rowgid]');
                if (!row) {
                    row = link.parentElement;
                    while (row && !row.querySelector('input[type="checkbox"], [role="checkbox"]')) row = row.parentElement;
                }
                if (!row) continue;
                const boxes = row.querySelectorAll('input[type="checkbox"], [role="checkbox"]');
                if (boxes.length === 1 && (boxes[0].checked || boxes[0].getAttribute('aria-checked') === 'true')) selected.add(match[1]);
            }
            return [...selected];
        }""")
        for sku in selected_skus:
            if sku == config.product.sku:
                continue
            other_link = self.page.locator(f'a[href*="/{sku}.html"]').first
            other_row = other_link.locator('xpath=ancestor::*[@data-rowgid][1]')
            if not await other_row.count():
                other_row = other_link.locator("xpath=ancestor::*[.//*[@role='checkbox' or @type='checkbox']][1]")
            boxes = other_row.get_by_role("checkbox")
            if await boxes.count() == 1 and await boxes.is_visible() and await boxes.is_enabled():
                await set_item(boxes, False)
        await set_item(checkbox, True)
        if not await checkbox.is_checked():
            raise NotReady("购物车勾选未生效")
        settle = await self._cart_settle_button(config, preflight=preflight)
        await settle.click()
        goods = self.page.locator(SELECTORS["goods"])
        await goods.first.wait_for(state="visible", timeout=15000)
        await self._guard()
        await self.ensure_manual_payment()
        self.checkout_open = True
        return await self.read_checkout(config, allow_pending_price=True) if preflight else await self.read_checkout(config)

    async def ensure_manual_payment(self):
        """The new checkout defaults to credit. Select a manual QR cashier."""
        rows = self.page.locator('[class*="modePaymentLi-"]')
        if not await rows.count():
            return
        notice = self.page.get_by_text("我知道了", exact=True)
        if await notice.count() == 1 and await notice.is_visible():
            await notice.click()
        row = rows.filter(has_text=re.compile(r"^微信支付$"))
        if await row.count() != 1:
            raise NeedsHuman("未找到唯一的扫码手动付款入口，未使用白条或银行卡提交")
        if not await row.is_visible():
            more = self.page.get_by_text("更多支付方式", exact=True)
            if await more.count() == 1:
                await more.click()
        box = row.get_by_role("checkbox")
        if not await box.is_checked():
            await row.locator("label").click()
        await expect(box).to_be_checked(timeout=10000)

    async def check_manual_payment(self):
        rows = self.page.locator('[class*="modePaymentLi-"]')
        if not await rows.count():
            return False
        selected = []
        for row in await rows.all():
            box = row.get_by_role("checkbox")
            if await box.count() == 1 and await box.is_checked():
                selected.append((await row.inner_text()).strip())
        if selected != ["微信支付"]:
            raise NeedsHuman("付款方式不是唯一的微信扫码手动付款，已暂停提交")
        return True

    async def recheck_checkout(self, config: TaskConfig, *, allow_pending_price=False) -> Checkout:
        await self._guard()
        if self.checkout_proof is not None and self.checked_checkout is not None and await self.checkout_proof.matches(config):
            return self.checked_checkout
        return await self.read_checkout(config, allow_pending_price=True) if allow_pending_price else await self.read_checkout(config)

    async def read_checkout(self, config: TaskConfig, *, allow_pending_price=False) -> Checkout:
        proof = await CheckoutProof.capture(self.page,
            [SELECTORS[key] for key in (('goods','address') if config.ignore_price else ('goods','total','address'))]+['[class*="modePaymentLi-"]'], config)
        try:
            checked = await self._read_checkout_fields(config, allow_pending_price=allow_pending_price)
            if not await proof.matches(config):
                raise NotReady('结算信息在核对时发生变化，等待页面稳定后重新核对')
        except BaseException:
            await proof.close()
            raise
        if self.checkout_proof is not None:
            await self.checkout_proof.close()
        self.checkout_proof, self.checked_checkout = proof, checked
        return checked

    async def _read_checkout_fields(self, config: TaskConfig, *, allow_pending_price=False) -> Checkout:
        goods = self.page.locator(SELECTORS["goods"])
        links = await goods.locator('a[href*="item.jd.com/"]').all()
        skus = set()
        for link in links:
            match = re.search(r"/(\d{8,16})\.html", await link.get_attribute("href") or "")
            if match:
                skus.add(match.group(1))
        modern = self.page.locator('.sku-item')
        if await modern.count():
            # New JD checkout has clickable titles rather than SKU hyperlinks.
            # Follow each actual UI detail action and verify its destination.
            for row in await modern.all():
                title = row.locator('[class^="title-"] [class^="ellipsis-"]')
                if await title.count() != 1:
                    raise NeedsHuman("结算商品详情入口不明确，已暂停提交")
                async with self.page.expect_popup(timeout=10000) as popup:
                    await title.click()
                detail = await popup.value
                try:
                    # SKU evidence is the committed destination URL; loading
                    # the product document/scripts adds no identity evidence.
                    deadline = time.monotonic() + 10
                    while detail.url == 'about:blank':
                        if detail.is_closed() or time.monotonic() >= deadline:
                            raise NeedsHuman("结算商品详情跳转超时，无法确认精确 SKU")
                        # Frame URL updates on navigation commit. Avoid a load
                        # state wait: some drivers never emit 'commit' as a
                        # load-state event for an already committed popup.
                        await asyncio.sleep(.01)
                    parsed = urlparse(detail.url)
                    match = re.fullmatch(r"/(\d{8,16})\.html", parsed.path)
                    if parsed.hostname != "item.jd.com" or not match:
                        raise NeedsHuman("结算商品详情无法确认精确 SKU")
                    skus.add(match.group(1))
                finally:
                    await detail.close()
        if skus != {config.product.sku}:
            raise NeedsHuman("结算清单不只包含目标 SKU，已暂停提交")
        amounts = []
        for item in await goods.locator(SELECTORS["quantity"]).all():
            if await item.is_visible():
                text = (await item.inner_text()).strip()
                match = re.fullmatch(r"[xX×*]?\s*(\d+)\s*(?:件|个)?", text)
                if match:
                    amounts.append(int(match.group(1)))
        if not amounts or sum(amounts) != config.quantity:
            if await modern.count():
                amounts = []
                for row in await modern.all():
                    quantity = row.locator('[class^="quantity-"]')
                    if await quantity.count() != 1:
                        raise NeedsHuman("结算数量无法确认，已暂停提交")
                    match = re.fullmatch(r"(?:\d+(?:\.\d+)?\s*kg\s*)?×\s*(\d+)", (await quantity.inner_text()).strip())
                    if not match:
                        raise NeedsHuman("结算数量格式无法确认，已暂停提交")
                    amounts.append(int(match.group(1)))
            if not amounts or sum(amounts) != config.quantity:
                raise NeedsHuman("结算数量无法确认或与任务不符，已暂停提交")
        total = None
        if not config.ignore_price:
            totals = set()
            for item in await self.page.locator(SELECTORS["total"]).all():
                if await item.is_visible():
                    label = await item.inner_text()
                    try:
                        totals.add(money_cents(label))
                    except NeedsHuman:
                        if allow_pending_price and re.search('待发布|待公布|暂无价格|开售后', label):
                            raise NotReady('开售前应付价格尚未公布') from None
                        raise
            if len(totals) != 1:
                raise NeedsHuman("应付总额未知或存在歧义，请检查结算页")
            total = totals.pop()
        addresses = [await el.inner_text() for el in await self.page.locator(SELECTORS["address"]).all() if await el.is_visible()]
        if not any(compact(config.address_hint) in compact(address) for address in addresses):
            raise NeedsHuman("选中的收货地址与任务关键字不符，已暂停提交")
        if not config.price_allowed(total):
            raise NeedsHuman(f"应付总额 ¥{total / 100:.2f} 超过上限或无效，已暂停提交")
        await self.check_manual_payment()
        return Checkout(sku=config.product.sku, quantity=sum(amounts), total_cents=total, address="已核对")

    async def submit(self, config: TaskConfig):
        if datetime.now(CHINA) < config.purchase_start:
            raise NeedsHuman("尚未到实际抢购开始时间，未提交订单")
        # Revalidate immediately before the irreversible click.
        await self._guard()
        checked = await self.recheck_checkout(config)
        button = self.page.locator(SELECTORS["submit"])
        visible = [el for el in await button.all() if await el.is_visible()]
        if len(visible) != 1 or not await visible[0].is_enabled():
            raise NeedsHuman("提交按钮不可用或存在歧义，请在电脑检查结算页")
        label = (await visible[0].inner_text()).strip()
        manual_qr = await self.check_manual_payment()
        if label != "提交订单" and not (manual_qr and label == "立即支付"):
            raise NeedsHuman("提交入口可能包含支付或信用服务，已暂停操作")
        end = config.purchase_end
        if config.product.sale_end:
            end = min(end, datetime.fromisoformat(config.product.sale_end))
        if datetime.now(CHINA) < config.purchase_start:
            raise NeedsHuman("尚未到实际抢购开始时间，未提交订单")
        if datetime.now(CHINA) >= end:
            raise NeedsHuman("本场次提交窗口已结束，未执行提交")
        self.submitted_at = datetime.now(CHINA)
        self.payment_pages_before = set((await self.browser.ensure()).pages)
        if config.submit_mode == 'api':
            if not manual_qr or self.before_api_dispatch is None:
                raise NeedsHuman('API 试验要求已核对的手动扫码付款和持久提交保护；未派发请求')
            self.api_bridge = PageSignedSubmit(self.browser, self.page, self.before_api_dispatch, self._api_order)
            try:
                self.api_signal = await self.api_bridge.submit(config, checked, visible[0], self.recheck_checkout)
            except Exception:
                if self.api_bridge.forwarded == 0:
                    self.api_signal = SubmitSignal('rejected')
                raise
            finally:
                if self.on_diagnostic is not None:
                    self.on_diagnostic(f'API 实验提交：实际派发 {self.api_bridge.forwarded} 次，'
                                       f'HTTP {self.api_bridge.status or "未确认"}；禁止回退 DOM 重复提交')
            return
        # Observe the request emitted by this exact click. Never send/replay a
        # second request, even when the browser or server response is unclear.
        self.submit_probe = SubmitProbe(self.browser, self.page, self._api_order)
        self.submit_probe.start()
        await visible[0].click(timeout=10000)

    def _api_order(self, order_id):
        self.expected_order_id = order_id
        if self.on_submit_order is not None:
            self.on_submit_order(order_id)

    async def aclose(self):
        if self.checkout_proof is not None:
            await self.checkout_proof.close()
            self.checkout_proof = None
        if self.api_bridge is not None:
            await self.api_bridge.close()
        if self.submit_probe is not None:
            await self.submit_probe.close()
            if self.on_diagnostic is not None:
                for sample in self.submit_probe.samples:
                    self.on_diagnostic(f"提交 API 响应：{sample['endpoint']}，HTTP {sample['http_status']}，"
                                       f"响应头 {sample['headers_ms']} 毫秒，取证 {sample['body_ms']} 毫秒，状态 {sample['signal']}")
            self.submit_probe = None

    async def enter_payment(self, config: TaskConfig) -> str | None:
        """Observe a real cashier, opening a safe post-order payment entry once."""
        context = await self.browser.ensure()
        deadline = time.monotonic()+self.payment_wait_seconds
        opened = False
        while time.monotonic() < deadline:
            if self.api_signal.kind == 'rejected':
                raise NeedsHuman('API 试验请求未成功，停止等待付款页；只核验，不回退重复提交')
            if self.submit_probe is not None:
                self.submit_probe.check()
            candidates = [self.page]+[p for p in context.pages if p not in self.payment_pages_before and p is not self.page]
            for page in candidates:
                if page is None or page.is_closed():
                    continue
                try:
                    text = await check_page_access(self.browser, page)
                    dialog = page.locator(SELECTORS['payment_dialog'])
                    marker = page.locator('[data-testid="payment-cashier"]')
                    inline = await dialog.count()==1 and await dialog.is_visible() and "微信APP扫码支付" in await dialog.inner_text()
                    fullpage = urlparse(page.url).hostname in {'pcashier.jd.com','payc.m.jd.com'} and bool(re.search('订单提交成功|扫描二维码支付|收银台',text))
                    fixture = await marker.count()==1 and await marker.is_visible()
                    if inline or fullpage or fixture:
                        self.page = page
                        self.payment_entered = True
                        self.payment_flow = 'entered'
                        # The cashier can appear between the initial body read
                        # and the locator checks. Read its current evidence,
                        # otherwise a newly rendered order ID can be missed.
                        payment_text = await (dialog if inline else marker if fixture else page.locator('body')).inner_text()
                        match = re.search(r'订单(?:编号|号)\s*[:：]?\s*(\d{8,})',payment_text)
                        self.payment_order_id = match.group(1) if match else None
                        return self.payment_order_id
                    if not opened and re.search('订单提交成功|下单成功',text):
                        entries = [el for el in await page.get_by_text(re.compile(r'^(?:去付款|去支付|立即付款)$')).all() if await el.is_visible()]
                        if len(entries)==1:
                            opened = True
                            await entries[0].click()
                except AccessLimited:
                    raise
                except Exception:
                    continue
            await asyncio.sleep(.15)
        self.payment_flow = 'not_confirmed'
        raise NeedsHuman("未能确认已进入付款界面；需查询订单，不能重复提交")

    async def lock_order(self, config: TaskConfig):
        if not self.payment_entered:
            raise NeedsHuman("付款界面尚未确认，不能报告锁单成功")
        try:
            await self.exit_payment_dialog()
            if not self.page.url.startswith(self.urls.orders):
                await self.page.goto(self.urls.orders,wait_until='domcontentloaded')
        except Exception:
            self.payment_flow = 'not_confirmed'
            raise

    async def exit_payment_dialog(self):
        # The checkout's “立即支付” creates the order and opens a WeChat QR
        # dialog. Closing this dialog cancels only the payment interaction.
        fixture = self.page.locator('[data-testid="payment-cashier"]')
        if await fixture.count()==1 and await fixture.is_visible():
            close = fixture.get_by_test_id('payment-exit')
            if await close.count()!=1 or (await close.inner_text()).strip()!='取消支付':
                raise NeedsHuman("退出付款入口不明确")
            await close.click()
            await fixture.wait_for(state='hidden')
            self.payment_flow='entered_and_exited'
            return
        if urlparse(self.page.url).hostname in {"pcashier.jd.com", "payc.m.jd.com"}:
            text = await self.page.locator("body").inner_text()
            if not ("订单提交成功" in text or ("微信支付" in text and "扫描二维码支付" in text)):
                raise NeedsHuman("无法确认手动扫码支付页面，请人工退出支付")
            # This cashier has no cancel button. Leaving it exits payment;
            # only the following order verification determines success.
            await self.page.goto(self.urls.orders, wait_until="domcontentloaded")
            self.payment_flow = "entered_and_exited"
            return
        dialog = self.page.locator(SELECTORS["payment_dialog"])
        await dialog.wait_for(state="visible", timeout=15000)
        if "微信APP扫码支付" not in await dialog.inner_text():
            raise NeedsHuman("订单可能已创建，但无法确认扫码支付页面，请核对订单")
        close = dialog.locator(SELECTORS["payment_close"])
        if await close.count() != 1 or not await close.is_visible():
            raise NeedsHuman("订单可能已创建，但支付弹窗退出入口不明确，请手动关闭")
        await close.click()
        await dialog.wait_for(state="hidden", timeout=10000)
        self.payment_flow = "entered_and_exited"

    async def _detail_skus(self, card, page, order_id: str) -> set[str]:
        """Inspect only a newly created candidate's detail, never older orders."""
        detail = card.get_by_text("订单详情", exact=True)
        if await detail.count() != 1:
            return set()
        target = None
        try:
            try:
                async with page.expect_popup(timeout=4000) as popup:
                    await detail.click()
                target = await popup.value
            except Exception:
                target = page
            await target.wait_for_load_state("domcontentloaded")
            text = await target.locator("body").inner_text()
            if not re.search(r"订单(?:编号|号)\s*[:：]?\s*" + re.escape(order_id) + r"\b", text):
                return set()
            result = set()
            for link in await target.locator('a[href*="item.jd.com/"],a[href*="item.m.jd.com/product/"]').all():
                match = re.search(r"/(\d{8,16})\.html", await link.get_attribute("href") or "")
                if match:
                    result.add(match.group(1))
            return result
        finally:
            if target is not None and target is not page:
                await target.close()

    async def _orders(self, config: TaskConfig | None = None, since: datetime | None = None, baseline: list[str] | None = None):
        check_access(self.browser)
        context = await self.browser.ensure()
        page = await context.new_page()
        restricted = False
        try:
            await page.goto(self.urls.orders, wait_until="domcontentloaded")
            await check_page_access(self.browser, page)
            cards = page.locator(SELECTORS["order_cards"])
            try:
                await page.wait_for_function("""selector => document.querySelector(selector) ||
                    /没有相关订单|暂无订单|还没有订单/.test(document.body.innerText)""",
                    arg=SELECTORS["order_cards"], timeout=10000)
                await check_page_access(self.browser, page)
                if not await cards.count():
                    return []
            except Exception:
                text = await check_page_access(self.browser, page)
                if any(s in text for s in ("没有相关订单", "暂无订单", "还没有订单")):
                    return []
                raise NeedsHuman("无法读取订单列表，需人工核对后恢复") from None
            await check_page_access(self.browser, page)
            orders = []
            for card in await cards.all():
                if await card.locator(SELECTORS["order_cards"]).count():
                    continue
                raw_id = await card.get_attribute("data-order-id") or await card.get_attribute("id") or ""
                id_match = re.search(r"(\d{8,})", raw_id)
                if not id_match:
                    order_link = card.locator('a[href*="orderId="], a[href*="orderid="]')
                    if await order_link.count():
                        id_match = re.search(r"(?:orderId|orderid)=(\d{8,})", await order_link.first.get_attribute("href") or "")
                if not id_match:
                    id_match = re.search(r"订单(?:编号|号)\s*[:：]?\s*(\d{8,})", await card.inner_text())
                if not id_match:
                    raise NeedsHuman("订单号解析失败，需要校准订单列表")
                text = await card.inner_text()
                sku_ids = set()
                for link in await card.locator('a[href*="item.jd.com/"]').all():
                    m = re.search(r"/(\d{8,16})\.html", await link.get_attribute("href") or "")
                    if m:
                        sku_ids.add(m.group(1))
                stamp = None
                time_nodes = card.locator(SELECTORS["order_time"])
                if await time_nodes.count():
                    raw_time = await time_nodes.first.get_attribute("title") or await time_nodes.first.inner_text()
                    m = re.search(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}", raw_time)
                    if m:
                        stamp = datetime.strptime(m.group(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=CHINA)
                total = None
                for node in await card.locator(SELECTORS["order_total"]).all():
                    try:
                        total = money_cents(await node.inner_text())
                        break
                    except NeedsHuman:
                        continue
                quantities = []
                for node in await card.locator(SELECTORS["quantity"]).all():
                    m = re.fullmatch(r"[xX×*]?\s*(\d+)\s*(?:件|个)?", (await node.inner_text()).strip())
                    if m:
                        quantities.append(int(m.group(1)))
                pending = bool(re.search("待付款|等待付款|等待支付|立即付款", text))
                navigated = False
                if (not sku_ids and config is not None and since is not None and stamp is not None and pending
                    and id_match.group(1) not in (baseline or [])
                    and since - timedelta(seconds=5) <= stamp <= datetime.now(CHINA) + timedelta(seconds=5)
                    and config.price_allowed(total)):
                    before = page.url
                    sku_ids = await self._detail_skus(card, page, id_match.group(1))
                    navigated = page.url != before
                orders.append({"id": id_match.group(1), "skus": sku_ids, "stamp": stamp, "total": total,
                               "quantity": sum(quantities) if quantities else None,
                               "pending": pending})
                if navigated:
                    break
            return orders
        except AccessLimited:
            restricted = True
            self.page = page
            await page.bring_to_front()
            raise
        finally:
            if not restricted:
                await page.close()

    async def verify(self, config: TaskConfig, baseline: list[str], since: datetime | None = None) -> OrderResult:
        since = since or self.submitted_at or config.scheduled_at
        probe = self.submit_probe
        await self.aclose()
        if probe is not None and probe.signal.kind in {'restricted', 'conflict'}:
            try:
                probe.check()
            except NeedsHuman as exc:
                return OrderResult(outcome='unknown', order_id=self.expected_order_id, message=str(exc))
        for attempt in range(3):
            try:
                orders = await self._orders(config, since=since, baseline=baseline)
                matches = [o for o in orders if o["id"] not in baseline
                           and (not self.expected_order_id or o['id'] == self.expected_order_id)
                           and o["skus"] == {config.product.sku} and o["pending"]
                           and o["quantity"] == config.quantity
                           and o["stamp"] and since - timedelta(seconds=5) <= o["stamp"] <= datetime.now(CHINA) + timedelta(seconds=5)
                           and config.price_allowed(o['total'])]
                if len(matches) == 1:
                    order = matches[0]
                    message = "锁单成功，待付款；已进入并退出扫码支付" if self.payment_flow == "entered_and_exited" else "锁单成功，待付款；已核对订单号和目标商品"
                    if self.payment_flow == "not_confirmed":
                        message += "；支付页退出状态需人工核对"
                    return OrderResult(outcome="created", message=message,
                                       order_id=order["id"], sku=config.product.sku, total_cents=order["total"],
                                       url=self.urls.orders, payment_flow=self.payment_flow, payment_entered=self.payment_entered)
                if len(matches) > 1:
                    return OrderResult(outcome="unknown", message="发现多个可能匹配的订单，请人工核对")
            except AccessLimited as exc:
                return OrderResult(outcome='unknown', order_id=self.expected_order_id, message=str(exc))
            except NeedsHuman as exc:
                return OrderResult(outcome='unknown', order_id=self.expected_order_id, message=str(exc)+'；不会自动重复提交')
            except Exception:
                pass
            if attempt < 2:
                await asyncio.sleep(2)
        if self.expected_order_id:
            return OrderResult(outcome='unknown', order_id=self.expected_order_id,
                               message=f"提交接口已返回订单号 {self.expected_order_id}，但尚未核实商品、金额和待付款状态；请核对该订单，不会重复提交")
        message = "未能确认具体新增订单，请先检查京东订单列表；不会自动重复提交"
        if probe is not None and probe.signal.kind == 'rejected':
            message = "京东下单接口返回未成功状态，订单列表未找到匹配新订单；请检查结算页，不会重复提交"
        return OrderResult(outcome="unknown", message=message)

    async def screenshot(self, task_id: str) -> str | None:
        if self.page is None or self.page.is_closed():
            return None
        folder = self.data / "screenshots"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{task_id}.png"
        try:
            # Mask likely address/recipient blocks in saved evidence.
            masks = self.page.locator(SELECTORS["address"] + ', [class^="consignee-item-"], [class^="scantoPayMain-"], #orderDetail')
            await self.page.screenshot(path=str(path), full_page=False, mask=[masks])
            return path.name
        except Exception:
            return None
