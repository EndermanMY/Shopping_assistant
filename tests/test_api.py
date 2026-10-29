from datetime import datetime, timedelta
import asyncio

from fastapi.testclient import TestClient
import pytest

from assistant.app import create_app
from assistant.models import CHINA, Status
from tests.test_core import FakeAdapter


@pytest.fixture
def config(config):
    return config.model_copy(update={"scheduled_at": datetime.now(CHINA)+timedelta(minutes=10)})


def client_for(tmp_path, authenticated=True):
    app = create_app(tmp_path, adapter_factory=FakeAdapter)
    client = TestClient(app, base_url="http://127.0.0.1")
    if authenticated:
        response = client.post("/api/auth/login", json={"code": app.state.access.code})
        assert response.status_code == 200
        assert "HttpOnly" in response.headers["set-cookie"]
    return app, client


def test_api_requires_access_code(tmp_path):
    app, client = client_for(tmp_path, False)
    with client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/tasks").status_code == 401
        assert client.post("/api/auth/login", json={"code": "wrong"}).status_code == 401
        assert client.get("/api/tasks/anything/screenshot").status_code == 401


def test_task_crud_and_sensitive_config_not_exposed_without_auth(tmp_path, config):
    app, client = client_for(tmp_path)
    with client:
        created = client.post("/api/tasks", json=config.model_dump(mode="json"))
        assert created.status_code == 201
        task = created.json()
        assert task["status"] == Status.DRAFT
        assert client.get("/api/tasks").json()[0]["id"] == task["id"]
        config = config.model_copy(update={"max_total_cents": 300000})
        assert client.put(f"/api/tasks/{task['id']}", json=config.model_dump(mode="json")).status_code == 200
        assert client.delete(f"/api/tasks/{task['id']}").status_code == 200
        assert client.get("/api/tasks").json() == []
        client.post("/api/auth/logout")
        assert client.get("/api/overview").status_code == 401


def test_cross_site_requests_and_rebinding_host_are_rejected(tmp_path):
    app, client = client_for(tmp_path)
    with client:
        assert client.post("/api/account/login", headers={"Origin": "https://attacker.invalid"}).status_code == 403
        assert client.get("/api/health", headers={"Host": "attacker.invalid"}).status_code == 400


def test_submit_record_can_be_deleted_and_restored_without_erasing_intent(tmp_path, config):
    app, client = client_for(tmp_path)
    with client:
        task = client.post("/api/tasks", json=config.model_dump(mode="json")).json()
        app.state.store.update(task["id"], submit_intent="may-have-submitted", status=Status.WAITING)
        assert client.put(f"/api/tasks/{task['id']}", json=config.model_dump(mode="json")).status_code == 409
        assert client.delete(f"/api/tasks/{task['id']}").status_code == 200
        assert client.get(f"/api/tasks/{task['id']}").status_code == 404
        assert client.get('/api/tasks').json() == []
        assert client.get('/api/tasks?deleted=true').json()[0]['submit_intent'] == 'may-have-submitted'
        assert client.post(f"/api/tasks/{task['id']}/restore").status_code == 200
        assert client.post(f"/api/tasks/{task['id']}/start").status_code == 409


def test_deleting_waiting_timer_stops_it_and_restore_does_not_restart(tmp_path, config):
    app, client = client_for(tmp_path)
    with client:
        task=client.post('/api/tasks',json=config.model_dump(mode='json')).json()
        assert client.post(f"/api/tasks/{task['id']}/start").status_code == 200
        assert client.delete(f"/api/tasks/{task['id']}").status_code == 200
        assert not app.state.engine.busy
        restored=client.post(f"/api/tasks/{task['id']}/restore").json()
        assert restored['status'] == Status.STOPPED
        assert not restored['submit_intent']
        assert not app.state.engine.busy


def test_archived_uncertain_submit_blocks_new_purchase_for_same_sku(tmp_path, config):
    app, client = client_for(tmp_path)
    with client:
        old=client.post('/api/tasks',json=config.model_dump(mode='json')).json()
        app.state.store.update(old['id'],status=Status.WAITING,submit_intent='unknown-attempt')
        client.delete(f"/api/tasks/{old['id']}")
        new=client.post('/api/tasks',json=config.model_dump(mode='json')).json()
        response=client.post(f"/api/tasks/{new['id']}/start")
        assert response.status_code == 409 and '未核验' in response.json()['detail']


