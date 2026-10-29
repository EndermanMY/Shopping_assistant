"""Main-branch DOM submission and semantic verification regressions, offline."""
import asyncio
from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError
from playwright.async_api import async_playwright, expect

from assistant.adapters.jd import JDAdapter, JDUrls
from assistant.engine import Engine
from assistant.models import CHINA, NeedsHuman, Status, TaskConfig
from tests.test_browser_flow import browser_harness, mock_shop, run_shop
from tests.test_console import console_server


async def test_cosmetic_rerenders_do_not_repeat_full_checkout_checks(config,mock_shop,browser_harness,tmp_path):
    state,origin=mock_shop;state['modernCheckout']=True
    adapter=JDAdapter(browser_harness,tmp_path);adapter.page=await browser_harness.task_page()
    await adapter.page.goto(origin+'/?stage=checkout');await adapter.ensure_manual_payment()
    calls=[];original=adapter._read_checkout_fields
    async def render(cfg,**kwargs):
        calls.append(1)
        result=await original(cfg,**kwargs)
        await adapter.page.evaluate("""() => {
            const goods=document.querySelector('.sku-item');
            goods.classList.toggle('hovered');goods.style.outline='none';
            goods.dataset.renderTick=String(Date.now());
            const payment=document.querySelector('.modePaymentLi-test');
            payment.__reactProps$fixture={onClick:()=>{}};
        }""")
        return result
    adapter._read_checkout_fields=render
    await adapter.read_checkout(config)
    # Nested markup changes with the same visible quantity also stay stable.
    await adapter.page.locator('.quantity-test').evaluate("n=>n.innerHTML='<span>'+n.textContent+'</span>'")
    await adapter.recheck_checkout(config);await adapter.recheck_checkout(config)
    assert len(calls)==1 and state['submits']==0
    await adapter.aclose()


async def test_continuous_real_checkout_changes_pause_after_two_reads(config,mock_shop,browser_harness,tmp_path):
    state,origin=mock_shop
    adapter=JDAdapter(browser_harness,tmp_path);adapter.page=await browser_harness.task_page()
    await adapter.page.goto(origin+'/?stage=checkout')
    original=adapter._read_checkout_fields;calls=[]
    async def change(cfg,**kwargs):
        result=await original(cfg,**kwargs);calls.append(1)
        await adapter.page.get_by_test_id('total').evaluate('(n,v)=>n.textContent=v',str(2698-len(calls)))
        return result
    adapter._read_checkout_fields=change
    with pytest.raises(NeedsHuman,match='连续变化'): await adapter.read_checkout(config)
    assert len(calls)==2 and state['submits']==0
    await adapter.aclose()


def test_legacy_api_configuration_is_normalized_to_dom_and_skip_is_opt_in(config):
    raw=config.model_dump(mode='json');raw['submit_mode']='api'
    cfg=TaskConfig.model_validate(raw)
    assert cfg.submit_mode=='dom' and not cfg.skip_checks
    raw.update(skip_checks=True,max_total_cents=None,address_hint='')
    assert TaskConfig.model_validate(raw).skip_checks
    raw['skip_checks']=False
    with pytest.raises(ValidationError): TaskConfig.model_validate(raw)


@pytest.mark.parametrize('problem',['wrongSku','quantity','wrongAddress','unknownPrice'])
async def test_unchecked_mode_uses_no_checkout_or_order_business_validation(store,config,mock_shop,browser_harness,tmp_path,problem):
    state,origin=mock_shop;state.update(modernCheckout=True,paymentOrderId='123456789013')
    state[problem]=2 if problem=='quantity' else True
    cfg=config.model_copy(update={'skip_checks':True,'max_total_cents':None,'address_hint':'',
        'scheduled_at':datetime.now(CHINA)-timedelta(milliseconds=1),'window_seconds':15})
    adapter=JDAdapter(browser_harness,tmp_path,JDUrls(product=origin+'/?stage=product&sku={sku}',cart=origin+'/?stage=cart',orders=origin+'/?stage=orders'))
    async def forbidden(*args,**kwargs): raise AssertionError('Business verification must not run')
    adapter._read_checkout_fields=forbidden;adapter._orders=forbidden;adapter.baseline=forbidden;adapter.reserve=forbidden
    engine=Engine(store,lambda:adapter);task=store.create(cfg);engine.start(task['id']);await engine.job
    result=store.get(task['id'])
    assert result['status']==Status.SUCCEEDED,result['message']
    assert state['submits']==1 and result['result']['order_id']=='123456789013'
    assert result['result']['sku'] is None and result['result']['total_cents'] is None
    assert '未核对' in result['message']
    assert not any('已核对' in e['message'] for e in store.events(task['id']))
    with pytest.raises(ValueError): engine.start(task['id'])


