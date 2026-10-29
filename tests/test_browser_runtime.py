import json
from pathlib import Path

import pytest

from assistant.browser_runtime import bundled_executable


def test_no_runtime_manifest_keeps_playwright_bundle(tmp_path):
    assert bundled_executable(tmp_path) is None


def test_pinned_browser_stays_inside_project_bundle(tmp_path):
    binary = tmp_path/'chrome-stable-test'/'chrome-win64'/'chrome.exe'
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b'test only')
    (tmp_path/'runtime.json').write_text(json.dumps({'relative_path':'chrome-stable-test/chrome-win64/chrome.exe'}))
    assert bundled_executable(tmp_path) == binary.resolve()


@pytest.mark.parametrize('relative', ['../chrome.exe','C:/Program Files/Google/Chrome/Application/chrome.exe','https://example.com/chrome.exe','missing/chrome.exe'])
def test_runtime_never_uses_external_or_missing_browser(tmp_path, relative):
    (tmp_path/'runtime.json').write_text(json.dumps({'relative_path':relative}))
    with pytest.raises(ValueError): bundled_executable(tmp_path)


async def test_open_only_shows_independent_window_without_navigation(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from assistant.browser import Browser
    page = SimpleNamespace(is_closed=lambda:False, bring_to_front=AsyncMock(), goto=AsyncMock())
    browser = Browser(tmp_path)
    browser.ensure = AsyncMock(return_value=SimpleNamespace(pages=[page]))
    await browser.login(open_only=True)
    page.bring_to_front.assert_awaited_once()
    page.goto.assert_not_awaited()


@pytest.mark.parametrize('mode,args', [('system',[]),('direct',['--no-proxy-server'])])
def test_session_network_is_explicit_local_setting(tmp_path,mode,args):
    from assistant.browser_runtime import session_settings
    (tmp_path/'browser-network.json').write_text(json.dumps({'mode':mode,'profile_name':'diagnostic-direct-test'}))
    profile, selected = session_settings(tmp_path)
    assert selected == mode and profile == (tmp_path/'browser'/'diagnostic-direct-test').resolve()


def test_without_network_setting_system_proxy_is_preserved(tmp_path):
    from assistant.browser_runtime import session_settings
    profile, mode = session_settings(tmp_path)
    assert mode == 'system' and profile.name == 'jd'


@pytest.mark.parametrize('config', [{'mode':'proxy-bypass-all'},{'profile_name':'../Chrome'},{'profile_name':'C:/Chrome'}])
def test_session_setting_cannot_use_personal_browser_profile(tmp_path,config):
    from assistant.browser_runtime import session_settings
    (tmp_path/'browser-network.json').write_text(json.dumps(config))
    with pytest.raises(ValueError):session_settings(tmp_path)


def test_clean_release_defaults_to_owned_native_cdp_without_proxy_bypass(tmp_path):
    from assistant.browser_runtime import connection_setting, session_settings
    assert connection_setting(tmp_path) == 'native_cdp'
    assert session_settings(tmp_path)[1] == 'system'
