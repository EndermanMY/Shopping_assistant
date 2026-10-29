import asyncio
from datetime import datetime, timedelta

import pytest

from assistant.adapters.jd import JDAdapter, JDUrls
from assistant.engine import Engine
from assistant.models import CHINA, Checkout, NeedsHuman, NotReady, Status
from tests.test_core import FakeAdapter, TimerClock
from tests.test_browser_flow import browser_harness, mock_shop, api_task_backend


@pytest.mark.parametrize('advance_ms', [0, 600, 5000])
async def test_preparation_finishes_before_ten_second_target_and_dispatch_waits(store, config, advance_ms):
    clock = TimerClock()
    cfg = config.model_copy(update={'scheduled_at':clock.wall+timedelta(seconds=45),'advance_ms':advance_ms,'window_seconds':30})
    trace = []
    class Prepared(FakeAdapter):
        async def prepare_checkout(self, cfg):
            trace.append(('prepare',clock.wall))
            clock.advance(4.359)
            trace.append(('ready',clock.wall))
            return Checkout(sku=cfg.product.sku,quantity=1,total_cents=100,address='已核对')
        async def checkout(self,cfg):
            trace.append(('recheck',clock.wall))
            return await super().checkout(cfg)
        async def submit(self,cfg):
            trace.append(('submit',clock.wall))
            await super().submit(cfg)
    adapter=Prepared();engine=Engine(store,lambda:adapter)
    engine.now=lambda:clock.wall;engine.monotonic=lambda:clock.ticks
    async def delay(seconds): clock.advance(seconds);await asyncio.sleep(0)
    engine.delay=delay
    task=store.create(cfg);engine.start(task['id']);await engine.job
    times=dict(trace)
    assert times['prepare']==cfg.checkout_prepare_at
    assert times['ready'] <= cfg.checkout_ready_by
    assert times['recheck']==times['submit']==cfg.purchase_start
    assert adapter.clicks==1


async def test_locked_preparation_does_not_prevent_on_time_checkout(store, config):
    clock=TimerClock();cfg=config.model_copy(update={'scheduled_at':clock.wall+timedelta(seconds=25)})
    checks=[]
    class Locked(FakeAdapter):
        async def prepare_checkout(self,cfg): raise NotReady('未解锁')
        async def checkout(self,cfg): checks.append(clock.wall);return await super().checkout(cfg)
    adapter=Locked();engine=Engine(store,lambda:adapter)
    engine.now=lambda:clock.wall;engine.monotonic=lambda:clock.ticks
    async def delay(seconds): clock.advance(seconds);await asyncio.sleep(0)
    engine.delay=delay
    task=store.create(cfg);engine.start(task['id']);await engine.job
    assert checks==[cfg.purchase_start] and adapter.clicks==1
    assert any('未解锁' in e['message'] for e in store.events(task['id']))


async def test_stop_during_checkout_preparation_has_no_submission_intent(store,config):
    entered=asyncio.Event()
    class Preparing(FakeAdapter):
        async def prepare_checkout(self,cfg): entered.set();await asyncio.Event().wait()
    cfg=config.model_copy(update={'scheduled_at':datetime.now(CHINA)+timedelta(seconds=10)})
    adapter=Preparing();engine=Engine(store,lambda:adapter)
    task=store.create(cfg);engine.start(task['id'])
    await asyncio.wait_for(entered.wait(),timeout=2)
    assert store.get(task['id'])['status']==Status.PRECHECKING
    await engine.stop(task['id'])
    assert store.get(task['id'])['status']==Status.STOPPED
    assert not store.get(task['id'])['submit_intent'] and adapter.clicks==0


