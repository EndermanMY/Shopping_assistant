"""Passive evidence from the browser's own submit request. Never replays it.

Only explicitly recognized endpoints and requests started in this attempt are
observed. Cookies, signatures, request bodies and raw responses stay in memory.
An API order ID is a candidate, never sufficient proof of a successful purchase.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from ..models import NeedsHuman
from ..protection import AccessLimited, restriction_reason


def submit_endpoint(url: str, method: str) -> str | None:
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return None
    if (parsed.scheme != 'https' or method.upper() != 'POST' or parsed.username or parsed.password
            or port not in (None, 443) or parsed.fragment):
        return None
    if parsed.hostname == 'api.m.jd.com' and parsed.path == '/api':
        functions = parse_qs(parsed.query).get('functionId', [])
        if len(functions) == 1 and functions[0] in {'pc_trade_submitOrder', 'balance_submitOrder_pc'}:
            return functions[0]
    if parsed.hostname == 'trade.jd.com' and parsed.path == '/shopping/order/submitOrder.action':
        return 'legacy_submitOrder'
    return None


@dataclass(frozen=True)
class SubmitSignal:
    kind: str = 'unknown'
    order_id: str | None = None


def parse_submit_response(payload) -> SubmitSignal:
    """Small, conservative schema set; unknown schemas cannot prove success."""
    if not isinstance(payload, dict):
        return SubmitSignal()
    nodes = [payload]
    for key in ('result', 'data', 'body'):
        if isinstance(payload.get(key), dict):
            nodes.append(payload[key])
    messages = ' '.join(str(node.get(key, ''))[:1000] for node in nodes
                        for key in ('message', 'msg', 'errorMessage', 'errMsg', 'redirectUrl'))
    # Codes and echo handling follow the public settlement 0.0.43 client.
    if any(str(node.get('code')) in {'2000', '710', '-900000001', '-900000002', '2100'}
           or (str(node.get('code')) == '-1' and (node.get('echo') or '2-11-1012-200002' in str(node.get('message', ''))))
           for node in nodes):
        return SubmitSignal('restricted')
    if restriction_reason('https://trade.jd.com/', messages) or any(
            isinstance(node.get('redirectUrl'), str) and restriction_reason(node['redirectUrl'], '') for node in nodes):
        return SubmitSignal('restricted')
    # A failed outer envelope must not be overridden by nested success data.
    if any(node.get('success') is False or node.get('resultFlag') is False
           or ('code' in node and (isinstance(node['code'], bool) or node['code'] not in (0, '0'))) for node in nodes):
        return SubmitSignal('rejected')
    ids = {str(node['orderId']) for node in nodes if 'orderId' in node
           and not isinstance(node['orderId'], bool) and re.fullmatch(r'\d{8,24}', str(node['orderId']))}
    body = payload.get('body')
    if isinstance(body, dict) and body.get('resultFlag') is True and isinstance(body.get('order'), dict):
        value = body['order'].get('orderIdStr')
        if isinstance(value, str) and re.fullmatch(r'\d{8,24}', value):
            ids.add(value)
    success = any(node.get('success') is True or ('code' in node and node['code'] in (0, '0')) for node in nodes)
    if success and len(ids) == 1:
        return SubmitSignal('candidate', ids.pop())
    return SubmitSignal()


class SubmitProbe:
    """One-shot listener, bounded in time and size, with no extra HTTP calls."""
    def __init__(self, browser, page, on_order=None):
        self.browser, self.page, self.on_order = browser, page, on_order
        self.requests = {}
        self.jobs = set()
        self.signal = SubmitSignal()
        self.samples = []
        self.started = time.monotonic()
        self.closed = False

    def start(self):
        self.page.on('request', self._request)
        self.page.on('response', self._response)

    def _request(self, request):
        endpoint = submit_endpoint(request.url, request.method)
        if self.closed or not endpoint or len(self.requests) >= 4:
            return
        try:
            if request.frame != self.page.main_frame:
                return
        except Exception:
            return
        self.requests[request] = (endpoint, time.monotonic())

    def _response(self, response):
        if self.closed or response.request not in self.requests:
            return
        job = asyncio.create_task(self._read(response))
        self.jobs.add(job)
        job.add_done_callback(self.jobs.discard)

    async def _read(self, response):
        endpoint, started = self.requests[response.request]
        sample = {'endpoint': endpoint, 'http_status': response.status,
                  'headers_ms': round((time.monotonic()-started)*1000), 'signal': 'unknown'}
        self.samples.append(sample)
        try:
            if response.status in (403, 429):
                self.signal = SubmitSignal('restricted', self.signal.order_id)
                protection = getattr(self.browser, 'protection', None)
                if protection is not None:
                    retry = response.headers.get('retry-after', '')
                    seconds = min(3600, max(300, int(retry))) if retry.isdigit() else 300
                    protection.pause('京东下单接口限制访问，已停止自动访问', seconds=seconds)
            elif response.status == 200:
                headers = response.headers
                length = headers.get('content-length', '')
                if 'application/json' not in headers.get('content-type', '').lower():
                    return
                if length.isdigit() and int(length) > 1_048_576:
                    return
                body = await asyncio.wait_for(response.body(), timeout=2)
                if len(body) > 1_048_576:
                    return
                signal = parse_submit_response(json.loads(body))
                if signal.kind == 'restricted':
                    protection = getattr(self.browser, 'protection', None)
                    if protection is not None:
                        protection.pause('京东下单接口要求人工验证，已停止自动访问')
                if self.signal.kind != 'restricted':
                    if self.signal.order_id and signal.order_id and self.signal.order_id != signal.order_id:
                        self.signal = SubmitSignal('conflict')
                    elif self.signal.kind != 'conflict' and (signal.kind != 'unknown' or self.signal.kind == 'unknown'):
                        self.signal = signal
                if signal.order_id and self.signal.kind == 'candidate' and self.on_order is not None:
                    self.on_order(signal.order_id)
        except (Exception, asyncio.CancelledError):
            # An unreadable response is uncertain, never permission to resubmit.
            return
        finally:
            sample['signal'] = self.signal.kind
            sample['body_ms'] = round((time.monotonic()-started)*1000)

    def check(self):
        if self.signal.kind == 'restricted':
            raise AccessLimited('京东下单接口限制访问或要求人工验证；已暂停，不会重复提交')
        if self.signal.kind == 'rejected':
            raise NeedsHuman('京东下单接口返回未成功状态，将只核验订单；不会改走另一通道重复提交')
        if self.signal.kind == 'conflict':
            raise NeedsHuman('本次提交返回多个不一致订单号，请人工核对；不会重复提交')

    async def close(self):
        if self.closed:
            return
        self.closed = True
        self.page.remove_listener('request', self._request)
        self.page.remove_listener('response', self._response)
        if self.jobs:
            _, pending = await asyncio.wait(list(self.jobs), timeout=2.1)
            for job in pending:
                job.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        self.requests.clear()
