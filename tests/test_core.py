import asyncio
from datetime import datetime, timedelta

import pytest

from assistant.adapters.jd import allowed_url, money_cents, parse_product, resolve_source
from assistant.engine import Engine
from assistant.models import CHINA, Checkout, NeedsHuman, NotReady, OrderResult, Status
from assistant.store import Store


class FakeAdapter:
    def __init__(self):
        self.clicks = 0
        self.checkouts = 0
        self.verifications = 0
        self.problem = None
        self.result = OrderResult(outcome="created", message="已确认", order_id="123456789012", sku="100327520619", total_cents=269900, payment_entered=True)
        self.not_ready = 0

    async def prepare(self, config):
        if self.problem == "captcha":
            raise NeedsHuman("请完成人工验证")
        return config.product

    async def reserve(self, config):
        pass

    async def baseline(self, config):
        return ["111111111111"]

    async def prepare_cart(self, config):
        pass

    async def enter_payment(self, config):
        return None

    async def lock_order(self, config):
        pass

    async def checkout(self, config):
        self.checkouts += 1
        if self.not_ready > 0:
            self.not_ready -= 1
            raise NotReady("尚未开售")
        if self.problem == "price":
            return Checkout(sku=config.product.sku, quantity=1, total_cents=config.max_total_cents + 1, address="已核对")
        if self.problem == "sku":
            return Checkout(sku="999999999999", quantity=1, total_cents=100, address="已核对")
        if self.problem == "quantity":
            return Checkout(sku=config.product.sku, quantity=2, total_cents=100, address="已核对")
        return Checkout(sku=config.product.sku, quantity=1, total_cents=269900, address="已核对")

    async def submit(self, config):
        self.clicks += 1
        if self.problem == "network_after_submit":
            raise TimeoutError("response lost")

    async def verify(self, config, baseline, since=None):
        self.verifications += 1
        return self.result

    async def screenshot(self, task_id):
        return None


async def execute(store, config, adapter):
    task = store.create(config)
    engine = Engine(store, lambda: adapter)
    engine.start(task["id"])
    await engine.job
    return store.get(task["id"]), engine


async def test_success_has_durable_intent_before_click(store, config):
    adapter = FakeAdapter()
    task, engine = await execute(store, config, adapter)
    assert task["status"] == Status.SUCCEEDED
    assert task["submit_intent"] and task["submitted_at"]
    assert task["baseline"] == ["111111111111"]
    assert task["result"]["order_id"] == "123456789012"
    assert adapter.clicks == 1
    with pytest.raises(ValueError, match="已创建订单"):
        engine.start(task["id"])


@pytest.mark.parametrize("problem", ["price", "sku", "quantity", "captcha"])
async def test_rejects_unsafe_checkout_before_click(store, config, problem):
    adapter = FakeAdapter()
    adapter.problem = problem
    task, _ = await execute(store, config, adapter)
    assert task["status"] == Status.WAITING
    assert task["submit_intent"] is None
    assert adapter.clicks == 0


async def test_lost_submit_response_verifies_without_another_click(store, config):
    adapter = FakeAdapter()
    adapter.problem = "network_after_submit"
    task, _ = await execute(store, config, adapter)
    assert task["status"] == Status.SUCCEEDED
    assert adapter.clicks == 1 and adapter.verifications == 1


async def test_unknown_result_resume_only_verifies(store, config):
    adapter = FakeAdapter()
    adapter.result = OrderResult(outcome="unknown", message="暂不能核实订单")
    task, engine = await execute(store, config, adapter)
    assert task["status"] == Status.WAITING
    engine.start(task["id"], resume=True)
    await engine.job
    assert adapter.clicks == 1 and adapter.checkouts == 1 and adapter.verifications == 2


async def test_stop_waiting_task_before_sale(store, config):
    config = config.model_copy(update={"scheduled_at": datetime.now(CHINA) + timedelta(minutes=10)})
    adapter = FakeAdapter()
    task = store.create(config)
    engine = Engine(store, lambda: adapter)
    engine.start(task["id"])
    await asyncio.sleep(0.01)
    await asyncio.wait_for(engine.stop(task["id"]), timeout=1)
    assert store.get(task["id"])["status"] == Status.STOPPED
    assert adapter.clicks == 0


