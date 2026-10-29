"""Unified per-task submission option; all network access is local/fake."""
import json
from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError
from playwright.async_api import async_playwright, expect

from assistant.models import CHINA, Status, TaskConfig
from assistant.store import Store
from tests.test_api import client_for
from tests.test_console import console_server


def test_api_requires_explicit_strict_flag_and_roundtrips(config, tmp_path):
    raw = config.model_dump(mode='json')
    raw.pop('use_api')
    raw['submit_mode'] = 'api'
    assert TaskConfig.model_validate(raw).use_api is False
    raw['use_api'] = True
    parsed = TaskConfig.model_validate(raw)
    assert parsed.submit_mode == 'api'
    assert TaskConfig.model_validate_json(parsed.model_dump_json()) == parsed
    for value in ('true', 1, None):
        with pytest.raises(ValidationError):
            TaskConfig.model_validate({**raw, 'use_api': value})
    store = Store(tmp_path/'options.db')
    task = store.create(parsed)
    store.db.close()
    reopened = Store(tmp_path/'options.db')
    assert reopened.get(task['id'])['config']['use_api'] is True
    assert reopened.get(task['id'])['status'] == Status.DRAFT
    reopened.db.close()


def test_per_task_api_changes_never_enable_other_tasks_or_change_existing_intent(tmp_path, config):
    app, client = client_for(tmp_path)
    raw = config.model_dump(mode='json')
    raw['scheduled_at'] = (datetime.now(CHINA)+timedelta(minutes=10)).isoformat()
    with client:
        first = client.post('/api/tasks', json={**raw, 'use_api': True}).json()
        second = client.post('/api/tasks', json={**raw, 'use_api': False}).json()
        changed = client.put(f"/api/tasks/{first['id']}", json={**raw, 'use_api': False})
        assert changed.status_code == 200
        assert not changed.json()['config']['use_api']
        assert not client.get(f"/api/tasks/{second['id']}").json()['config']['use_api']
        app.state.store.update(first['id'], submit_intent='fixture-only', status=Status.WAITING)
        assert client.put(f"/api/tasks/{first['id']}", json={**raw, 'use_api': True}).status_code == 409
        assert not app.state.engine.busy


@pytest.mark.parametrize('width', [1440, 320])
async def test_api_checkbox_default_off_save_edit_and_reload(console_server, config, width):
    app, origin = console_server
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={'width':width, 'height':900})
        errors = []
        page.on('pageerror', lambda err: errors.append(str(err)))
        await page.goto(origin)
        await page.get_by_label('控制台访问码').fill(app.state.access.code)
        await page.get_by_role('button', name='连接工作台').click()
        await page.get_by_role('button', name='创建抢购任务', exact=True).click()
        checkbox = page.get_by_label('调用 API 提交订单', exact=True)
        await expect(checkbox).not_to_be_checked()
        product = config.product.model_copy(update={'sale_start':(datetime.now(CHINA)+timedelta(minutes=10)).isoformat()})
        await page.route('**/api/products/resolve', lambda r:r.fulfill(body=product.model_dump_json(), content_type='application/json'))
        await page.get_by_label('商品链接 / 京东分享文案 / SKU').fill(config.product.sku)
        await page.get_by_role('button', name='解析', exact=True).click()
        await expect(page.locator('.resolved-product')).to_be_visible()
        await page.get_by_label('最高应付总额').fill('2699')
        await page.get_by_label('收货地址核对关键字').fill('测试小区')
        await checkbox.check()
        await expect(page.get_by_label('无需条件核对模式', exact=True)).not_to_be_checked()
        assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        await page.get_by_role('button', name='保存任务', exact=True).click()
        await expect(page.locator('.task-row')).to_contain_text('API 提交')
        task = app.state.store.tasks()[0]
        assert task['config']['use_api'] and task['status'] == Status.DRAFT
        assert not app.state.engine.busy
        await page.reload()
        await page.get_by_role('button', name=f'查看任务 {config.product.sku}', exact=True).click()
        await expect(page.locator('.detail-grid')).to_contain_text('API 提交')
        await page.get_by_role('button', name='修改配置', exact=True).click()
        await expect(checkbox).to_be_checked()
        await checkbox.uncheck()
        await page.get_by_role('button', name='保存任务', exact=True).click()
        await expect(page.locator('.task-row')).to_contain_text('页面提交')
        assert not app.state.store.get(task['id'])['config']['use_api']
        assert not errors and not app.state.engine.busy
        await browser.close()
