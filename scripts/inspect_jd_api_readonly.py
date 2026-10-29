"""Inspect checkout and try ONE read-only native API call; block order submit.

No task/database changes, no API order dispatch, no cookie/token/body exports.
Run with the source venv from the project root. Closes its own browser on exit.
"""
import asyncio
import json
import re
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from assistant.adapters.jd import JDAdapter, inspect_product
from assistant.adapters.jd_api import parse_submit_response, submit_endpoint
from assistant.browser import Browser
from assistant.models import CHINA, NeedsHuman, TaskConfig
from assistant.protection import check_access, check_page_access

ROOT = Path(__file__).resolve().parent.parent
READ_FUNCTION = 'balance_getCurrentOrder_pc'


def field_names(value, prefix='', depth=0):
    """Schema only; no scalar values or dynamic dictionary keys are exported."""
    if depth > 3:
        return []
    result = []
    if isinstance(value, dict):
        for key, item in list(value.items())[:80]:
            if not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,49}', key):
                continue
            name = prefix+'.'+key if prefix else key
            result.append(name+':'+type(item).__name__)
            result.extend(field_names(item, name, depth+1))
    elif isinstance(value, list) and value:
        result.extend(field_names(value[0], prefix+'[]', depth+1))
    return result[:160]


async def main():
    data = ROOT/'data'
    browser = Browser(data)
    record = {'sku':'100285622568', 'checked_at':datetime.now(CHINA).isoformat(),
              'order_submit_blocked':0, 'read_api_attempted':False, 'tasks_changed':False,
              'stage':'browser', 'schemas':[]}
    jobs = set()
    async def block_submit(route):
        if submit_endpoint(route.request.url, route.request.method):
            record['order_submit_blocked'] += 1
            await route.abort()
        else:
            await route.continue_()

    async def observe(response):
        parsed = urlparse(response.url)
        function = parse_qs(parsed.query).get('functionId', [])
        if parsed.hostname != 'api.m.jd.com' or function != [READ_FUNCTION] or record['read_api_attempted']:
            return
        # Reserve the one-shot slot before any await, including our own reply.
        record['read_api_attempted'] = True
        received = time.monotonic()
        record['original_http_status'] = response.status
        payload = await asyncio.wait_for(response.json(), timeout=2)
        record['schemas'] = field_names(payload)
        if parse_submit_response(payload).kind == 'restricted' or response.status in (403,429):
            browser.protection.pause('只读结算 API 提示访问限制，已停止诊断')
            record['read_api_result'] = 'restricted'
            return
        if str(payload.get('code')) != '0' or time.monotonic()-received > 2:
            record['read_api_result'] = 'original_unconfirmed_or_stale'
            return
        check_access(browser)
        page = response.request.frame.page
        await check_page_access(browser, page)
        # The exact, freshly emitted read-only request is reused once inside
        # the same browser. No signature generation or account data export.
        result = await page.evaluate("""async spec => {
            const controller = new AbortController();
            const timer = setTimeout(()=>controller.abort(),5000);
            try {
                const r = await fetch(spec.url,{method:'POST',body:spec.body,
                    headers:{'content-type':'application/x-www-form-urlencoded'},
                    credentials:'include',redirect:'error',signal:controller.signal});
                return {status:r.status, data:await r.json()};
            } finally { clearTimeout(timer); }
        }""", {'url':response.request.url, 'body':response.request.post_data})
        record['read_api_http_status'] = result['status']
        record['read_api_ms'] = round((time.monotonic()-received)*1000)
        signal = parse_submit_response(result['data'])
        if result['status'] in (403,429) or signal.kind == 'restricted':
            browser.protection.pause('只读 API 直调受到访问限制，已停止诊断')
            record['read_api_result'] = 'restricted'
        else:
            record['read_api_result'] = 'ok' if result['status'] == 200 and str(result['data'].get('code')) == '0' else 'unconfirmed'
        print(json.dumps({'read_api_result':record['read_api_result']}), flush=True)

    def response_received(response):
        job = asyncio.create_task(observe(response))
        jobs.add(job)
        job.add_done_callback(jobs.discard)
        # Retrieve failures without printing request URLs or token-bearing exceptions.
        def completed(task):
            if not task.cancelled() and task.exception():
                record['read_api_error'] = type(task.exception()).__name__
        job.add_done_callback(completed)
    try:
        context = await browser.ensure()
        await context.route(re.compile(r'https://(?:api\.m\.jd\.com/api|trade\.jd\.com/shopping/order/submitOrder\.action)(?:\?|$)'), block_submit)
        context.on('response', response_received)
        status = await browser.status()
        record['logged_in'] = status['logged_in']
        if not status['logged_in']:
            raise NeedsHuman('应用会话需要人工登录；未继续结算检查')
        check_access(browser)
        record['stage'] = 'product'
        print('Checking product (no order submit).', flush=True)
        product = await inspect_product(record['sku'], browser)
        # Reuse only the locally configured address check; never print it.
        with sqlite3.connect((data/'assistant.db').resolve().as_uri()+'?mode=ro', uri=True) as db:
            configs = [json.loads(row[0]) for row in db.execute('SELECT config FROM tasks ORDER BY created_at DESC')]
        address = next((c['address_hint'] for c in configs if c.get('address_hint')), None)
        if not address:
            raise NeedsHuman('没有已保存的地址核对关键字，未继续结算检查')
        config = TaskConfig(product=product, scheduled_at=datetime.now(CHINA)-timedelta(milliseconds=1),
                            window_seconds=180, max_total_cents=150000, address_hint=address)
        adapter = JDAdapter(browser, data)
        adapter.page = await browser.task_page()
        record['stage'] = 'cart'
        print('Checking exact SKU in cart (no order submit).', flush=True)
        await adapter.prepare_cart(config)
        record['stage'] = 'checkout'
        print('Opening checkout; submit endpoint is blocked.', flush=True)
        checkout = await adapter.checkout(config)
        record.update(checkout_sku_matches=checkout.sku == product.sku,
                      checkout_quantity_matches=checkout.quantity == 1,
                      checkout_within_test_cap=checkout.total_cents <= 150000)
        if jobs:
            await asyncio.gather(*list(jobs), return_exceptions=True)
        record['stage'] = 'complete'
    except Exception as exc:
        record['error_type'] = type(exc).__name__
        # Keep the full private driver error locally; never export signed URLs.
        diagnostic = data/'diagnostics'/'api-readonly-error.txt'
        diagnostic.parent.mkdir(parents=True, exist_ok=True)
        diagnostic.write_text(str(exc), encoding='utf-8')
        record['diagnostic'] = str(diagnostic.relative_to(ROOT))
        # Only our own generic messages may be exported.
        if isinstance(exc, NeedsHuman):
            record['message'] = str(exc)
    finally:
        if jobs:
            for job in list(jobs): job.cancel()
            await asyncio.gather(*list(jobs), return_exceptions=True)
        await browser.close()
        target = data/'qa'/'jd-api-readonly-20261029.json'
        target.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(record, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
