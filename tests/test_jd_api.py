import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from assistant.adapters.jd_api import SubmitProbe, parse_submit_response, submit_endpoint
from assistant.protection import AccessLimited, AccessProtection


@pytest.mark.parametrize('url,method,expected', [
    ('https://api.m.jd.com/api?functionId=pc_trade_submitOrder', 'POST', 'pc_trade_submitOrder'),
    ('https://api.m.jd.com/api?appid=pctrade-core&functionId=balance_submitOrder_pc', 'POST', 'balance_submitOrder_pc'),
    ('https://trade.jd.com/shopping/order/submitOrder.action', 'POST', 'legacy_submitOrder'),
    ('https://api.m.jd.com/api?functionId=pc_trade_submitOrder', 'GET', None),
    ('https://evil.example/api?functionId=pc_trade_submitOrder', 'POST', None),
    ('https://api.m.jd.com.evil.example/api?functionId=pc_trade_submitOrder', 'POST', None),
    ('https://api.m.jd.com/api?functionId=pc_trade_submitOrder&functionId=other', 'POST', None),
    ('http://api.m.jd.com/api?functionId=pc_trade_submitOrder', 'POST', None),
    ('https://api.m.jd.com/api?functionId=other', 'POST', None),
    ('https://api.m.jd.com:8443/api?functionId=balance_submitOrder_pc', 'POST', None),
])
def test_endpoint_scope(url, method, expected):
    assert submit_endpoint(url, method) == expected


@pytest.mark.parametrize('payload,kind,order_id', [
    ({'success': True, 'orderId': '123456789012'}, 'candidate', '123456789012'),
    ({'code': '0', 'data': {'orderId': 123456789012}}, 'candidate', '123456789012'),
    ({'code': '0', 'body': {'resultFlag': True, 'order': {'orderIdStr':'123456789012'}}}, 'candidate', '123456789012'),
    ({'code': '0', 'body': {'resultFlag': False, 'order': {'orderIdStr':'123456789012'}}}, 'rejected', None),
    ({'code': '0', 'body': {'order': {'orderIdStr':'123456789012'}}}, 'unknown', None),
    ({'code': '710', 'message':'error'}, 'restricted', None),
    ({'code': '-1', 'echo':'challenge'}, 'restricted', None),
    ({'success': False, 'result': {'success': True, 'orderId': '123456789012'}}, 'rejected', None),
    ({'code': False, 'orderId': '123456789012'}, 'rejected', None),
    ({'orderId': '123456789012'}, 'unknown', None),
    ({'success': True, 'orderId': '123'}, 'unknown', None),
    ({'success': True, 'orderId': '123456789012', 'data': {'orderId': '123456789099'}}, 'unknown', None),
    ({'success': False, 'message': '请完成安全验证'}, 'restricted', None),
    ({'code': '0', 'redirectUrl': 'https://trade.m.jd.com/common/limit.html'}, 'restricted', None),
    ({'success': False, 'message': '商品已售完'}, 'rejected', None),
    (['unrecognized'], 'unknown', None),
])
def test_schema_never_invents_success(payload, kind, order_id):
    signal = parse_submit_response(payload)
    assert (signal.kind, signal.order_id) == (kind, order_id)


class Page:
    main_frame = object()
    def __init__(self): self.listeners = {}
    def on(self, event, handler): self.listeners[event] = handler
    def remove_listener(self, event, handler): self.listeners.pop(event, None)


class Request:
    url = 'https://api.m.jd.com/api?functionId=pc_trade_submitOrder&h5st=SECRET'
    method = 'POST'
    def __init__(self, frame): self.frame = frame


async def test_correlates_only_current_request_and_does_not_save_raw_response(tmp_path):
    browser = SimpleNamespace(protection=AccessProtection(tmp_path))
    page = Page()
    captured = []
    probe = SubmitProbe(browser, page, captured.append)
    probe.start()
    old = Request(page.main_frame)
    body = AsyncMock(return_value=b'{"success":true,"orderId":"123456789012","cookie":"SECRET"}')
    response = SimpleNamespace(request=old, status=200, headers={'content-type':'application/json'}, body=body)
    probe._response(response)
    assert not probe.jobs
    probe._request(old)
    probe._response(response)
    await probe.close()
    assert captured == ['123456789012']
    assert probe.signal.kind == 'candidate'
    assert 'SECRET' not in json.dumps(probe.samples)
    assert not page.listeners


@pytest.mark.parametrize('status', [403, 429])
async def test_submit_api_restriction_pauses_without_reading_or_retrying(tmp_path, status):
    browser = SimpleNamespace(protection=AccessProtection(tmp_path))
    page = Page()
    probe = SubmitProbe(browser, page)
    probe.start()
    req = Request(page.main_frame)
    probe._request(req)
    body = AsyncMock()
    probe._response(SimpleNamespace(request=req, status=status, headers={'retry-after':'600'}, body=body))
    await probe.close()
    with pytest.raises(AccessLimited): probe.check()
    body.assert_not_awaited()
    assert 598 <= browser.protection.state()['remaining_seconds'] <= 600


async def test_unrelated_frame_is_not_observed(tmp_path):
    page = Page()
    probe = SubmitProbe(SimpleNamespace(), page)
    probe.start()
    probe._request(Request(object()))
    assert not probe.requests
    await probe.close()
