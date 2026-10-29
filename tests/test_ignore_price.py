"""Opt-in price bypass; all purchase tests use the offline mock store."""
from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError
from playwright.async_api import async_playwright, expect

from assistant.adapters.jd import JDAdapter
from assistant.models import CHINA, NeedsHuman, Status, TaskConfig
from tests.test_api import client_for
from tests.test_browser_flow import browser_harness, mock_shop, run_shop, api_task_backend
from tests.test_console import console_server


def test_price_bypass_is_explicit_and_budget_stays_required_by_default(config):
    raw=config.model_dump(mode='json');raw.pop('ignore_price',None)
    assert not TaskConfig.model_validate(raw).ignore_price
    raw.pop('max_total_cents')
    with pytest.raises(ValidationError): TaskConfig.model_validate(raw)
    raw['ignore_price']=True
    cfg=TaskConfig.model_validate(raw)
    assert cfg.max_total_cents is None and cfg.price_allowed(None)
    raw['ignore_price']='false'
    with pytest.raises(ValidationError): TaskConfig.model_validate(raw)


def test_api_persists_price_choice_and_cannot_remove_budget_without_opt_in(tmp_path,config):
    app,client=client_for(tmp_path)
    raw=config.model_dump(mode='json')
    raw.update(scheduled_at=(datetime.now(CHINA)+timedelta(minutes=5)).isoformat(),ignore_price=True,max_total_cents=None)
    with client:
        response=client.post('/api/tasks',json=raw)
        assert response.status_code==201
        task=response.json();assert task['config']['ignore_price'] is True
        assert client.get('/api/tasks').json()[0]['config']['max_total_cents'] is None
        raw['ignore_price']=False
        assert client.put(f"/api/tasks/{task['id']}",json=raw).status_code==422
        assert app.state.store.get(task['id'])['config']['ignore_price'] is True
        raw['max_total_cents']=50000
        assert client.put(f"/api/tasks/{task['id']}",json=raw).status_code==200
        assert not app.state.store.get(task['id'])['config']['ignore_price']


@pytest.mark.parametrize('mode',['dom','api'])
@pytest.mark.parametrize('problem',['unknown_price','over_cap','missing_order_total'])
async def test_opt_in_ignores_price_but_still_confirms_one_real_mock_order(store,config,mock_shop,browser_harness,tmp_path,mode,problem):
    state,_=mock_shop;state['modernCheckout']=True
    if problem!='over_cap': state['unknownPrice']=True
    if problem=='missing_order_total': state['hideOrderTotal']=True
    if mode=='api':
        state.update(submitApi='balance_submitOrder_pc',sdkXHR=True)
        calls=await api_task_backend(await browser_harness.ensure(),state,store)
    cfg=config.model_copy(update={'ignore_price':True,'max_total_cents':100,'submit_mode':mode})
    task,_=await run_shop(store,cfg,mock_shop,browser_harness,tmp_path)
    assert task['status']==Status.SUCCEEDED,task['message']
    assert task['result']['order_id'] and task['result']['payment_entered']
    assert state['submits']==1
    assert any('无视价格下单' in e['message'] for e in store.events(task['id']))
    if problem=='missing_order_total':
        assert task['result']['total_cents'] is None and '金额以京东订单为准' in task['message']
    if mode=='api': assert len(calls)==1


@pytest.mark.parametrize('problem',['wrongSku','quantity','wrongAddress','captcha'])
async def test_ignore_price_still_blocks_wrong_product_quantity_address_and_captcha(store,config,mock_shop,browser_harness,tmp_path,problem):
    state,_=mock_shop;state.update(modernCheckout=True,unknownPrice=True)
    state[problem]=2 if problem=='quantity' else True
    cfg=config.model_copy(update={'ignore_price':True,'max_total_cents':None})
    task,_=await run_shop(store,cfg,mock_shop,browser_harness,tmp_path)
    assert task['status']==Status.WAITING and not task['submit_intent']
    assert state['submits']==0


async def test_prepared_ignore_price_proof_skips_price_changes_but_not_option_change(config,mock_shop,browser_harness,tmp_path):
    state,origin=mock_shop;state.update(modernCheckout=True,unknownPrice=True)
    adapter=JDAdapter(browser_harness,tmp_path);adapter.page=await browser_harness.task_page()
    await adapter.page.goto(origin+'/?stage=checkout');await adapter.ensure_manual_payment()
    cfg=config.model_copy(update={'ignore_price':True,'max_total_cents':100})
    checked=await adapter.read_checkout(cfg)
    assert checked.total_cents is None
    await adapter.page.locator('.price').evaluate("n=>n.textContent='999999.00'")
    assert await adapter.checkout_proof.matches(cfg)
    assert not await adapter.checkout_proof.matches(config)
    await adapter.page.locator('.active-test').evaluate("n=>n.textContent='Wrong address'")
    with pytest.raises(NeedsHuman): await adapter.recheck_checkout(cfg)
    assert state['submits']==0
    await adapter.aclose()


@pytest.mark.parametrize('width',[1440,375])
async def test_console_price_option_can_be_saved_and_disabled_again(console_server,config,width):
    app,origin=console_server
    async with async_playwright() as pw:
        browser=await pw.chromium.launch()
        page=await browser.new_page(viewport={'width':width,'height':900})
        await page.goto(origin);await page.get_by_label('控制台访问码').fill(app.state.access.code)
        await page.get_by_role('button',name='连接工作台').click()
        await page.get_by_role('button',name='创建抢购任务',exact=True).click()
        toggle=page.get_by_label('无视价格下单',exact=True)
        await expect(toggle).not_to_be_checked()
        product=config.product.model_copy(update={'sale_start':(datetime.now(CHINA)+timedelta(minutes=5)).isoformat()})
        await page.route('**/api/products/resolve',lambda r:r.fulfill(body=product.model_dump_json(),content_type='application/json'))
        await page.get_by_label('商品链接 / 京东分享文案 / SKU').fill(config.product.sku)
        await page.get_by_role('button',name='解析',exact=True).click()
        await expect(page.locator('.resolved-product')).to_be_visible()
        await toggle.check();await expect(page.get_by_label('最高应付总额')).to_be_disabled()
        await page.get_by_label('收货地址核对关键字').fill('测试小区')
        await page.get_by_role('button',name='保存任务',exact=True).click()
        await expect(page.locator('.task-row')).to_contain_text('无视价格下单')
        assert app.state.store.tasks()[0]['config']['ignore_price'] is True
        assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        await page.locator('.task-product').click()
        await page.get_by_role('button',name='修改配置',exact=True).click()
        await expect(toggle).to_be_checked();await toggle.uncheck()
        await expect(page.get_by_label('最高应付总额')).to_be_enabled()
        await page.get_by_label('最高应付总额').fill('500')
        await page.get_by_role('button',name='保存任务',exact=True).click()
        await expect(page.locator('.task-row')).to_contain_text('上限 ¥500.00')
        assert not app.state.store.tasks()[0]['config']['ignore_price']
        await browser.close()
