from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from assistant.adapters.jd_api_direct import DirectSubmitTrial, PreparedSubmit
from assistant.models import Checkout, NeedsHuman
from assistant.protection import AccessLimited, AccessProtection


def trial(config, tmp_path):
    checkout = Checkout(sku=config.product.sku, quantity=1, total_cents=100, address='已核对')
    page = SimpleNamespace(url='https://trade.jd.com/shopping/order/getOrderInfo.action',
                           evaluate=AsyncMock(return_value={'status':200,'data':{'success':True,'orderId':'123456789012'}}))
    browser = SimpleNamespace(protection=AccessProtection(tmp_path))
    request = PreparedSubmit(url='https://api.m.jd.com/api?functionId=pc_trade_submitOrder',
                             body='test-only', page_url=page.url, checkout=checkout)
    return DirectSubmitTrial(browser, page, lambda:None), request, AsyncMock(return_value=checkout)


async def test_direct_candidate_sends_once_and_never_claims_purchase_success(config, tmp_path):
    client, prepared, recheck = trial(config, tmp_path)
    signal = await client.submit(config, prepared, recheck)
    assert signal.kind == 'candidate' and signal.order_id == '123456789012'
    with pytest.raises(NeedsHuman, match='禁止再次'):
        await client.submit(config, prepared, recheck)
    client.page.evaluate.assert_awaited_once()
    assert 'test-only' not in repr(prepared)


async def test_direct_timeout_is_uncertain_and_does_not_retry(config, tmp_path):
    client, prepared, recheck = trial(config, tmp_path)
    client.page.evaluate.side_effect = TimeoutError('network disconnected')
    assert (await client.submit(config, prepared, recheck)).kind == 'unknown'
    with pytest.raises(NeedsHuman): await client.submit(config, prepared, recheck)
    client.page.evaluate.assert_awaited_once()


@pytest.mark.parametrize('status', [403, 429])
async def test_direct_risk_pauses_the_whole_session(config, tmp_path, status):
    client, prepared, recheck = trial(config, tmp_path)
    client.page.evaluate.return_value = {'status':status,'data':None}
    with pytest.raises(AccessLimited): await client.submit(config, prepared, recheck)
    assert client.browser.protection.state()['paused']
    assert client.dispatched


@pytest.mark.parametrize('problem', ['stale', 'page', 'price', 'time', 'host', 'cookie'])
async def test_direct_rejects_unsafe_or_stale_request_before_dispatch(config, tmp_path, problem):
    from dataclasses import replace
    client, prepared, recheck = trial(config, tmp_path)
    if problem == 'stale': prepared = replace(prepared, prepared_at=prepared.prepared_at-3)
    if problem == 'page': client.page.url += '?changed=true'
    if problem == 'price': recheck.return_value = prepared.checkout.model_copy(update={'total_cents':99999999})
    if problem == 'time': config = config.model_copy(update={'scheduled_at':config.scheduled_at+timedelta(days=1)})
    if problem == 'host': prepared = replace(prepared, url='https://evil.example/submit')
    if problem == 'cookie': prepared = replace(prepared, headers={'cookie':'SECRET'})
    with pytest.raises(NeedsHuman): await client.submit(config, prepared, recheck)
    client.page.evaluate.assert_not_awaited()


async def test_direct_respects_existing_access_pause(config, tmp_path):
    client, prepared, recheck = trial(config, tmp_path)
    client.browser.protection.pause('访问异常')
    with pytest.raises(AccessLimited): await client.submit(config, prepared, recheck)
    client.page.evaluate.assert_not_awaited()


async def test_failed_intent_write_prevents_api_dispatch(config, tmp_path):
    client, prepared, recheck = trial(config, tmp_path)
    def fail(): raise OSError('disk unavailable')
    client.persist_intent = fail
    with pytest.raises(OSError): await client.submit(config, prepared, recheck)
    client.page.evaluate.assert_not_awaited()
