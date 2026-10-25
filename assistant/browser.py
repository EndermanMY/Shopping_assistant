from __future__ import annotations

import asyncio
from pathlib import Path


class Browser:
    def __init__(self, data: Path):
        self.data = data
        self.context = None
        self.playwright = None
        self.page = None
        self.lock = asyncio.Lock()

    async def ensure(self):
        async with self.lock:
            if self.context is None:
                from playwright.async_api import async_playwright
                self.playwright = await async_playwright().start()
                try:
                    self.context = await self.playwright.chromium.launch_persistent_context(
                        str(self.data / "browser" / "jd"),
                        headless=False, viewport={"width": 1440, "height": 960},
                        locale="zh-CN", timezone_id="Asia/Shanghai",
                    )
                    self.context.set_default_timeout(7000)
                except Exception:
                    await self.playwright.stop()
                    self.playwright = None
                    raise RuntimeError("浏览器启动失败，请运行 install.ps1 安装 Chromium；如已打开应用浏览器，请先关闭重复实例") from None
            return self.context

    async def task_page(self):
        context = await self.ensure()
        if self.page is None or self.page.is_closed():
            self.page = await context.new_page()
        return self.page

    async def login(self):
        context = await self.ensure()
        page = await context.new_page()
        await page.goto("https://passport.jd.com/new/login.aspx", wait_until="domcontentloaded")
        await page.bring_to_front()

    async def status(self):
        if self.context is None:
            return {"browser_open": False, "logged_in": False, "message": "请打开京东登录浏览器"}
        try:
            cookies = await self.context.cookies(["https://www.jd.com"])
            names = {c["name"] for c in cookies if c.get("value")}
            logged_in = "thor" in names and bool(names & {"pin", "unick", "_tp"})
            return {"browser_open": True, "logged_in": logged_in,
                    "message": "已检测到登录会话，任务开始时再次校验" if logged_in else "等待在电脑浏览器中完成京东登录"}
        except Exception:
            await self.close()
            return {"browser_open": False, "logged_in": False, "message": "浏览器已关闭，请重新打开"}

    async def close(self):
        if self.context is not None:
            try:
                await self.context.close()
            except Exception:
                pass
        if self.playwright is not None:
            await self.playwright.stop()
        self.context = self.playwright = self.page = None