def test_delete_during_verification_finishes_check_before_archiving(tmp_path, config):
    app, client = client_for(tmp_path)
    class Verifying(FakeAdapter):
        async def verify(self, config, baseline, since=None):
            self.verifying=True
            await asyncio.sleep(.15)
            return await super().verify(config, baseline, since)
    adapter=Verifying();adapter.verifying=False
    app.state.engine.adapter_factory=lambda:adapter
    app.state.engine.now=lambda:config.scheduled_at
    with client:
        task=client.post('/api/tasks',json=config.model_dump(mode='json')).json()
        client.post(f"/api/tasks/{task['id']}/start")
        async def wait_for_verifying():
            while not adapter.verifying:
                await asyncio.sleep(.005)
        client.portal.call(wait_for_verifying)
        assert client.delete(f"/api/tasks/{task['id']}").status_code == 200
        archived=client.get('/api/tasks?deleted=true').json()[0]
        assert archived['result']['outcome']=='created' and archived['submit_intent']
        assert adapter.clicks == 1 and adapter.verifications == 1


def test_custom_timing_limits_and_past_effective_start(tmp_path, config):
    app, client = client_for(tmp_path)
    with client:
        data=config.model_dump(mode='json')|{'advance_ms':1250,'window_seconds':913}
        task=client.post('/api/tasks',json=data)
        assert task.status_code == 201
        assert task.json()['config']['advance_ms'] == 1250
        assert task.json()['config']['window_seconds'] == 913
        for patch in ({'advance_ms':-1},{'advance_ms':1.5},{'window_seconds':0},{'window_seconds':2.2}, {'scheduled_at':datetime.now(CHINA).isoformat()}, {'advance_ms':3600000}):
            assert client.post('/api/tasks',json=data|patch).status_code == 422


def test_access_code_rate_limit(tmp_path):
    app, client = client_for(tmp_path, False)
    with client:
        for _ in range(8):
            assert client.post("/api/auth/login", json={"code": "wrong"}).status_code == 401
        assert client.post("/api/auth/login", json={"code": app.state.access.code}).status_code == 401


def test_websocket_delivers_persisted_events(tmp_path):
    app, client = client_for(tmp_path)
    with client:
        with client.websocket_connect("ws://127.0.0.1/ws", headers={"origin": "http://127.0.0.1"}) as ws:
            # Each websocket queue is registered after accept; schedule the
            # log inside the ASGI event loop to avoid a cross-thread Queue put.
            client.portal.call(app.state.engine.log, None, "测试实时日志")
            message = ws.receive_json()
            assert message["type"] == "log" and message["event"]["message"] == "测试实时日志"
            assert client.get("/api/events").json()[-1]["message"] == "测试实时日志"


def test_invalid_limits_and_timezone_are_rejected(tmp_path, config):
    app, client = client_for(tmp_path)
    with client:
        for patch in ({"max_total_cents":0}, {"quantity":2}, {"scheduled_at":"2026-10-25T10:00:00"}, {"address_hint":" "}):
            raw = config.model_dump(mode="json") | patch
            assert client.post("/api/tasks", json=raw).status_code == 422


