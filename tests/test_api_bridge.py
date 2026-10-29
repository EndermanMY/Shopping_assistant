import asyncio
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from assistant.adapters.jd_submit_bridge import PageSignedSubmit


async def test_child_frame_cannot_supply_the_order_request():
    main = object()
    page = SimpleNamespace(main_frame=main)
    bridge = PageSignedSubmit(None,page,lambda:None)
    request = SimpleNamespace(url='https://api.m.jd.com/api?functionId=balance_submitOrder_pc',
                              method='POST',frame=object(),resource_type='xhr')
    route = SimpleNamespace(request=request, abort=AsyncMock(), fallback=AsyncMock())
    await bridge._handle(route)
    route.abort.assert_awaited_once()
    route.fallback.assert_not_awaited()
    assert bridge.prepared is None


async def test_sdk_duplicate_xhr_cannot_pass_the_native_fetch_slot():
    page = SimpleNamespace(main_frame=object())
    bridge = PageSignedSubmit(None,page,lambda:None)
    bridge.armed = True
    bridge.prepared = SimpleNamespace(url='https://api.m.jd.com/api?functionId=balance_submitOrder_pc',body='fixture')
    request = SimpleNamespace(url=bridge.prepared.url,method='POST',frame=page.main_frame,
                              resource_type='xhr',post_data='fixture')
    route = SimpleNamespace(request=request,abort=AsyncMock(),fallback=AsyncMock())
    await bridge._handle(route)
    route.abort.assert_awaited_once()
    route.fallback.assert_not_awaited()
    assert bridge.forwarded == 0


@pytest.mark.parametrize('method',['GET','PUT'])
async def test_unrecognized_writes_and_get_order_creation_never_pass(method):
    page=SimpleNamespace(main_frame=object(),url='https://trade.jd.com/checkout')
    bridge=PageSignedSubmit(None,page,lambda:None)
    bridge.future=asyncio.get_running_loop().create_future()
    request=SimpleNamespace(url='https://api.m.jd.com/changed?functionId=createOrder',method=method)
    route=SimpleNamespace(request=request,abort=AsyncMock(),fallback=AsyncMock())
    await bridge._handle(route)
    route.abort.assert_awaited_once();route.fallback.assert_not_awaited()
    assert bridge.future.exception() is not None


async def test_post_order_navigation_allows_manual_cashier_traffic_but_no_second_order():
    page=SimpleNamespace(main_frame=object(),url='https://order.jd.com/center/list.action')
    bridge=PageSignedSubmit(None,page,lambda:None)
    bridge.closed=True;bridge.checkout_url='https://trade.jd.com/checkout'
    request=SimpleNamespace(url='https://payc.m.jd.com/queryCashier',method='POST')
    route=SimpleNamespace(request=request,abort=AsyncMock(),fallback=AsyncMock())
    await bridge._handle(route);route.fallback.assert_awaited_once()
    request.url='https://api.m.jd.com/api?functionId=balance_submitOrder_pc'
    request.frame=page.main_frame;request.resource_type='xhr'
    route=SimpleNamespace(request=request,abort=AsyncMock(),fallback=AsyncMock())
    await bridge._handle(route);route.abort.assert_awaited_once();route.fallback.assert_not_awaited()


async def test_api_cors_preflight_is_not_mistaken_for_a_second_order():
    bridge=PageSignedSubmit(None,SimpleNamespace(main_frame=object()),lambda:None)
    request=SimpleNamespace(url='https://api.m.jd.com/api?functionId=balance_submitOrder_pc',method='OPTIONS')
    route=SimpleNamespace(request=request,abort=AsyncMock(),fallback=AsyncMock())
    await bridge._handle(route);route.fallback.assert_awaited_once();route.abort.assert_not_awaited()
    assert bridge.forwarded==0
