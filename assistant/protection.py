"""Pause JD automation on visible access restrictions, without hiding it."""
from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from .models import NeedsHuman


class AccessLimited(NeedsHuman):
    pass


def restriction_reason(url: str, text: str) -> str | None:
    path = urlparse(url).path.lower()
    if any(part in path for part in ('risk_handler', 'captcha', 'pc-frequent', '/common/limit.html')):
        return '京东要求人工验证或限制访问'
    if re.search(r'滑动验证|安全验证|请完成验证|访问过于频繁|操作过于频繁|拖动滑块', text):
        return '京东要求人工验证或限制访问'
    if '当前页面异常' in text and re.search(r'请刷新|切换账号', text):
        return '京东显示当前页面异常，具体原因尚未确认'
    return None


class AccessProtection:
    def __init__(self, data: Path):
        self.path = data / 'jd-access-pause.json'
        self.now = time.time

    def state(self):
        if not self.path.exists():
            return {'paused': False, 'remaining_seconds': 0}
        try:
            saved = json.loads(self.path.read_text(encoding='utf-8'))
            remaining = max(0, math.ceil(saved['until'] - self.now()))
            return {'paused': remaining > 0, 'remaining_seconds': remaining, 'reason': saved['reason']}
        except (ValueError, KeyError, OSError, TypeError):
            raise AccessLimited('访问保护记录无法读取，请检查本地 jd-access-pause.json；已停止自动访问') from None

    def pause(self, reason: str, seconds: int = 300):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Persist only a generic reason and a deadline; no URLs, cookies or IDs.
        self.path.write_text(json.dumps({'until': self.now()+max(300, min(3600, seconds)), 'reason': reason}, ensure_ascii=False), encoding='utf-8')

    def check(self):
        state = self.state()
        if state['paused']:
            raise AccessLimited(f"{state['reason']}；自动访问已暂停，还需等待约 {state['remaining_seconds']} 秒。请人工处理，之后手动恢复；不要连续刷新或反复登录")


def check_access(browser):
    protection = getattr(browser, 'protection', None)
    if protection is not None:
        protection.check()


async def check_page_access(browser, page):
    text = await page.locator('body').inner_text()
    reason = restriction_reason(page.url, text)
    if reason:
        protection = getattr(browser, 'protection', None)
        if protection is not None:
            protection.pause(reason)
            protection.check()
        raise AccessLimited(reason+'；已停止自动访问，请人工处理后恢复')
    check_access(browser)
    if (urlparse(page.url).hostname or '') in {'passport.jd.com', 'plogin.m.jd.com'}:
        raise NeedsHuman('京东登录已失效，请在应用独立浏览器中人工登录后恢复；不再自动重试')
    return text
