import asyncio
import json
from datetime import datetime, timedelta

import pytest
from assistant.adapters.jd import JDAdapter, JDUrls
from assistant.engine import Engine
from assistant.models import CHINA, Status
from tests.test_browser_flow import browser_harness, mock_shop, run_shop

async def api_task_backend(context, state, store, *, status=200, drop=False):
    requests = []
    async def handle(route):
        if route.request.method == 'OPTIONS':
            origin = route.request.headers.get('origin', '')
            return await route.fulfill(status=204, headers={'Access-Control-Allow-Origin':origin,
                'Access-Control-Allow-Credentials':'true','Access-Control-Allow-Methods':'POST',
                'Access-Control-Allow-Headers':'content-type,x-api-eid-token'})
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
    config = config.model_copy(update={'use_api':True})
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
    config = config.model_copy(update={'use_api':True})
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
    config = config.model_copy(update={'use_api':True,'scheduled_at':datetime.now(CHINA)-timedelta(milliseconds=1),'window_seconds':30})
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
    config = config.model_copy(update={'use_api':True})
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
    config = config.model_copy(update={'use_api':True,'scheduled_at':datetime.now(CHINA)-timedelta(milliseconds=1),'window_seconds':30})
    task=store.create(config);engine.start(task['id']);await engine.job
    assert not calls and state['submits'] == 0
    assert store.get(task['id'])['submit_intent']


async def test_opt_in_api_unsupported_sdk_never_falls_back_to_network(store, config, mock_shop, browser_harness, tmp_path):
    state, _ = mock_shop
    state.update(modernCheckout=True, submitApi='balance_submitOrder_pc')  # fetch SDK not calibrated
    calls = await api_task_backend(await browser_harness.ensure(), state, store)
    config = config.model_copy(update={'use_api':True})
    task, _ = await run_shop(store, config, mock_shop, browser_harness, tmp_path)
    assert not calls and state['submits'] == 0
    assert task['status'] == Status.WAITING


@pytest.mark.parametrize('browser_harness',['persistent','native_cdp'],indirect=True)
@pytest.mark.parametrize('mode',['normal','ignore_price','skip_checks'])
async def test_api_modes_support_sdk_loading_and_single_dispatch(store,config,mock_shop,browser_harness,tmp_path,mode):
    state,_=mock_shop;state.update(modernCheckout=True,submitApi='balance_submitOrder_pc',sdkXHR=True,sdkButtonBusy=True,sdkEidToken=True)
    calls=await api_task_backend(await browser_harness.ensure(),state,store)
    values={'use_api':True}
    if mode=='ignore_price':values.update(ignore_price=True,max_total_cents=None);state['unknownPrice']=True
    if mode=='skip_checks':
        values.update(skip_checks=True,max_total_cents=None,address_hint='')
        state.update(wrongSku=True,wrongAddress=True,quantity=2,unknownPrice=True)
    task,_=await run_shop(store,config.model_copy(update=values),mock_shop,browser_harness,tmp_path)
    assert task['status']==Status.SUCCEEDED,task['message']
    assert len(calls)==state['submits']==1
    assert task['config']['use_api'] is True and task['config']['submit_mode']=='api'
    if mode=='skip_checks':assert '未核对' in task['message'] and task['result']['sku'] is None


async def test_unrecognized_order_endpoint_is_blocked_before_network(store,config,mock_shop,browser_harness,tmp_path):
    state,_=mock_shop;state.update(modernCheckout=True,submitApi=True,sdkXHR=True,
        submitUrl='https://api.m.jd.com/new-order-api?functionId=unknown_createOrder')
    actual=[]
    async def endpoint(route):actual.append(route.request);await route.fulfill(status=500)
    await (await browser_harness.ensure()).route('https://api.m.jd.com/new-order-api*',endpoint)
    task,_=await run_shop(store,config.model_copy(update={'use_api':True}),mock_shop,browser_harness,tmp_path)
    assert not actual and not state['submits']
    assert task['status']==Status.WAITING and task['submit_intent']


async def test_api_prepares_before_start_and_does_not_reopen_sku_at_dispatch(store,config,mock_shop,browser_harness,tmp_path):
    state,origin=mock_shop;state.update(modernCheckout=True,submitApi='balance_submitOrder_pc',sdkXHR=True,sdkButtonBusy=True)
    context=await browser_harness.ensure();calls=await api_task_backend(context,state,store)
    details=[]
    async def detail(route):details.append(datetime.now(CHINA));await route.fulfill(body='<p>Offline SKU</p>')
    await context.route('https://item.jd.com/**',detail)
    cfg=config.model_copy(update={'use_api':True,'scheduled_at':datetime.now(CHINA)+timedelta(seconds=4),'advance_ms':600,'window_seconds':15})
    adapter=JDAdapter(browser_harness,tmp_path,JDUrls(product=origin+'/?stage=product&sku={sku}',cart=origin+'/?stage=cart',orders=origin+'/?stage=orders'))
    engine=Engine(store,lambda:adapter);task=store.create(cfg);engine.start(task['id']);await engine.job
    saved=store.get(task['id'])
    assert saved['status']==Status.SUCCEEDED,saved['message']
    assert len(details)==1 and details[0]<cfg.purchase_start
    assert datetime.fromisoformat(saved['submitted_at'])>=cfg.purchase_start
    assert len(calls)==state['submits']==1
