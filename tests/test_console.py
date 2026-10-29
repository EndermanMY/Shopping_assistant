"""Browser tests of our own console, using isolated data and a fake adapter.

No requests in these tests submit orders to JD.
"""
import asyncio
import json
import socket
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import uvicorn
from playwright.async_api import async_playwright, expect

from assistant.app import ROOT, create_app
from assistant.models import CHINA
from tests.test_core import FakeAdapter


@pytest.fixture
def console_server(tmp_path):
    app = create_app(tmp_path, adapter_factory=FakeAdapter)
    sock = socket.socket()
    sock.bind(("127.0.0.1",0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic()+8
    while not server.started and time.monotonic()<deadline:
        time.sleep(.02)
    assert server.started
    yield app, f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=8)


@pytest.mark.parametrize("width",[1440,375,320])
async def test_console_create_start_stop_and_responsive_layout(console_server, config, width):
    app, origin = console_server
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width":width,"height":900})
        errors = []
        page.on("pageerror",lambda error:errors.append(str(error)))
        await page.goto(origin)
        await page.get_by_label("控制台访问码").fill(app.state.access.code)
        await page.get_by_role("button",name="连接工作台").click()
        await expect(page.get_by_role("heading",name="抢购工作台.")).to_be_visible()
        await expect(page.locator(".connection")).to_contain_text("实时连接")
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        await page.get_by_role("button",name="创建抢购任务",exact=True).click()
        product = config.product.model_copy(update={"sale_start":(datetime.now(CHINA)+timedelta(minutes=10)).isoformat()})
        await page.route("**/api/products/resolve",lambda route:route.fulfill(body=product.model_dump_json(), content_type="application/json"))
        await page.get_by_label("商品链接 / 京东分享文案 / SKU").fill(config.product.sku)
        await page.get_by_role("button",name="解析",exact=True).click()
        await expect(page.locator(".resolved-product")).to_contain_text("M码")
        await page.get_by_label("最高应付总额").fill("2699.00")
        await page.get_by_label("收货地址核对关键字").fill("测试小区")
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        await page.get_by_role("button",name="保存任务",exact=True).click()
        await expect(page.locator(".task-row")).to_have_count(1)
        await expect(page.locator(".task-row")).to_contain_text("待启用")
        await page.get_by_role("button",name="启用",exact=True).click()
        await expect(page.locator(".status-badge")).to_contain_text("购物车等待")
        await page.get_by_role("button",name="停止",exact=True).click()
        await expect(page.locator(".status-badge")).to_contain_text("已停止")
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        if width in (1440,375):
            folder = ROOT / "data" / "qa"
            folder.mkdir(exist_ok=True,parents=True)
            await page.screenshot(path=str(folder/f"console-{width}-test-data.png"),full_page=True)
        assert not errors
        await browser.close()


async def test_mobile_connect_and_modal_keyboard_navigation(console_server):
    app,origin = console_server
    async with async_playwright() as playwright:
        browser=await playwright.chromium.launch()
        page=await browser.new_page(viewport={"width":375,"height":812})
        await page.goto(origin)
        await page.get_by_label("控制台访问码").fill(app.state.access.code)
        await page.get_by_role("button",name="连接工作台").click()
        await expect(page.get_by_role("button",name="打开导航")).to_be_visible()
        await page.get_by_role("button",name="打开导航").click()
        await page.get_by_role("button",name="手机连接",exact=True).click()
        await expect(page.get_by_role("dialog",name="连接手机控制台")).to_be_visible()
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        await page.keyboard.press("Escape")
        await expect(page.get_by_role("dialog")).to_have_count(0)
        await browser.close()


async def test_expired_task_offers_edit_without_enabling(console_server, config):
    app,origin=console_server
    expired_config=config.model_copy(update={"scheduled_at":datetime.now(CHINA)-timedelta(days=1)})
    saved=app.state.store.create(expired_config)
    async with async_playwright() as playwright:
        browser=await playwright.chromium.launch()
        page=await browser.new_page(viewport={"width":375,"height":812})
        await page.goto(origin)
        await page.get_by_label("控制台访问码").fill(app.state.access.code)
        await page.get_by_role("button",name="连接工作台").click()
        await expect(page.locator(".task-row")).to_contain_text("场次已过期")
        await page.get_by_role("button",name="修改场次",exact=True).click()
        await expect(page.get_by_label("最高应付总额")).to_have_value("2699.00")
        await expect(page.get_by_label("收货地址核对关键字")).to_have_value("测试小区")
        assert app.state.store.get(saved["id"])["status"] == "draft"
        assert not app.state.engine.busy
        await browser.close()


@pytest.mark.parametrize('width', [1440, 375])
async def test_custom_timing_and_delete_running_task_can_restore(console_server, config, width):
    app,origin=console_server
    async with async_playwright() as pw:
        browser=await pw.chromium.launch()
        page=await browser.new_page(viewport={'width':width,'height':900})
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        await page.goto(origin)
        await page.get_by_label('控制台访问码').fill(app.state.access.code)
        await page.get_by_role('button',name='连接工作台').click()
        await page.get_by_role('button',name='创建抢购任务',exact=True).click()
        product=config.product.model_copy(update={'sale_start':(datetime.now(CHINA)+timedelta(minutes=10)).isoformat()})
        await page.route('**/api/products/resolve',lambda r:r.fulfill(body=product.model_dump_json(),content_type='application/json'))
        await page.get_by_label('商品链接 / 京东分享文案 / SKU').fill(config.product.sku)
        await page.get_by_role('button',name='解析',exact=True).click()
        await expect(page.locator('.resolved-product')).to_be_visible()
        await page.get_by_label('最高应付总额').fill('2699')
        await page.get_by_label('收货地址核对关键字').fill('测试小区')
        await page.get_by_label('提前抢购时间（毫秒）').fill('350')
        await page.get_by_label('持续抢购时间（秒）').fill('913')
        await expect(page.locator('.timing-preview')).to_contain_text('.650')
        assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        await page.get_by_role('button',name='保存任务',exact=True).click()
        await expect(page.locator('.task-row')).to_have_count(1)
        saved=app.state.store.tasks()[0]
        assert saved['config']['advance_ms']==350 and saved['config']['window_seconds']==913
        await page.get_by_role('button',name='启用',exact=True).click()
        await expect(page.locator('.status-badge')).to_contain_text('购物车等待')
        await page.get_by_role('button',name=f'删除任务 {config.product.sku}',exact=True).click()
        await expect(page.locator('.task-row')).to_have_count(0)
        assert not app.state.engine.busy
        await page.get_by_role('button',name='已删除任务',exact=True).click()
        await expect(page.locator('.deleted-task')).to_have_count(1)
        await page.get_by_role('button',name='恢复到列表',exact=True).click()
        await page.keyboard.press('Escape')
        await expect(page.locator('.task-row')).to_have_count(1)
        await expect(page.locator('.status-badge')).to_contain_text('已停止')
        assert not app.state.engine.busy
        await page.get_by_role('button',name=f'查看任务 {config.product.sku}',exact=True).click()
        await page.get_by_role('button',name='修改配置',exact=True).click()
        await expect(page.get_by_label('提前抢购时间（毫秒）')).to_have_value('350')
        await expect(page.get_by_label('持续抢购时间（秒）')).to_have_value('913')
        await page.get_by_label('开抢时间').fill('2030-01-01T10:00:17')
        await page.get_by_role('button',name='解析',exact=True).click()
        await expect(page.get_by_label('开抢时间')).to_have_value('2030-01-01T10:00:17')
        await page.get_by_label('开抢时间').fill('2020-01-01T10:00:17')
        await page.get_by_role('button',name='保存任务',exact=True).click()
        await expect(page.get_by_role('alert')).to_contain_text('实际抢购开始时间已过去')
        assert app.state.store.get(saved['id'])['config']['scheduled_at'] == saved['config']['scheduled_at']
        assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        await page.screenshot(path=str(ROOT/'data/qa'/f'timing-delete-{width}.png'),full_page=True)
        assert not errors
        await browser.close()


async def test_successful_order_task_has_delete_button(console_server, config):
    app,origin=console_server
    task=app.state.store.create(config)
    app.state.store.update(task['id'],status='succeeded',submit_intent='known-order',result={'outcome':'created','order_id':'123456789012','total_cents':269900})
    async with async_playwright() as pw:
        browser=await pw.chromium.launch();page=await browser.new_page()
        await page.goto(origin)
        await page.get_by_label('控制台访问码').fill(app.state.access.code)
        await page.get_by_role('button',name='连接工作台').click()
        await page.get_by_role('button',name=f'删除任务 {config.product.sku}',exact=True).click()
        await expect(page.locator('.task-row')).to_have_count(0)
        assert app.state.store.tasks(deleted=True)[0]['result']['order_id']=='123456789012'
        await browser.close()


async def test_final_success_notification_contains_order_id(console_server, config):
    app,origin=console_server
    cfg=config.model_copy(update={'scheduled_at':datetime.now(CHINA)+timedelta(seconds=1),'window_seconds':60})
    task=app.state.store.create(cfg)
    async with async_playwright() as pw:
        browser=await pw.chromium.launch();page=await browser.new_page()
        await page.goto(origin)
        await page.get_by_label('控制台访问码').fill(app.state.access.code)
        await page.get_by_role('button',name='连接工作台').click()
        await expect(page.locator('.connection')).to_contain_text('实时连接')
        await page.get_by_role('button',name='启用',exact=True).click()
        await expect(page.locator('.status-badge')).to_contain_text('抢购成功')
        await expect(page.get_by_role('status')).to_contain_text('123456789012')
        assert app.state.store.get(task['id'])['result']['payment_entered']
        await browser.close()


async def test_order_created_without_cashier_does_not_display_success(console_server, config):
    app,origin=console_server
    task=app.state.store.create(config)
    app.state.store.update(task['id'],status='waiting_human',submit_intent='existing-order',result={'outcome':'created','order_id':'123456789012','total_cents':269900,'payment_entered':False})
    async with async_playwright() as pw:
        browser=await pw.chromium.launch();page=await browser.new_page()
        await page.goto(origin)
        await page.get_by_label('控制台访问码').fill(app.state.access.code)
        await page.get_by_role('button',name='连接工作台').click()
        await page.get_by_role('button',name=f'查看任务 {config.product.sku}',exact=True).click()
        await expect(page.locator('.order-result strong')).to_have_text('订单已创建，流程待确认')
        await expect(page.locator('.order-result')).to_contain_text('尚未确认进入付款界面')
        await browser.close()


@pytest.mark.parametrize('entry', ['details', 'list'])
async def test_unknown_order_can_reset_from_details_without_starting(console_server, config, entry):
    from assistant.models import Status
    app, origin = console_server
    task = app.state.store.create(config)
    app.state.store.update(task['id'], status=Status.WAITING, submit_intent='unknown-attempt',
        result={'outcome':'unknown','message':'需要核对订单','payment_entered':False})
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={'width':375,'height':900})
        await page.goto(origin)
        await page.get_by_label('控制台访问码').fill(app.state.access.code)
        await page.get_by_role('button',name='连接工作台').click()
        await expect(page.get_by_role('button',name='核验订单',exact=True)).to_be_visible()
        if entry == 'list':
            await page.get_by_role('button',name=f'重置任务 {config.product.sku}',exact=True).click()
        else:
            await page.get_by_role('button',name=f'查看任务 {config.product.sku}').click()
            await page.get_by_role('button',name='重置到抢购前',exact=True).click()
        confirm = page.get_by_role('button',name='确认未下单并重置',exact=True)
        await expect(confirm).to_be_disabled()
        await page.get_by_role('checkbox',name='我已检查京东订单列表，确认本次没有生成订单').check()
        await confirm.click()
        await expect(page.get_by_role('dialog',name='任务详情')).to_be_visible()
        await expect(page.locator('.detail-message')).to_contain_text('已确认未下单并重置')
        await expect(page.get_by_role('button',name='修改配置',exact=True)).to_be_enabled()
        assert app.state.store.get(task['id'])['submit_intent'] is None
        assert not app.state.engine.busy
        assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        await browser.close()


async def test_api_candidate_order_is_shown_without_success_or_reset(console_server, config):
    app, origin = console_server
    task = app.state.store.create(config)
    app.state.store.update(task['id'], status='waiting_human', submit_intent='api-unknown',
                           submission_order_id='123456789012')
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={'width':375,'height':900})
        await page.goto(origin)
        await page.get_by_label('控制台访问码').fill(app.state.access.code)
        await page.get_by_role('button',name='连接工作台').click()
        await expect(page.get_by_role('button',name=f'重置任务 {config.product.sku}',exact=True)).to_have_count(0)
        await page.get_by_role('button',name=f'查看任务 {config.product.sku}').click()
        await expect(page.locator('.detail-grid')).to_contain_text('123456789012（待交叉核验）')
        await expect(page.get_by_role('button',name='重置到抢购前',exact=True)).to_have_count(0)
        assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert not app.state.engine.busy
        await browser.close()
