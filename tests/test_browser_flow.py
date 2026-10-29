import json
import asyncio
import threading
import time
import random
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.async_api import async_playwright

from assistant.adapters.jd import JDAdapter, JDUrls, parse_product
from assistant.engine import Engine
from assistant.models import CHINA, Status, NeedsHuman, NotReady


@pytest.fixture
def mock_shop(config):
    state = {"total":269900, "unlockAt":int(time.time()*1000)-1, "submits":0, "orders":[], "selection":None, "inCart":True,"trace":[]}
    body = (Path(__file__).parent / "fixtures" / "mock_shop.html").read_text(encoding="utf-8")
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_GET(self):
            path = urlparse(self.path)
            if path.path in ('/reserve','/add-cart','/checkout-check','/submit','/payment-enter','/payment-exit'):
                state['trace'].append({'step':path.path,'at':datetime.now(CHINA).isoformat()})
            if path.path == "/submit":
                state["submits"] += 1
                state["orders"].append({"id": str(123456789012+state["submits"]), "time":datetime.now(CHINA).strftime("%Y-%m-%d %H:%M:%S")})
                raw, content = b"ok", "text/plain"
            elif path.path == "/reserve":
                state["reservedCalls"] = state.get("reservedCalls",0)+1
                raw, content = b"ok", "text/plain"
            elif path.path == '/add-cart':
                state['addCalls']=state.get('addCalls',0)+1
                state['inCart']=True
                raw,content=b'ok','text/plain'
            elif path.path == '/cart-quantity':
                state['cartQuantity']=int(parse_qs(path.query)['value'][0])
                raw,content=b'ok','text/plain'
            elif path.path in ('/payment-enter', '/payment-exit', '/cancel-order'):
                state[path.path] = state.get(path.path, 0) + 1
                raw, content = b"ok", "text/plain"
            elif path.path == "/orders":
                state['orderReads'] = state.get('orderReads', 0)+1
                raw, content = json.dumps([] if state.get("hideOrders") else state["orders"]).encode(), "application/json"
            elif path.path == "/checkout-check":
                state["selection"] = parse_qs(path.query)["selected"][0]
                raw, content = b"ok", "text/plain"
            else:
                if parse_qs(path.query).get("stage") == ["cart"]:
                    state['trace'].append({'step':'cart','at':datetime.now(CHINA).isoformat()})
                    state["cartVisits"] = state.get("cartVisits", 0) + 1
                    if state.get("lockForMs") and state["cartVisits"] == 1:
                        state["unlockAt"] = int(time.time() * 1000) + state["lockForMs"]
                item = {"item":{"skuId": config.product.sku, "skuName":config.product.title,
                                "newColorSize":[{"skuId":config.product.sku, "size":"雷鸟GT Max bilibili 限定版", "spec":"M码"}]}}
                reservation = {}
                if state.get("needReservation"):
                    reservation = {"yueStime":(datetime.now(CHINA)-timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),
                                   "yueEtime":(datetime.now(CHINA)+timedelta(minutes=-1 if state.get("reservationExpired") else 30)).strftime("%Y-%m-%d %H:%M:%S")}
                raw = body.replace("__PRODUCT__", json.dumps(item, ensure_ascii=False)).replace("__RESERVATION__", json.dumps(reservation)).replace("__CONFIG__", json.dumps(state, ensure_ascii=False)).encode()
                content = "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type",content)
            self.send_header("Content-Length",str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    for _ in range(100):
        try:
            server = ThreadingHTTPServer(("127.0.0.1",random.randrange(20000,60000)),Handler)
            break
        except OSError:
            continue
    else:
        raise RuntimeError('No safe local mock shop port available')
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    yield state, f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


@pytest.fixture
async def browser_harness(request, tmp_path):
    async with async_playwright() as playwright:
        native = None
        chromium = None
        if getattr(request, 'param', 'persistent') == 'native_cdp':
            from assistant.native_browser import NativeBrowser
            from assistant.browser_runtime import bundled_executable
            native = NativeBrowser()
            context = await native.start(playwright.chromium, bundled_executable(Path(__file__).resolve().parent.parent/'data'/'browsers'), tmp_path/'native-profile', direct=True)
        else:
            chromium = await playwright.chromium.launch(headless=True)
            context = await chromium.new_context()
        # All JD hosts used by fixtures stay offline. Individual tests can
        # register newer routes for intentional delay/error scenarios.
        await context.route('https://item.jd.com/**', lambda route:route.fulfill(body='<p>Offline SKU detail</p>'))
        await context.route('https://item.m.jd.com/**', lambda route:route.fulfill(body='<p>Offline SKU detail</p>'))
        await context.route('https://api.m.jd.com/**', lambda route:route.abort('blockedbyclient'))
        class Harness:
            async def ensure(self):
                return context
            async def task_page(self):
                return await context.new_page()
            async def status(self):
                return {"logged_in":True}
        yield Harness()
        if native is not None:
            await native.close()
        else:
            await context.close()
            await chromium.close()


async def run_shop(store, config, mock_shop, browser_harness, tmp_path):
    # Browser startup time is unrelated to the purchase window being tested.
    config = config.model_copy(update={"scheduled_at": datetime.now(CHINA) - timedelta(milliseconds=50), "window_seconds":30})
    state, origin = mock_shop
    urls = JDUrls(product=origin+"/?stage=product&sku={sku}", cart=origin+"/?stage=cart",orders=origin+"/?stage=orders")
    adapter = JDAdapter(browser_harness, tmp_path, urls)
    adapter.payment_wait_seconds = state.get('paymentWaitSeconds',15)
    engine = Engine(store, lambda:adapter)
    task = store.create(config)
    engine.start(task["id"])
    await engine.job
    return store.get(task["id"]), engine


@pytest.mark.parametrize("modern", [False, True])
async def test_real_browser_runs_exact_sku_only(store, config, mock_shop, browser_harness, tmp_path, modern):
    mock_shop[0]["modernOrders"] = modern
    task, _ = await run_shop(store,config,mock_shop,browser_harness,tmp_path)
    state, _ = mock_shop
    assert task["status"] == Status.SUCCEEDED, task["message"]
    assert task["result"]["order_id"]
    assert state["selection"] == config.product.sku
    assert state["submits"] == 1


async def test_existing_target_pending_order_blocks_new_submit(store, config, mock_shop, browser_harness, tmp_path):
    state, _ = mock_shop
    state["orders"] = [{"id":"123456789099", "time":datetime.now(CHINA).strftime("%Y-%m-%d %H:%M:%S")}]
    task, _ = await run_shop(store, config, mock_shop, browser_harness, tmp_path)
    assert task["status"] == Status.WAITING
    assert "已有" in task["message"]
    assert state["submits"] == 0


async def test_reserve_add_cart_waits_on_cart_without_checkout(store, config, mock_shop, browser_harness, tmp_path):
    state,origin=mock_shop
    state.update(needReservation=True,inCart=False)
    cfg=config.model_copy(update={'scheduled_at':datetime.now(CHINA)+timedelta(seconds=40),'window_seconds':10})
    adapter=JDAdapter(browser_harness,tmp_path,JDUrls(product=origin+'/?stage=product&sku={sku}',cart=origin+'/?stage=cart',orders=origin+'/?stage=orders'))
    engine=Engine(store,lambda:adapter);task=store.create(cfg);engine.start(task['id'])
    async def wait_ready():
        while store.get(task['id'])['status'] not in {Status.SCHEDULED,Status.WAITING}:
            await asyncio.sleep(.02)
    try:
        await asyncio.wait_for(wait_ready(),timeout=15)
        assert store.get(task['id'])['status']==Status.SCHEDULED,store.get(task['id'])['message']
        assert adapter.page.url==origin+'/?stage=cart'
        assert state['reservedCalls']==1 and state['addCalls']==1
        assert state['selection'] is None and state['submits']==0
        assert not any(x['step']=='/checkout-check' for x in state['trace'])
        assert (await adapter.page.get_by_label('商品数量').input_value())=='1'
    finally:
        await engine.stop(task['id'])


async def test_readonly_probe_reports_absent_cart_item_without_adding(config, mock_shop, browser_harness, tmp_path):
    state,origin=mock_shop;state['inCart']=False
    adapter=JDAdapter(browser_harness,tmp_path,JDUrls(product=origin+'/?stage=product&sku={sku}',cart=origin+'/?stage=cart',orders=origin+'/?stage=orders'))
    result=await adapter.probe(config.product)
    assert result['cart_found'] is False and result['checkbox_found'] is False
    assert result['order_list_readable'] is True
    assert state.get('addCalls',0)==0 and state['submits']==0


@pytest.mark.parametrize('browser_harness', ['persistent','native_cdp'], indirect=True)
async def test_full_chain_adds_exact_product_and_reports_order_after_payment(store, config, mock_shop, browser_harness, tmp_path):
    state,_=mock_shop;state.update(needReservation=True,inCart=False,modernCheckout=True,paymentDelayMs=250)
    context=await browser_harness.ensure()
    await context.route('https://item.jd.com/**',lambda r:r.fulfill(body='<p>Fixture product</p>'))
    task,_=await run_shop(store,config,mock_shop,browser_harness,tmp_path)
    assert task['status']==Status.SUCCEEDED,task['message']
    steps=[x['step'] for x in state['trace']]
    positions=[steps.index(step) for step in ['/reserve','/add-cart','/checkout-check','/submit','/payment-enter','/payment-exit']]
    assert positions==sorted(positions)
    assert state['addCalls']==1 and state['submits']==1
    assert task['payment_entered_at'] and task['result']['payment_entered']
    assert task['result']['order_id'] in task['message'] and '抢购成功' in task['message']
    assert state.get('/cancel-order',0)==0


async def test_existing_cart_not_added_twice_and_quantity_is_normalized(store, config, mock_shop, browser_harness, tmp_path):
    state,_=mock_shop;state['cartQuantity']=2
    task,_=await run_shop(store,config,mock_shop,browser_harness,tmp_path)
    assert task['status']==Status.SUCCEEDED,task['message']
    assert state.get('addCalls',0)==0 and state['cartQuantity']==1


@pytest.mark.parametrize('delayed', [0, 250])
async def test_jd_split_columns_find_quantity_in_complete_sku_row(config, mock_shop, browser_harness, tmp_path, delayed):
    state,origin=mock_shop;state.update(splitCart=True,cartQuantity=2,quantityDelayMs=delayed)
    adapter=JDAdapter(browser_harness,tmp_path,JDUrls(cart=origin+'/?stage=cart'))
    adapter.page=await browser_harness.task_page()
    await adapter.prepare_cart(config)
    assert (await adapter.page.locator('[data-rowgid="fixture-target"] ._cart-number_test input').input_value())=='1'
    assert await adapter.page.get_by_label('商品备注').input_value()=='不要修改'
    assert state['submits']==0 and state['selection'] is None


async def test_locked_split_cart_quantity_can_be_verified_before_sale(config, mock_shop, browser_harness, tmp_path):
    state,origin=mock_shop;state.update(splitCart=True,lockedQuantity=True,unlockAt=int(time.time()*1000)+60000)
    adapter=JDAdapter(browser_harness,tmp_path,JDUrls(cart=origin+'/?stage=cart'));adapter.page=await browser_harness.task_page()
    await adapter.prepare_cart(config)
    assert adapter.cart_ready
    assert not await adapter.page.get_by_label('目标商品').is_enabled()
    assert state['submits']==0


async def test_two_quantity_fields_in_one_sku_row_pause_instead_of_guessing(config, mock_shop, browser_harness, tmp_path):
    state,origin=mock_shop;state.update(splitCart=True,duplicateQuantity=True)
    adapter=JDAdapter(browser_harness,tmp_path,JDUrls(cart=origin+'/?stage=cart'));adapter.page=await browser_harness.task_page()
    with pytest.raises(NeedsHuman,match='无法唯一确认'):
        await adapter.prepare_cart(config)
    assert state['submits']==0 and state['selection'] is None


async def test_created_order_without_payment_entry_is_not_final_success(store, config, mock_shop, browser_harness, tmp_path):
    state,_=mock_shop;state.update(lostResponse=True,paymentWaitSeconds=.35)
    task,engine=await run_shop(store,config,mock_shop,browser_harness,tmp_path)
    assert task['status']==Status.WAITING
    assert task['result']['outcome']=='created' and task['result']['order_id']
    assert task['result']['payment_entered'] is False and not task['payment_entered_at']
    assert '抢购成功' not in task['message']
    engine.start(task['id'],resume=True);await engine.job
    assert state['submits']==1 and store.get(task['id'])['status']==Status.WAITING


async def test_cashier_order_id_must_match_created_order(store, config, mock_shop, browser_harness, tmp_path):
    state,_=mock_shop;state.update(modernCheckout=True,paymentOrderId='999999999999')
    context=await browser_harness.ensure()
    await context.route('https://item.jd.com/**',lambda r:r.fulfill(body='<p>Fixture product</p>'))
    task,_=await run_shop(store,config,mock_shop,browser_harness,tmp_path)
    assert task['status']==Status.WAITING
    assert task['result']['outcome']=='unknown' and '不一致' in task['message']
    assert state['submits']==1


async def test_cashier_rendered_after_body_snapshot_still_provides_order_id(config, mock_shop, browser_harness, tmp_path, monkeypatch):
    import assistant.adapters.jd as jd
    state, origin = mock_shop
    adapter = JDAdapter(browser_harness, tmp_path)
    adapter.page = await browser_harness.task_page()
    await adapter.page.goto(origin+'/?stage=checkout')
    original_check = jd.check_page_access

    async def show_after_snapshot(browser, page):
        text = await original_check(browser, page)
        await page.locator('body').evaluate("""n => n.insertAdjacentHTML('beforeend',
            '<div class="scantoPay-test"><div class="scantoPayHead-test">微信支付 订单号：999999999999</div><p>微信APP扫码支付</p></div>')""")
        return text

    monkeypatch.setattr(jd, 'check_page_access', show_after_snapshot)
    assert await adapter.enter_payment(config) == '999999999999'
    assert adapter.payment_entered and state['submits'] == 0


async def test_async_cart_selection_ignores_store_checkbox(store, config, mock_shop, browser_harness, tmp_path):
    mock_shop[0]["asyncCart"] = True
    task, _ = await run_shop(store, config, mock_shop, browser_harness, tmp_path)
    assert task["status"] == Status.SUCCEEDED, task["message"]
    assert mock_shop[0]["selection"] == config.product.sku
    assert mock_shop[0]["submits"] == 1


@pytest.mark.parametrize('problem', [None, 'wrongSku', 'quantity', 'wrongAddress', 'unknownPrice'])
@pytest.mark.parametrize('browser_harness', ['persistent','native_cdp'], indirect=True)
async def test_modern_checkout_follows_exact_product_and_manual_payment(store, config, mock_shop, browser_harness, tmp_path, problem):
    state, _ = mock_shop
    state['modernCheckout'] = True
    if problem:
        state[problem] = 2 if problem == 'quantity' else True
    context = await browser_harness.ensure()
    await context.route('https://item.jd.com/**', lambda route: route.fulfill(body='<p>Local product detail fixture</p>'))
    task, _ = await run_shop(store, config, mock_shop, browser_harness, tmp_path)
    assert task['status'] == (Status.WAITING if problem else Status.SUCCEEDED), task['message']
    assert state['submits'] == (0 if problem else 1)
    assert state.get('/cancel-order', 0) == 0
    if not problem:
        assert task['result']['payment_flow'] == 'entered_and_exited'
        assert state['/payment-exit'] == 1


async def test_order_number_success_survives_missing_payment_close(store, config, mock_shop, browser_harness, tmp_path):
    state, _ = mock_shop
    state.update(modernCheckout=True, missingPaymentClose=True)
    context = await browser_harness.ensure()
    await context.route('https://item.jd.com/**', lambda route: route.fulfill(body='<p>Local product detail fixture</p>'))
    task, _ = await run_shop(store, config, mock_shop, browser_harness, tmp_path)
    assert task['status'] == Status.SUCCEEDED
    assert task['result']['order_id']
    assert task['result']['payment_flow'] == 'not_confirmed'
    assert state['submits'] == 1
    assert state.get('/cancel-order', 0) == 0


async def test_fullpage_wechat_exit_returns_to_orders_without_cancelling(mock_shop, browser_harness, tmp_path):
    state, origin = mock_shop
    context = await browser_harness.ensure()
    await context.route('https://pcashier.jd.com/**', lambda route: route.fulfill(body='<h1>微信支付</h1><p>请使用微信扫一扫 扫描二维码支付</p>', content_type='text/html; charset=utf-8'))
    adapter = JDAdapter(browser_harness, tmp_path, JDUrls(orders=origin+'/?stage=orders'))
    adapter.page = await context.new_page()
    await adapter.page.goto('https://pcashier.jd.com/weixin/test')
    await adapter.exit_payment_dialog()
    assert adapter.page.url == origin+'/?stage=orders'
    assert adapter.payment_flow == 'entered_and_exited'
    assert state.get('/cancel-order', 0) == 0
    assert state['submits'] == 0


async def test_automatic_reservation_before_purchase(store, config, mock_shop, browser_harness, tmp_path):
    state, _ = mock_shop
    state["needReservation"] = True
    task, _ = await run_shop(store,config,mock_shop,browser_harness,tmp_path)
    assert task["status"] == Status.SUCCEEDED, task["message"]
    assert state["reservedCalls"] == 1 and state["submits"] == 1


async def test_modern_already_set_reminder_does_not_require_reserve_button(store, config, mock_shop, browser_harness, tmp_path):
    state,_=mock_shop
    state.update(needReservation=True,modernReminder=True)
    task,_=await run_shop(store,config,mock_shop,browser_harness,tmp_path)
    assert task["status"] == Status.SUCCEEDED, task["message"]
    assert state.get("reservedCalls",0) == 0
    messages=[event["message"] for event in store.events(task["id"])]
    assert any("开抢提醒已设置" in message for message in messages)
    assert not any(message.startswith("预约已确认") for message in messages)


async def test_expired_reservation_stops_before_buying(store, config, mock_shop, browser_harness, tmp_path):
    state, _ = mock_shop
    state.update(needReservation=True,reservationExpired=True)
    task, _ = await run_shop(store,config,mock_shop,browser_harness,tmp_path)
    assert task["status"] == Status.WAITING and "预约已截止" in task["message"]
    assert state.get("reservedCalls",0) == 0 and state["submits"] == 0


async def test_submit_rechecks_deadline_after_loading_checkout(config, mock_shop, browser_harness, tmp_path):
    state,origin = mock_shop
    adapter=JDAdapter(browser_harness,tmp_path)
    adapter.page=await browser_harness.task_page()
    await adapter.page.goto(origin+'/?stage=checkout')
    config=config.model_copy(update={"scheduled_at":datetime.now(CHINA)-timedelta(minutes=1)})
    with pytest.raises(NeedsHuman,match="窗口已结束"):
        await adapter.submit(config)
    assert state["submits"] == 0


@pytest.mark.parametrize("problem", ["wrongSku", "wrongAddress", "unknownPrice", "overBudget", "quantity", "captcha"])
async def test_real_browser_prevents_wrong_orders(store, config, mock_shop, browser_harness, tmp_path, problem):
    state, _ = mock_shop
    state[problem] = 2 if problem == "quantity" else True
    if problem == "overBudget":
        state["total"] = config.max_total_cents + 1
    task, _ = await run_shop(store,config,mock_shop,browser_harness,tmp_path)
    assert task["status"] == Status.WAITING, task["message"]
    assert state["submits"] == 0


async def test_delayed_cart_unlock_retries(store, config, mock_shop, browser_harness, tmp_path):
    state, _ = mock_shop
    state["lockForMs"] = 800
    task, _ = await run_shop(store,config,mock_shop,browser_harness,tmp_path)
    assert task["status"] == Status.SUCCEEDED, task["message"]
    assert state["submits"] == 1
    assert state["cartVisits"] >= 2


@pytest.mark.parametrize('browser_harness', ['persistent','native_cdp'], indirect=True)
async def test_uncertain_result_does_not_submit_again_on_resume(store, config, mock_shop, browser_harness, tmp_path):
    state, _ = mock_shop
    state["hideOrders"] = True
    state["lostResponse"] = True
    state["modernCheckout"] = True
    task, engine = await run_shop(store,config,mock_shop,browser_harness,tmp_path)
    assert task["status"] == Status.WAITING
    assert state["submits"] == 1
    state["hideOrders"] = False
    engine.start(task["id"],resume=True)
    await engine.job
    assert store.get(task["id"])["status"] == Status.SUCCEEDED
    assert state["submits"] == 1


def test_parse_reservation_timing_and_locked_variant():
    html = '''<script>window._itemOnly = ({"item":{"skuId":"100327520619","skuName":"雷鸟限定版","newColorSize":[{"skuId":"100327520619","size":"限定版","spec":"M码"}]}});</script><script>window.data={"yuyue":{"yueStime":"2026-10-24 11:15:00","yueEtime":"2026-10-25 09:55:00","qiangStime":"2026-10-25 10:00:00","qiangEtime":"2026-10-25 11:00:00"}};</script>'''
    product = parse_product(html,"100327520619")
    assert product.spec == "限定版 / M码"
    assert product.reservation_required
    assert product.sale_start == "2026-10-25T10:00:00+08:00"


@pytest.mark.parametrize("label", ["去结算 (1)", "领券结算（1）", "领券结算 ( 1 )"])
async def test_visible_single_item_settle_entry(config, browser_harness, tmp_path, label):
    adapter = JDAdapter(browser_harness, tmp_path)
    adapter.page = await browser_harness.task_page()
    await adapter.page.set_content(f'<button style="display:none">{label}</button><button>{label}</button>')
    config = config.model_copy(update={"scheduled_at": datetime.now(CHINA)-timedelta(seconds=1)})
    entry = await adapter._cart_settle_button(config)
    assert await entry.inner_text() == label


async def test_checkout_sku_check_does_not_wait_for_detail_scripts(config, mock_shop, browser_harness, tmp_path):
    state, origin = mock_shop
    state['modernCheckout'] = True
    context = await browser_harness.ensure()
    release_script = asyncio.Event()
    release_timer = asyncio.get_running_loop().call_later(5, release_script.set)

    async def serve_detail(route):
        if route.request.url.endswith('/slow.js'):
            await release_script.wait()
            try:
                await route.fulfill(body='/* released after identity check */', content_type='text/javascript')
            except Exception:
                pass  # The verified detail tab may already be closed.
        else:
            await route.fulfill(body='<script src="https://item.jd.com/slow.js"></script><p>Detail</p>', content_type='text/html')

    await context.route('https://item.jd.com/**', serve_detail)
    adapter = JDAdapter(browser_harness, tmp_path)
    adapter.page = await browser_harness.task_page()
    await adapter.page.goto(origin+'/?stage=checkout')
    await adapter.ensure_manual_payment()
    try:
        # DOMContentLoaded cannot fire until release_script is set. Both the
        # initial check and the immediate recheck must finish without it.
        first = await asyncio.wait_for(adapter.read_checkout(config), timeout=3)
        second = await asyncio.wait_for(adapter.read_checkout(config), timeout=3)
        assert first.sku == second.sku == config.product.sku
        assert state['submits'] == 0
    finally:
        release_script.set()
        release_timer.cancel()


async def test_settle_waits_for_coupon_count_update(config, browser_harness, tmp_path):
    adapter = JDAdapter(browser_harness, tmp_path)
    adapter.page = await browser_harness.task_page()
    await adapter.page.set_content('<button>领券结算（0）</button><script>setTimeout(()=>document.querySelector("button").textContent="领券结算（1）",200)</script>')
    config = config.model_copy(update={"scheduled_at": datetime.now(CHINA)-timedelta(seconds=1)})
    entry = await adapter._cart_settle_button(config)
    assert await entry.inner_text() == "领券结算（1）"


async def test_ambiguous_visible_settle_entries_pause(config, browser_harness, tmp_path):
    adapter = JDAdapter(browser_harness, tmp_path)
    adapter.page = await browser_harness.task_page()
    await adapter.page.set_content('<button>去结算 (1)</button><button>领券结算（1）</button>')
    config = config.model_copy(update={"scheduled_at": datetime.now(CHINA)-timedelta(seconds=1)})
    with pytest.raises(NeedsHuman, match="多个可见"):
        await adapter._cart_settle_button(config)


async def test_current_page_anomaly_pauses_and_keeps_independent_session(browser_harness, tmp_path):
    from assistant.browser import Browser
    from assistant.protection import AccessLimited
    browser = Browser(tmp_path)
    adapter = JDAdapter(browser,tmp_path)
    adapter.page = await browser_harness.task_page()
    await adapter.page.set_content('<h1>当前页面异常</h1><p>请刷新或切换账号试试</p>')
    with pytest.raises(AccessLimited): await adapter._guard()
    assert browser.protection.state()['paused']
    assert not adapter.page.is_closed()


async def test_locked_cart_poll_does_not_reload_every_poll(config, mock_shop, browser_harness, tmp_path):
    state, origin = mock_shop
    state['unlockAt'] = int(time.time()*1000)+60000
    adapter = JDAdapter(browser_harness,tmp_path,JDUrls(cart=origin+'/?stage=cart'))
    adapter.page = await browser_harness.task_page()
    await adapter.prepare_cart(config)
    with pytest.raises(NotReady): await adapter.checkout(config)
    visits = state['cartVisits']
    with pytest.raises(NotReady): await adapter.checkout(config)
    assert state['cartVisits'] == visits
    adapter.last_cart_refresh -= 6
    with pytest.raises(NotReady): await adapter.checkout(config)
    assert state['cartVisits'] == visits+1


async def test_order_verification_stops_after_access_restriction(config, browser_harness, tmp_path):
    from assistant.protection import AccessLimited
    adapter = JDAdapter(browser_harness,tmp_path)
    calls = []
    async def denied(*args, **kwargs):
        calls.append(1)
        raise AccessLimited('自动访问已暂停')
    adapter._orders = denied
    result = await adapter.verify(config,[])
    assert result.outcome == 'unknown' and '暂停' in result.message
    assert len(calls) == 1


@pytest.mark.parametrize('browser_harness', ['persistent', 'native_cdp'], indirect=True)
@pytest.mark.parametrize('api_function', ['pc_trade_submitOrder', 'balance_submitOrder_pc'])
async def test_submit_api_candidate_is_durable_and_matches_order_without_extra_submit(store, config, mock_shop, browser_harness, tmp_path, api_function):
    state, _ = mock_shop
    state.update(submitApi=api_function, modernCheckout=True, paymentDelayMs=250)
    context = await browser_harness.ensure()
    calls = []
    async def submit(route):
        task = store.tasks()[0]
        assert task['submit_intent'] and task['submitted_at']
        calls.append(1)
        state['submits'] += 1
        state['orders'].append({'id':'123456789013','time':datetime.now(CHINA).strftime('%Y-%m-%d %H:%M:%S')})
        payload = {'success':True,'orderId':'123456789013'} if api_function == 'pc_trade_submitOrder' else {
            'code':'0', 'body':{'resultFlag':True,'order':{'orderIdStr':'123456789013'}}}
        await route.fulfill(status=200, headers={'Content-Type':'application/json','Access-Control-Allow-Origin':'*'},
                            body=json.dumps(payload))
    await context.route('https://api.m.jd.com/api?*', submit)
    task, engine = await run_shop(store, config, mock_shop, browser_harness, tmp_path)
    assert task['status'] == Status.SUCCEEDED, task['message']
    assert task['submission_order_id'] == task['result']['order_id'] == '123456789013'
    assert len(calls) == state['submits'] == 1
    assert any('提交 API 响应' in e['message'] for e in store.events(task['id']))
    assert not any('h5st' in e['message'] for e in store.events(task['id']))


async def test_submit_api_known_order_survives_missing_cashier_and_order_list(store, config, mock_shop, browser_harness, tmp_path):
    state, _ = mock_shop
    state.update(submitApi=True, lostResponse=True, hideOrders=True, paymentWaitSeconds=.2)
    context = await browser_harness.ensure()
    async def submit(route):
        state['submits'] += 1
        await route.fulfill(status=200, headers={'Content-Type':'application/json','Access-Control-Allow-Origin':'*'},
                            body=json.dumps({'success':True,'orderId':'123456789013'}))
    await context.route('https://api.m.jd.com/api?*', submit)
    task, engine = await run_shop(store, config, mock_shop, browser_harness, tmp_path)
    assert task['status'] == Status.WAITING
    assert task['submission_order_id'] == task['result']['order_id'] == '123456789013'
    assert task['result']['outcome'] == 'unknown'
    assert not task['payment_entered_at'] and '尚未核实' in task['message']
    with pytest.raises(ValueError, match='不能重置'):
        await engine.reset(task['id'])
    engine.start(task['id'], resume=True)
    await engine.job
    assert state['submits'] == 1
    assert store.get(task['id'])['result']['order_id'] == '123456789013'


async def test_submit_api_rejection_skips_cashier_timeout_but_never_retries(store, config, mock_shop, browser_harness, tmp_path):
    state, _ = mock_shop
    state.update(submitApi=True)
    context = await browser_harness.ensure()
    async def submit(route):
        state['submits'] += 1
        await route.fulfill(status=200, headers={'Content-Type':'application/json','Access-Control-Allow-Origin':'*'},
                            body=json.dumps({'success':False,'message':'商品无货'}))
    await context.route('https://api.m.jd.com/api?*', submit)
    task, _ = await run_shop(store, config, mock_shop, browser_harness, tmp_path)
    assert task['status'] == Status.WAITING and state['submits'] == 1
    assert '接口返回未成功' in task['message']
    events = store.events(task['id'])
    payment = next(e for e in events if '提交动作结束' in e['message'])
    verifying = next(e for e in events if '正在查看订单号' in e['message'])
    assert (datetime.fromisoformat(verifying['time'])-datetime.fromisoformat(payment['time'])).total_seconds() < 3


async def test_submit_api_restriction_does_not_query_orders_again(store, config, mock_shop, browser_harness, tmp_path):
    state, _ = mock_shop
    state.update(submitApi=True)
    context = await browser_harness.ensure()
    async def submit(route):
        state['submits'] += 1
        await route.fulfill(status=429, headers={'Content-Type':'application/json','Access-Control-Allow-Origin':'*'},body='{}')
    await context.route('https://api.m.jd.com/api?*', submit)
    task, _ = await run_shop(store, config, mock_shop, browser_harness, tmp_path)
    assert task['status'] == Status.WAITING and '暂停' in task['message']
    assert state['submits'] == 1
    assert not task['result']['order_id']


async def test_submit_api_candidate_cannot_match_a_different_new_order(config, browser_harness, tmp_path):
    adapter = JDAdapter(browser_harness, tmp_path)
    adapter.expected_order_id = '123456789013'
    async def orders(*args, **kwargs):
        return [{'id':'123456789099','skus':{config.product.sku},'quantity':1,'stamp':datetime.now(CHINA),
                 'total':100,'pending':True}]
    adapter._orders = orders
    result = await adapter.verify(config, [])
    assert result.outcome == 'unknown' and result.order_id == '123456789013'


@pytest.mark.parametrize('browser_harness', ['persistent', 'native_cdp'], indirect=True)
async def test_direct_api_trial_runs_native_fetch_once_in_real_browser(config, mock_shop, browser_harness, tmp_path):
    from assistant.adapters.jd_api_direct import DirectSubmitTrial, PreparedSubmit
    state, origin = mock_shop
    context = await browser_harness.ensure()
    calls = []
    intent = []
    async def submit(route):
        if route.request.method == 'OPTIONS':
            await route.fulfill(status=204, headers={'Access-Control-Allow-Origin':origin,
                'Access-Control-Allow-Credentials':'true', 'Access-Control-Allow-Methods':'POST',
                'Access-Control-Allow-Headers':'content-type'})
            return
        assert intent == ['persisted']
        calls.append(route.request)
        await route.fulfill(status=200, headers={'Content-Type':'application/json',
            'Access-Control-Allow-Origin':origin, 'Access-Control-Allow-Credentials':'true'},
            body=json.dumps({'success':True,'orderId':'123456789013'}))
    await context.route('https://api.m.jd.com/api?*', submit)
    adapter = JDAdapter(browser_harness, tmp_path)
    adapter.page = await browser_harness.task_page()
    await adapter.page.goto(origin+'/?stage=checkout')
    await adapter.page.get_by_test_id('submit-order').wait_for()
    checkout = await adapter.read_checkout(config)
    config = config.model_copy(update={'scheduled_at':datetime.now(CHINA)-timedelta(milliseconds=50), 'window_seconds':30})
    prepared = PreparedSubmit('https://api.m.jd.com/api?functionId=balance_submitOrder_pc',
                              'fixture-only=1', adapter.page.url, checkout)
    client = DirectSubmitTrial(browser_harness, adapter.page, lambda:intent.append('persisted'))
    signal = await client.submit(config, prepared, adapter.read_checkout)
    assert signal.kind == 'candidate' and signal.order_id == '123456789013'
    assert len(calls) == 1 and calls[0].post_data == 'fixture-only=1'
    assert state['submits'] == 0  # The DOM button was never clicked.
    with pytest.raises(NeedsHuman):
        await client.submit(config, prepared, adapter.read_checkout)
    assert len(calls) == 1


async def api_task_backend(context, state, store, *, status=200, drop=False):
    requests = []
    async def handle(route):
        if route.request.method == 'OPTIONS':
            origin = route.request.headers.get('origin', '')
            return await route.fulfill(status=204, headers={'Access-Control-Allow-Origin':origin,
                'Access-Control-Allow-Credentials':'true','Access-Control-Allow-Methods':'POST',
                'Access-Control-Allow-Headers':'content-type'})
        assert store.tasks()[0]['submit_intent']
        requests.append(route.request)
        if status == 200:
            state['submits'] += 1
            state['orders'].append({'id':'123456789013','time':datetime.now(CHINA).strftime('%Y-%m-%d %H:%M:%S')})
        if drop:
            return await route.abort('connectionreset')
        origin = route.request.headers.get('origin', '')
        await route.fulfill(status=status, headers={'Content-Type':'application/json',
            'Access-Control-Allow-Origin':origin, 'Access-Control-Allow-Credentials':'true'},
            body=json.dumps({'code':'0','body':{'resultFlag':True,'order':{'orderIdStr':'123456789013'}}}
                            if status == 200 else {'code':'error'}))
    await context.route('https://api.m.jd.com/api?*', handle)
    return requests


@pytest.mark.parametrize('browser_harness', ['persistent','native_cdp'], indirect=True)
async def test_opt_in_api_task_uses_fresh_sdk_request_and_only_one_actual_post(store, config, mock_shop, browser_harness, tmp_path):
    state, _ = mock_shop
    state.update(modernCheckout=True, submitApi='balance_submitOrder_pc', sdkXHR=True)
    calls = await api_task_backend(await browser_harness.ensure(), state, store)
    config = config.model_copy(update={'submit_mode':'api'})
    task, _ = await run_shop(store, config, mock_shop, browser_harness, tmp_path)
    assert task['status'] == Status.SUCCEEDED, task['message']
    assert len(calls) == state['submits'] == 1
    assert task['submission_order_id'] == '123456789013'
    assert task['result']['payment_entered']
    assert any('实际派发 1 次' in e['message'] for e in store.events(task['id']))


async def test_opt_in_api_disconnect_keeps_guard_and_resume_never_dispatches_again(store, config, mock_shop, browser_harness, tmp_path):
    from assistant.browser import Browser
    state, _ = mock_shop
    state.update(modernCheckout=True, submitApi='balance_submitOrder_pc', sdkXHR=True, paymentWaitSeconds=.2)
    context = await browser_harness.ensure()
    calls = await api_task_backend(context, state, store, drop=True)
    config = config.model_copy(update={'submit_mode':'api'})
    task, engine = await run_shop(store, config, mock_shop, browser_harness, tmp_path)
    assert len(calls) == state['submits'] == 1
    assert task['status'] == Status.WAITING
    # The page SDK (or a repeated click) cannot send another order after error.
    guarded = next(p for p in context.pages if getattr(p, '_api_submit_guarded', False))
    await guarded.evaluate("fetch('https://api.m.jd.com/api?functionId=balance_submitOrder_pc',{method:'POST',body:'fixture-only'}).catch(()=>{})")
    assert len(calls) == 1
    engine.start(task['id'], resume=True)
    await engine.job
    assert len(calls) == 1
    # Future tasks use a fresh page, rather than disabling the old retry guard.
    browser = Browser(tmp_path)
    browser.context, browser.page = context, guarded
    assert await browser.task_page() is not guarded


async def test_opt_in_api_price_change_after_sdk_preparation_sends_no_request(store, config, mock_shop, browser_harness, tmp_path):
    state, origin = mock_shop
    state.update(modernCheckout=True, submitApi='balance_submitOrder_pc', sdkXHR=True)
    context = await browser_harness.ensure()
    calls = await api_task_backend(context, state, store)
    class Changed(JDAdapter):
        async def recheck_checkout(self, cfg, **kwargs):
            if self.api_bridge is not None and self.api_bridge.held is not None:
                await self.page.locator('.price').evaluate("n => n.textContent = '999999.00'")
            return await super().recheck_checkout(cfg, **kwargs)
    adapter = Changed(browser_harness,tmp_path,JDUrls(product=origin+'/?stage=product&sku={sku}',cart=origin+'/?stage=cart',orders=origin+'/?stage=orders'))
    engine = Engine(store,lambda:adapter)
    config = config.model_copy(update={'submit_mode':'api','scheduled_at':datetime.now(CHINA)-timedelta(milliseconds=1),'window_seconds':30})
    task = store.create(config)
    engine.start(task['id']); await engine.job
    assert not calls and not state['submits']
    assert store.get(task['id'])['status'] == Status.WAITING


async def test_opt_in_api_429_pauses_without_post_submit_order_queries(store, config, mock_shop, browser_harness, tmp_path):
    from assistant.protection import AccessProtection
    state, _ = mock_shop
    state.update(modernCheckout=True, submitApi='balance_submitOrder_pc', sdkXHR=True)
    browser_harness.protection = AccessProtection(tmp_path)
    calls = await api_task_backend(await browser_harness.ensure(), state, store, status=429)
    config = config.model_copy(update={'submit_mode':'api'})
    task, _ = await run_shop(store, config, mock_shop, browser_harness, tmp_path)
    assert len(calls) == 1 and state['submits'] == 0
    assert browser_harness.protection.state()['paused']
    assert state['orderReads'] == 1  # Only the pre-submit baseline was read.
    assert task['status'] == Status.WAITING


async def test_opt_in_api_stop_before_dispatch_blocks_the_wire(store, config, mock_shop, browser_harness, tmp_path):
    state, origin = mock_shop
    state.update(modernCheckout=True, submitApi='balance_submitOrder_pc', sdkXHR=True)
    calls = await api_task_backend(await browser_harness.ensure(), state, store)
    class Stopped(JDAdapter):
        async def recheck_checkout(self, cfg, **kwargs):
            if self.api_bridge is not None and self.api_bridge.held is not None:
                engine.stop_event.set()
            return await super().recheck_checkout(cfg, **kwargs)
    adapter = Stopped(browser_harness,tmp_path,JDUrls(product=origin+'/?stage=product&sku={sku}',cart=origin+'/?stage=cart',orders=origin+'/?stage=orders'))
    engine = Engine(store,lambda:adapter)
    config = config.model_copy(update={'submit_mode':'api','scheduled_at':datetime.now(CHINA)-timedelta(milliseconds=1),'window_seconds':30})
    task=store.create(config);engine.start(task['id']);await engine.job
    assert not calls and state['submits'] == 0
    assert store.get(task['id'])['submit_intent']


async def test_opt_in_api_unsupported_sdk_never_falls_back_to_network(store, config, mock_shop, browser_harness, tmp_path):
    state, _ = mock_shop
    state.update(modernCheckout=True, submitApi='balance_submitOrder_pc')  # fetch SDK not calibrated
    calls = await api_task_backend(await browser_harness.ensure(), state, store)
    config = config.model_copy(update={'submit_mode':'api'})
    task, _ = await run_shop(store, config, mock_shop, browser_harness, tmp_path)
    assert not calls and state['submits'] == 0
    assert task['status'] == Status.WAITING