def test_reset_unknown_attempt_requires_ack_and_preserves_history(tmp_path, config):
    app, client = client_for(tmp_path)
    with client:
        task = client.post('/api/tasks', json=config.model_dump(mode='json')).json()
        old_config = config.model_copy(update={'scheduled_at':datetime.now(CHINA)-timedelta(minutes=2)})
        original = app.state.store.update(task['id'], config=old_config.model_dump(mode='json'),
            status=Status.WAITING, submit_intent='old-intent', submitted_at=datetime.now(CHINA).isoformat(),
            baseline=['111111111111'], screenshot='old.png',
            result={'outcome':'unknown','message':'unknown','payment_entered':False},
            payment_entered_at=None, payment_order_id=None)
        url = f"/api/tasks/{task['id']}/reset"
        assert client.post(url, json={}).status_code == 422
        assert client.post(url, json={'confirm_no_order':'true'}).status_code == 422
        result = client.post(url, json={'confirm_no_order':True})
        assert result.status_code == 200, result.text
        reset = result.json()
        assert reset['status'] == Status.STOPPED and reset['config'] == original['config']
        for key in ('submit_intent','submitted_at','baseline','result','screenshot','payment_entered_at','payment_order_id'):
            assert reset[key] is None
        import json
        snapshot = json.loads(app.state.store.db.execute('SELECT snapshot FROM task_resets').fetchone()[0])
        assert snapshot['submit_intent'] == 'old-intent' and snapshot['result']['outcome'] == 'unknown'
        assert not app.state.engine.busy
        assert client.post(f"/api/tasks/{task['id']}/start").status_code == 409  # old scene not reused
        assert client.put(f"/api/tasks/{task['id']}",json=config.model_dump(mode='json')).status_code == 200


@pytest.mark.parametrize('problem', ['pending', 'network', 'created', 'running'])
def test_reset_refuses_existing_orders_busy_or_unreadable_list(tmp_path, config, problem):
    from assistant.models import NeedsHuman
    app, client = client_for(tmp_path)
    class Blocked(FakeAdapter):
        async def baseline(self, config):
            if problem == 'pending':
                raise NeedsHuman('订单列表已有该商品的待付款订单')
            raise RuntimeError('network')
    with client:
        task = client.post('/api/tasks',json=config.model_dump(mode='json')).json()
        app.state.store.update(task['id'],status=Status.WAITING,submit_intent='keep-intent')
        if problem == 'created':
            app.state.store.update(task['id'],result={'outcome':'created','order_id':'123456789012'})
        if problem == 'running':
            other_config = config.model_copy(update={'product':config.product.model_copy(update={'sku':'100000000002'})})
            other = client.post('/api/tasks',json=other_config.model_dump(mode='json')).json()
            assert client.post(f"/api/tasks/{other['id']}/start").status_code == 200
        else:
            app.state.engine.adapter_factory = Blocked
        response = client.post(f"/api/tasks/{task['id']}/reset",json={'confirm_no_order':True})
        assert response.status_code == (502 if problem == 'network' else 409)
        assert app.state.store.get(task['id'])['submit_intent'] == 'keep-intent'
        assert app.state.store.db.execute('SELECT COUNT(*) FROM task_resets').fetchone()[0] == 0


def test_cooldown_blocks_repeated_start_and_resume_without_changing_task(tmp_path, config):
    app, client = client_for(tmp_path)
    with client:
        task = client.post('/api/tasks',json=config.model_dump(mode='json')).json()
        app.state.browser.protection.pause('当前页面异常')
        for operation in ('start','resume','resume'):
            response = client.post(f"/api/tasks/{task['id']}/{operation}")
            assert response.status_code == 409
        assert not app.state.engine.busy
        assert app.state.store.get(task['id'])['status'] == Status.DRAFT
        assert client.get('/api/account/status').json()['access_paused']


def test_open_only_browser_endpoint_does_not_request_login_page(tmp_path):
    from unittest.mock import AsyncMock
    app, client = client_for(tmp_path)
    with client:
        app.state.browser.login = AsyncMock()
        response = client.post('/api/account/login?open_only=true')
        assert response.status_code == 200
        app.state.browser.login.assert_awaited_once_with(open_only=True)


def test_api_mode_is_optional_and_persisted_without_starting(tmp_path, config):
    app, client = client_for(tmp_path)
    with client:
        assert config.submit_mode == 'dom'
        payload = config.model_dump(mode='json')
        payload['submit_mode'] = 'api'
        created = client.post('/api/tasks',json=payload)
        assert created.status_code == 201
        assert created.json()['config']['submit_mode'] == 'api'
        assert created.json()['status'] == 'draft' and not app.state.engine.busy
        payload['submit_mode'] = 'retry-until-success'
        assert client.post('/api/tasks',json=payload).status_code == 422
