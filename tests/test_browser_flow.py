import json
import asyncio
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.async_api import async_playwright

from assistant.adapters.jd import JDAdapter, JDUrls, parse_product
from assistant.engine import Engine
from assistant.models import CHINA, Status, NeedsHuman


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
    server = ThreadingHTTPServer(("127.0.0.1",0),Handler)
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    yield state, f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


@pytest.fixture
async def browser_harness():
    async with async_playwright() as playwright:
        chromium = await playwright.chromium.launch(headless=True)
        context = await chromium.new_context()
        class Harness:
            async def ensure(self):
                return context
            async def task_page(self):
                return await context.new_page()
            async def status(self):
                return {"logged_in":True}
        yield Harness()
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


async def test_async_cart_selection_ignores_store_checkbox(store, config, mock_shop, browser_harness, tmp_path):
    mock_shop[0]["asyncCart"] = True
    task, _ = await run_shop(store, config, mock_shop, browser_harness, tmp_path)
    assert task["status"] == Status.SUCCEEDED, task["message"]
    assert mock_shop[0]["selection"] == config.product.sku
    assert mock_shop[0]["submits"] == 1


@pytest.mark.parametrize('problem', [None, 'wrongSku', 'quantity', 'wrongAddress', 'unknownPrice'])
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
