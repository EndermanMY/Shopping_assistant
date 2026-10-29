import asyncio
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
