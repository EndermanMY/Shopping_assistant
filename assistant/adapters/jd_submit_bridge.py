"""Prepare with JD's page SDK, send once via the same browser's native fetch.

The SDK request is held BEFORE network dispatch. A single matching native-fetch
request may pass; the held request is fulfilled locally with its actual result.
The page keeps a submit guard after completion, preventing SDK retries.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from urllib.parse import parse_qs, urlparse

from ..models import NeedsHuman
from .jd_api import SubmitSignal, submit_endpoint
from .jd_api_direct import PreparedSubmit, DirectSubmitTrial


API_ROUTE = re.compile(r'^https://(?:api\.m\.jd\.com(?::443)?/api|trade\.jd\.com(?::443)?/shopping/order/submitOrder\.action)(?:\?|$)')


class PageSignedSubmit:
    def __init__(self, browser, page, persist_intent, on_order=None):
        self.browser, self.page = browser, page
        self.persist_intent = persist_intent
        self.on_order = on_order
        self.future = None
        self.held = None
        self.prepared = None
        self.armed = False
        self.forwarded = 0
        self.blocked = 0
        self.closed = False
        self.signal = SubmitSignal()
        self.status = None

    async def _handle(self, route):
        request = route.request
        endpoint = submit_endpoint(request.url, request.method)
        if request.method != 'POST':
            return await route.fallback()
        if endpoint:
            try:
                main_request = request.frame == self.page.main_frame
            except Exception:
                main_request = False
            if not main_request:
                self.blocked += 1
                return await route.abort('blockedbyclient')
        if endpoint and not self.closed:
            if self.prepared is None and self.future is not None and not self.future.done():
                if request.resource_type != 'xhr':
                    self.future.set_exception(NeedsHuman('当前网页不是已校准的 XHR 下单请求，未直调；不会回退点击'))
                    self.blocked += 1
                    return await route.abort('blockedbyclient')
                headers = request.headers
                selected = {'content-type':headers.get('content-type','').split(';')[0].lower()}
                if 'x-api-eid-token' in headers:
                    selected['x-api-eid-token'] = headers['x-api-eid-token']
                self.held = route
                self.prepared = PreparedSubmit(request.url, request.post_data or '', self.page.url,
                                               self.checkout, selected, time.monotonic())
                self.future.set_result(self.prepared)
                return  # Hold this original request; it has not hit the wire.
            if (self.armed and request.resource_type == 'fetch' and self.prepared is not None and self.forwarded == 0
                    and request.url == self.prepared.url and request.post_data == self.prepared.body):
                self.armed = False
                self.forwarded += 1
                return await route.fallback()
        # Only audited read functions can proceed during/after preparation.
        functions = parse_qs(urlparse(request.url).query).get('functionId', [])
        if len(functions) == 1 and re.fullmatch(r'balance_get[A-Za-z0-9_]+|balance_success_pc|pctradesoa_getWareGrow', functions[0]):
            return await route.fallback()
        self.blocked += 1
        await route.abort('blockedbyclient')

    async def submit(self, config, checkout, button, recheck):
        self.checkout = checkout
        self.future = asyncio.get_running_loop().create_future()
        await self.page.route(API_ROUTE, self._handle)
        # Keep this page guarded and use a new task tab for future tasks.
        self.page._api_submit_guarded = True
        def before_send():
            self.persist_intent()
            self.armed = True
        client = DirectSubmitTrial(self.browser, self.page, before_send)
        try:
            await button.click(timeout=7000)
            try:
                prepared = await asyncio.wait_for(asyncio.shield(self.future), timeout=6)
            except asyncio.TimeoutError:
                raise NeedsHuman('未取得当前页面生成的已知下单 API 请求，未直调；不会回退点击') from None
            self.signal = await client.submit(config, prepared, recheck)
            if self.signal.order_id and self.on_order is not None:
                self.on_order(self.signal.order_id)
            return self.signal
        finally:
            self.armed = False
            response = client.response
            self.status = response['status'] if response is not None else None
            if self.held is not None:
                route, self.held = self.held, None
                try:
                    if response is not None and response.get('data') is not None:
                        parsed = urlparse(self.page.url)
                        origin = f'{parsed.scheme}://{parsed.netloc}'
                        await route.fulfill(status=response['status'], headers={
                            'Content-Type':'application/json', 'Access-Control-Allow-Origin':origin,
                            'Access-Control-Allow-Credentials':'true'}, body=json.dumps(response['data']))
                    else:
                        await route.abort('blockedbyclient')
                except Exception:
                    pass  # Unknown navigation/result never permits redispatch.
            self.closed = True
            self.prepared = None
            client.response = None

    async def close(self):
        self.closed = True
        self.armed = False
        if self.future is not None and not self.future.done():
            self.future.cancel()
        if self.held is not None:
            route, self.held = self.held, None
            try:
                await route.abort('blockedbyclient')
            except Exception:
                pass
        self.prepared = None