@pytest.mark.parametrize('mode',['dom','api'])
async def test_browser_prepares_before_start_and_reuses_identity_at_dispatch(store,config,mock_shop,browser_harness,tmp_path,mode):
    state,origin=mock_shop
    state.update(modernCheckout=True,sdkButtonBusy=True)
    context=await browser_harness.ensure()
    if mode=='api':
        state.update(submitApi='balance_submitOrder_pc',sdkXHR=True)
        api_calls=await api_task_backend(context,state,store)
    details=[]
    async def detail(route):
        details.append(datetime.now(CHINA))
        await route.fulfill(body='<p>Offline SKU evidence</p>')
    await context.route('https://item.jd.com/**',detail)
    cfg=config.model_copy(update={'scheduled_at':datetime.now(CHINA)+timedelta(seconds=4),'advance_ms':600,
                                  'window_seconds':30,'submit_mode':mode})
    adapter=JDAdapter(browser_harness,tmp_path,JDUrls(product=origin+'/?stage=product&sku={sku}',cart=origin+'/?stage=cart',orders=origin+'/?stage=orders'))
    engine=Engine(store,lambda:adapter);task=store.create(cfg);engine.start(task['id']);await engine.job
    result=store.get(task['id'])
    assert result['status']==Status.SUCCEEDED,result['message']
    assert len(details)==1 and details[0]<cfg.purchase_start
    checkout_time=datetime.fromisoformat(next(x['at'] for x in state['trace'] if x['step']=='/checkout-check'))
    assert checkout_time<cfg.purchase_start
    submitted=datetime.fromisoformat(result['submitted_at'])
    assert submitted>=cfg.purchase_start
    assert (submitted-cfg.purchase_start).total_seconds()<1
    assert state['submits']==1
    if mode=='api': assert len(api_calls)==1


@pytest.mark.parametrize('problem',['price','quantity','sku','address','payment','handler'])
async def test_critical_changes_invalidate_prepared_checkout(config,mock_shop,browser_harness,tmp_path,problem):
    state,origin=mock_shop;state['modernCheckout']=True
    context=await browser_harness.ensure()
    adapter=JDAdapter(browser_harness,tmp_path);adapter.page=await browser_harness.task_page()
    await adapter.page.goto(origin+'/?stage=checkout')
    await adapter.ensure_manual_payment()
    await adapter.read_checkout(config)
    proof=adapter.checkout_proof
    assert await proof.matches(config)
    scripts={
        'price':"document.querySelector('.price').textContent='999999.00'",
        'quantity':"document.querySelector('.quantity-test').textContent='×2'",
        'sku':"document.querySelector('.title-test').textContent='Other SKU';document.querySelector('.title-test').onclick=()=>window.open('https://item.jd.com/999999999999.html')",
        'address':"document.querySelector('.active-test').textContent='Wrong address'",
        'payment':"document.querySelector('.modePaymentLi-test input').click()",
        'handler':"document.querySelector('.title-test').onclick=()=>window.open('https://item.jd.com/999999999999.html')",
    }
    await adapter.page.evaluate(scripts[problem])
    assert not await proof.matches(config)
    with pytest.raises(NeedsHuman): await adapter.recheck_checkout(config)
    assert state['submits']==0
    await adapter.aclose()


async def test_unreleased_price_returns_to_cart_without_pausing_for_manual_work(config,mock_shop,browser_harness,tmp_path):
    state,origin=mock_shop;state['unknownPrice']=True
    cfg=config.model_copy(update={'scheduled_at':datetime.now(CHINA)+timedelta(seconds=10)})
    adapter=JDAdapter(browser_harness,tmp_path,JDUrls(cart=origin+'/?stage=cart'))
    adapter.page=await browser_harness.task_page();await adapter.prepare_cart(cfg)
    with pytest.raises(NotReady,match='价格尚未公布'): await adapter.prepare_checkout(cfg)
    assert adapter.page.url==origin+'/?stage=cart'
    assert state['submits']==0


async def test_valid_checkout_proof_does_not_hide_a_new_verification_prompt(config,mock_shop,browser_harness,tmp_path):
    state,origin=mock_shop
    adapter=JDAdapter(browser_harness,tmp_path);adapter.page=await browser_harness.task_page()
    await adapter.page.goto(origin+'/?stage=checkout')
    await adapter.read_checkout(config)
    await adapter.page.locator('body').evaluate("n => n.insertAdjacentHTML('beforeend','<p>请完成安全验证</p>')")
    assert await adapter.checkout_proof.matches(config)
    with pytest.raises(NeedsHuman,match='验证'): await adapter.recheck_checkout(config)
    assert state['submits']==0
    await adapter.aclose()
