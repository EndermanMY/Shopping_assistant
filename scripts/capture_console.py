"""Capture this local console for QA without changing any purchase task."""
import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright

ROOT=Path(__file__).resolve().parent.parent


async def main():
    code=json.loads((ROOT/'data/access.json').read_text(encoding='utf-8'))['code']
    async with async_playwright() as playwright:
        browser=await playwright.chromium.launch()
        page=await browser.new_page(viewport={"width":1440,"height":1000})
        await page.goto('http://127.0.0.1:18765/')
        await page.get_by_label('控制台访问码').fill(code)
        await page.get_by_role('button',name='连接工作台').click()
        await page.get_by_role('heading',name='抢购工作台.').wait_for()
        await page.locator('.connection').filter(has_text='实时连接').wait_for()
        folder=ROOT/'data/qa'
        folder.mkdir(parents=True,exist_ok=True)
        await page.screenshot(path=str(folder/'console-current.png'))
        await page.set_viewport_size({"width":375,"height":812})
        assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        await page.screenshot(path=str(folder/'console-mobile-current.png'),full_page=True)
        await page.request.post('http://127.0.0.1:18765/api/auth/logout')
        await browser.close()


if __name__=='__main__':
    asyncio.run(main())