async def test_stop_immediately_after_start(store, config):
    task = store.create(config)
    adapter = FakeAdapter()
    engine = Engine(store, lambda: adapter)
    engine.start(task["id"])
    await engine.stop(task["id"])
    assert store.get(task["id"])["status"] == Status.STOPPED
    assert adapter.clicks == 0


async def test_one_account_rejects_parallel_run(store, config):
    config = config.model_copy(update={"scheduled_at": datetime.now(CHINA) + timedelta(minutes=10)})
    adapter = FakeAdapter()
    first, second = store.create(config), store.create(config)
    engine = Engine(store, lambda: adapter)
    engine.start(first["id"])
    with pytest.raises(ValueError, match="串行"):
        engine.start(second["id"])
    await engine.stop(first["id"])


async def test_expired_slot_does_not_roll_to_tomorrow(store, config):
    config = config.model_copy(update={"scheduled_at": datetime.now(CHINA) - timedelta(days=1)})
    task = store.create(config)
    engine = Engine(store, FakeAdapter)
    with pytest.raises(ValueError, match="窗口已结束"):
        engine.start(task["id"])


class TimerClock:
    def __init__(self):
        self.wall = datetime.now(CHINA)
        self.ticks = 0.0

    def advance(self, seconds):
        self.wall += timedelta(seconds=seconds)
        self.ticks += seconds


@pytest.mark.parametrize('elapsed,expected', [(-.2,.2),(-.6,.5),(0,.5),(4.9,.5),(5,1),(14.9,1),(15,2),(300,2)])
def test_opening_retry_is_fast_and_aligned_with_sale(store, config, elapsed, expected):
    engine = Engine(store, FakeAdapter)
    sale = datetime.now(CHINA)
    config = config.model_copy(update={'scheduled_at':sale})
    engine.now = lambda: sale+timedelta(seconds=elapsed)
    assert engine.retry_interval(config) == pytest.approx(expected)


@pytest.mark.parametrize('advance_ms', [0, 250, 1750])
async def test_purchase_start_and_custom_duration_are_enforced(store, config, advance_ms):
    clock=TimerClock()
    cfg=config.model_copy(update={'scheduled_at':clock.wall+timedelta(seconds=3), 'advance_ms':advance_ms,'window_seconds':7})
    calls=[]
    class Unavailable(FakeAdapter):
        async def checkout(self, config):
            calls.append(clock.wall)
            raise NotReady('No stock')
    adapter=Unavailable();engine=Engine(store,lambda:adapter)
    engine.now=lambda:clock.wall;engine.monotonic=lambda:clock.ticks
    async def delay(seconds):
        clock.advance(seconds)
        await asyncio.sleep(0)
    engine.delay=delay
    task=store.create(cfg);engine.start(task['id']);await engine.job
    assert calls[0] == cfg.purchase_start
    assert all(cfg.purchase_start <= call < cfg.purchase_end for call in calls)
    assert clock.wall >= cfg.purchase_end
    assert store.get(task['id'])['status'] == Status.FAILED
    assert adapter.clicks == 0


async def test_clock_rollback_during_wait_never_starts_checkout_early(store, config):
    clock=TimerClock();start=clock.wall+timedelta(seconds=2)
    cfg=config.model_copy(update={'scheduled_at':start,'window_seconds':5})
    calls=[]
    class Recorder(FakeAdapter):
        async def checkout(self, config):
            calls.append(clock.wall)
            return await super().checkout(config)
    adapter=Recorder();engine=Engine(store,lambda:adapter)
    engine.now=lambda:clock.wall;engine.monotonic=lambda:clock.ticks
    jumped=False
    async def delay(seconds):
        nonlocal jumped
        clock.advance(seconds)
        if not jumped:
            clock.wall-=timedelta(seconds=1.5);jumped=True
        await asyncio.sleep(0)
    engine.delay=delay;task=store.create(cfg);engine.start(task['id']);await engine.job
    assert calls and calls[0] >= start
    assert store.get(task['id'])['status'] == Status.SUCCEEDED


