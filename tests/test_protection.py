from types import SimpleNamespace

import pytest

from assistant.browser import Browser
from assistant.protection import AccessLimited, AccessProtection, restriction_reason


@pytest.mark.parametrize('text', ['当前页面异常 请刷新或切换账号试试', '请完成安全验证', '操作过于频繁'])
def test_access_prompts_are_detected(text):
    assert restriction_reason('https://item.jd.com/100327520619.html', text)


def test_unknown_price_and_unsold_are_not_access_restrictions():
    assert restriction_reason('https://item.jd.com/100327520619.html', '待发布 尚未开售 暂时无货') is None


def test_pause_survives_restart_and_does_not_extend_on_checks(tmp_path):
    protection = AccessProtection(tmp_path)
    protection.now = lambda: 1000
    protection.pause('页面异常')
    restarted = AccessProtection(tmp_path)
    restarted.now = lambda: 1100
    for _ in range(3):
        with pytest.raises(AccessLimited): restarted.check()
    assert restarted.state()['remaining_seconds'] == 200
    restarted.now = lambda: 1301
    restarted.check()
    assert not restarted.state()['paused']


@pytest.mark.parametrize('status,nav,expected', [(403,True,True),(429,True,True),(403,False,False),(200,True,False)])
def test_only_denied_navigation_responses_pause(tmp_path, status, nav, expected):
    browser = Browser(tmp_path)
    response = SimpleNamespace(status=status,url='https://item.jd.com/product.html',
        request=SimpleNamespace(is_navigation_request=lambda:nav),frame=SimpleNamespace(parent_frame=None))
    browser._access_response(response)
    assert browser.protection.state()['paused'] == expected


async def test_paused_browser_status_is_local_and_does_not_launch(tmp_path):
    browser = Browser(tmp_path)
    browser.protection.pause('页面异常')
    status = await browser.status()
    assert status['access_paused'] and not status['logged_in']
    assert browser.context is None and browser.playwright is None


async def test_status_detects_post_login_anomaly_without_network(tmp_path):
    class Body:
        async def inner_text(self, **kwargs):
            return '当前页面异常 请刷新或切换账号试试'
    class Context:
        pages = [SimpleNamespace(url='https://www.jd.com/', is_closed=lambda:False, locator=lambda _:Body())]
        async def cookies(self, *args):
            raise AssertionError('Cookie success must not mask an abnormal page')
    browser = Browser(tmp_path)
    browser.context = Context()
    status = await browser.status()
    assert status['access_paused'] and not status['logged_in']
    assert browser.protection.state()['paused']