async def test_unchecked_mode_without_this_clicks_order_id_never_guesses_or_retries(store,config,mock_shop,browser_harness,tmp_path):
    mock_shop[0]['modernCheckout']=True
    cfg=config.model_copy(update={'skip_checks':True,'max_total_cents':None,'address_hint':''})
    task,engine=await run_shop(store,cfg,mock_shop,browser_harness,tmp_path)
    assert task['status']==Status.WAITING and task['result']['outcome']=='unknown'
    engine.start(task['id'],resume=True);await engine.job
    assert mock_shop[0]['submits']==1


@pytest.mark.parametrize('stop_before_start',[True,False])
async def test_unchecked_mode_still_waits_for_effective_start_and_can_stop_before_it(store,config,mock_shop,browser_harness,tmp_path,stop_before_start):
    state,origin=mock_shop;state.update(modernCheckout=True,paymentOrderId='123456789013',wrongAddress=True)
    await browser_harness.ensure()
    cfg=config.model_copy(update={'skip_checks':True,'max_total_cents':None,'address_hint':'',
        'scheduled_at':datetime.now(CHINA)+timedelta(seconds=3),'advance_ms':200,'window_seconds':15})
    adapter=JDAdapter(browser_harness,tmp_path,JDUrls(product=origin+'/?stage=product&sku={sku}',cart=origin+'/?stage=cart',orders=origin+'/?stage=orders'))
    engine=Engine(store,lambda:adapter);task=store.create(cfg);engine.start(task['id'])
    while store.get(task['id'])['status']!=Status.SCHEDULED:
        assert engine.busy;await asyncio.sleep(.01)
    if stop_before_start:
        await engine.stop(task['id'])
        assert state['submits']==0 and not store.get(task['id'])['submit_intent']
        assert store.get(task['id'])['status']==Status.STOPPED
    else:
        await engine.job
        result=store.get(task['id'])
        assert result['status']==Status.SUCCEEDED,result['message']
        assert datetime.fromisoformat(result['submitted_at'])>=cfg.purchase_start
        assert state['submits']==1


@pytest.mark.parametrize('width',[1440,375])
async def test_unchecked_mode_ui_default_off_and_persists_without_budget_or_address(console_server,config,width):
    app,origin=console_server
    async with async_playwright() as pw:
        browser=await pw.chromium.launch();page=await browser.new_page(viewport={'width':width,'height':900})
        await page.goto(origin);await page.get_by_label('控制台访问码').fill(app.state.access.code)
        await page.get_by_role('button',name='连接工作台').click()
        await page.get_by_role('button',name='创建抢购任务',exact=True).click()
        await expect(page.get_by_label('无需条件核对模式',exact=True)).not_to_be_checked()
        assert await page.locator('#submit-mode').count()==0
        product=config.product.model_copy(update={'sale_start':(datetime.now(CHINA)+timedelta(minutes=5)).isoformat()})
        await page.route('**/api/products/resolve',lambda r:r.fulfill(body=product.model_dump_json(),content_type='application/json'))
        await page.get_by_label('商品链接 / 京东分享文案 / SKU').fill(config.product.sku)
        await page.get_by_role('button',name='解析',exact=True).click()
        await expect(page.locator('.resolved-product')).to_be_visible()
        await page.get_by_label('无需条件核对模式',exact=True).check()
        await expect(page.get_by_label('最高应付总额')).to_be_disabled()
        await expect(page.get_by_label('收货地址核对关键字')).to_be_disabled()
        assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        await page.get_by_role('button',name='保存任务',exact=True).click()
        await expect(page.locator('.task-row')).to_contain_text('无需条件核对')
        saved=app.state.store.tasks()[0]
        assert saved['config']['skip_checks'] and saved['config']['submit_mode']=='dom'
        assert saved['config']['max_total_cents'] is None and saved['config']['address_hint']==''
        assert saved['status']==Status.DRAFT and not app.state.engine.busy
        await browser.close()