async def test_clock_rollback_after_checkout_blocks_submit(store, config):
    clock=TimerClock();cfg=config.model_copy(update={'scheduled_at':clock.wall,'window_seconds':5})
    class Rollback(FakeAdapter):
        async def checkout(self, config):
            result=await super().checkout(config)
            clock.wall-=timedelta(seconds=1)
            return result
    adapter=Rollback();engine=Engine(store,lambda:adapter)
    engine.now=lambda:clock.wall;engine.monotonic=lambda:clock.ticks
    task=store.create(cfg);engine.start(task['id']);await engine.job
    assert store.get(task['id'])['status'] == Status.WAITING
    assert store.get(task['id'])['submit_intent'] is None and adapter.clicks == 0


async def test_not_ready_has_bounded_retry_then_only_one_submit(store, config):
    adapter = FakeAdapter()
    adapter.not_ready = 1
    task, _ = await execute(store, config, adapter)
    assert task["status"] == Status.SUCCEEDED
    assert adapter.checkouts == 2 and adapter.clicks == 1


def test_restart_marks_interrupted_and_retains_submit_intent(tmp_path, config):
    path = tmp_path / "restart.db"
    store = Store(path)
    task = store.create(config)
    store.update(task["id"], status=Status.SUBMITTING, submit_intent="intent-id", baseline=["11111111"])
    store.close()
    reopened = Store(path)
    assert reopened.get(task["id"])["status"] == Status.INTERRUPTED
    assert reopened.get(task["id"])["submit_intent"] == "intent-id"
    reopened.close()


async def test_restart_keeps_payment_evidence_and_only_verifies(tmp_path, config):
    path=tmp_path/'payment-restart.db';store=Store(path);task=store.create(config)
    store.update(task['id'],status=Status.LOCKING,submit_intent='one-submit',submitted_at=config.scheduled_at.isoformat(),payment_entered_at=config.scheduled_at.isoformat(),payment_order_id='123456789012',baseline=[])
    store.close();store=Store(path)
    adapter=FakeAdapter();adapter.result=adapter.result.model_copy(update={'payment_entered':False})
    engine=Engine(store,lambda:adapter);engine.start(task['id'],resume=True);await engine.job
    result=store.get(task['id'])
    assert result['status']==Status.SUCCEEDED
    assert result['result']['payment_entered'] and adapter.clicks==0 and adapter.checkouts==0
    store.close()


@pytest.mark.parametrize("value,result", [("￥2,699.00",269900),("¥ 2294.15",229415),("应付总额：2699元",269900)])
def test_money_uses_exact_cents(value, result):
    assert money_cents(value) == result


@pytest.mark.parametrize("value", ["待发布", "￥100.00 ￥200.00", "0.001", "-2"])
def test_unknown_or_ambiguous_price_stops(value):
    with pytest.raises(NeedsHuman):
        money_cents(value)


async def test_source_supports_sku_and_direct_link_without_network():
    assert await resolve_source("100327520619") == "100327520619"
    assert await resolve_source("【京东】https://item.m.jd.com/product/100327520619.html 商品") == "100327520619"


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "https://jd.com.evil.test/", "file:///data/access.json", "https://user:password@item.jd.com/100327520619.html", "https://item.jd.com:8443/"])
def test_source_disallows_non_jd_or_private_urls(url):
    assert not allowed_url(url)


async def test_reset_reserves_worker_until_order_check_finishes(store, config):
    import asyncio
    entered, release = asyncio.Event(), asyncio.Event()
    class Checking(FakeAdapter):
        async def baseline(self, config):
            entered.set()
            await release.wait()
            return []
    adapter = Checking()
    engine = Engine(store,lambda:adapter)
    task = store.create(config)
    store.update(task['id'],status=Status.WAITING,submit_intent='unknown-intent')
    reset = asyncio.create_task(engine.reset(task['id']))
    await entered.wait()
    try:
        assert engine.busy
        with pytest.raises(ValueError): engine.start(task['id'],resume=True)
        with pytest.raises(ValueError): await engine.reset(task['id'])
        assert store.get(task['id'])['submit_intent'] == 'unknown-intent'
    finally:
        release.set()
    result = await reset
    assert result['status'] == Status.STOPPED
    assert adapter.clicks == 0 and not engine.busy
