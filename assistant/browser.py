from __future__ import annotations

import asyncio
import os
import json
from pathlib import Path
from urllib.parse import urlparse

from .protection import AccessProtection, restriction_reason
from .browser_runtime import bundled_executable, session_settings, connection_setting
from .native_browser import NativeBrowser


class Browser:
    def __init__(self, data: Path):
        self.data = data
        self.context = None
        self.playwright = None
        self.page = None
        self.lock = asyncio.Lock()
        self.protection = AccessProtection(data)
        self.network_mode = 'system'
        self.native = None

    async def ensure(self):
        async with self.lock:
            if self.context is None:
                from playwright.async_api import async_playwright
                self.playwright = await async_playwright().start()
                try:
                    executable = bundled_executable(Path(os.environ.get('PLAYWRIGHT_BROWSERS_PATH', self.data/'browsers')))
                    profile, self.network_mode = session_settings(self.data)
                    connection_mode = connection_setting(self.data)
                    if connection_mode == 'native_cdp':
                        self.native = NativeBrowser()
                        self.context = await self.native.start(self.playwright.chromium,
                            executable or self.playwright.chromium.executable_path, profile, direct=self.network_mode == 'direct')
                    elif connection_mode == 'persistent':
                        self.context = await self.playwright.chromium.launch_persistent_context(
                            str(profile), executable_path=str(executable) if executable is not None else None,
                            args=['--no-proxy-server'] if self.network_mode == 'direct' else [],
                            headless=False, viewport={"width": 1440, "height": 960},
                            locale="zh-CN", timezone_id="Asia/Shanghai")
                    else:
                        raise ValueError('Unknown browser connection mode')
                    self.context.set_default_timeout(7000)
                    self.context.on('response', self._access_response)
                except Exception:
                    if self.native is not None:
                        await self.native.close()
                        self.native = None
                    await self.playwright.stop()
                    self.playwright = None
                    raise RuntimeError("浏览器启动失败，请运行 install.ps1 安装 Chromium；如已打开应用浏览器，请先关闭重复实例") from None
            return self.context

    def _access_response(self, response):
        host = urlparse(response.url).hostname or ''
        if (response.status in (403, 429) and (host == 'jd.com' or host.endswith('.jd.com'))
                and response.request.is_navigation_request() and response.frame.parent_frame is None):
            self.protection.pause(f'京东页面返回 HTTP {response.status}，自动访问已停止')

    async def task_page(self):
        context = await self.ensure()
        if self.page is None or self.page.is_closed() or getattr(self.page, '_api_submit_guarded', False):
            self.page = await context.new_page()
        return self.page

    async def login(self, open_only=False):
        context = await self.ensure()
        if open_only or self.protection.state()['paused']:
            # Showing the current window is local; do not request login again.
            page = self.page if self.page is not None and not self.page.is_closed() else context.pages[-1] if context.pages else await context.new_page()
            await page.bring_to_front()
            return
        page = await context.new_page()
        await page.goto("https://passport.jd.com/new/login.aspx", wait_until="domcontentloaded")
        await page.bring_to_front()

    async def status(self):
        # Local DOM inspection only. A valid cookie must not hide an error
        # that appeared after the user finished login in the same window.
        if self.context is not None and not self.protection.state()['paused']:
            pages = self.context.pages
            if pages:
                page = pages[-1]
                host = urlparse(page.url).hostname or ''
                if not page.is_closed() and (host == 'jd.com' or host.endswith('.jd.com')):
                    try:
                        text = await page.locator('body').inner_text(timeout=500)
                        reason = restriction_reason(page.url, text)
                    except Exception:
                        reason = None
                    if reason:
                        self.protection.pause(reason)
        protection = self.protection.state()
        if protection['paused']:
            return {'browser_open': self.context is not None, 'logged_in': False,
                    'access_paused': True,
                    'browser_network': self.network_mode,
                    'message': f"自动访问已暂停，剩余约 {protection['remaining_seconds']} 秒。请人工处理异常；等待结束后手动恢复任务"}
        if self.context is None:
            return {"browser_open": False, "logged_in": False, "message": "请打开京东登录浏览器", 'browser_network': self.network_mode}
        try:
            cookies = await self.context.cookies(["https://www.jd.com"])
            names = {c["name"] for c in cookies if c.get("value")}
            logged_in = "thor" in names and bool(names & {"pin", "unick", "_tp"})
            return {"browser_open": True, "logged_in": logged_in,
                    'browser_network': self.network_mode,
                    "message": "已检测到登录会话，任务开始时再次校验" if logged_in else "等待在电脑浏览器中完成京东登录"}
        except Exception:
            await self.close()
            return {"browser_open": False, "logged_in": False, "message": "浏览器已关闭，请重新打开"}

    async def close(self):
        if self.native is not None:
            await self.native.close()
            self.native = None
        elif self.context is not None:
            try:
                await self.context.close()
            except Exception:
                pass
        if self.playwright is not None:
            await self.playwright.stop()
        self.context = self.playwright = self.page = None
