from __future__ import annotations

import asyncio
import json
import os
import socket
import ipaddress
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .adapters.jd import JDAdapter, inspect_product
from .browser import Browser
from .engine import Engine
from .models import ACTIVE, CHINA, NeedsHuman, Status, TaskConfig
from .security import Access
from .store import Store

ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", ROOT))


class Login(BaseModel):
    code: str = Field(max_length=200)


class Source(BaseModel):
    source: str = Field(min_length=1, max_length=2000)


class ResetTask(BaseModel):
    confirm_no_order: bool = Field(default=False, strict=True)


def lan_ips():
    ips = set()
    try:
        for entry in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = entry[4][0]
            if ipaddress.ip_address(ip).is_private and not ipaddress.ip_address(ip).is_loopback:
                ips.add(ip)
    except OSError:
        pass
    return sorted(ips)


def create_app(data: Path | None = None, adapter_factory=None):
    data = data or Path(os.environ.get("SHOPPING_DATA_DIR", ROOT / "data-api"))
    data.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / "data" / "browsers"))
    store, access, browser = Store(data / "assistant.db"), Access(data), Browser(data)
    engine = Engine(store, adapter_factory or (lambda: JDAdapter(browser, data)))

    @asynccontextmanager
    async def lifespan(app):
        yield
        await engine.close()
        await browser.close()
        store.close()

    app = FastAPI(title="购物小助手", version="0.2.0-api.2", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.access = store, access
    app.state.browser, app.state.engine = browser, engine

    @app.middleware("http")
    async def security(request: Request, call_next):
        host = request.url.hostname or ""
        allowed_hosts = {"localhost", "127.0.0.1", "::1", socket.gethostname().lower(), *lan_ips()}
        if host.lower() not in allowed_hosts:
            return JSONResponse({"detail": "不支持的控制台地址，请使用启动终端显示的本机或局域网地址"}, status_code=400)
        if request.url.path.startswith("/api/") and request.url.path not in {"/api/auth/login", "/api/health"}:
            if not access.valid(request.cookies.get("shopping_session")):
                return JSONResponse({"detail": "请先输入控制台访问码"}, status_code=401)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and origin != f"{request.url.scheme}://{request.headers.get('host')}":
                return JSONResponse({"detail": "拒绝跨站请求"}, status_code=403)
        result = await call_next(request)
        result.headers["X-Content-Type-Options"] = "nosniff"
        result.headers["X-Frame-Options"] = "DENY"
        result.headers["Referrer-Policy"] = "same-origin"
        if request.url.path.startswith("/api/"):
            result.headers["Cache-Control"] = "no-store"
        return result

    @app.get("/api/health")
    async def health():
        return {"ok": True, "version": "0.2.0-api.2"}

    @app.post("/api/auth/login")
    async def login(body: Login, request: Request, response: Response):
        token = access.login(body.code, request.client.host if request.client else "unknown")
        if not token:
            raise HTTPException(401, "访问码不正确或尝试次数过多，请稍后再试")
        response.set_cookie("shopping_session", token, httponly=True, samesite="strict", max_age=86400)
        return {"ok": True}

    @app.post("/api/auth/logout")
    async def logout(request: Request, response: Response):
        access.sessions.pop(request.cookies.get("shopping_session"), None)
        response.delete_cookie("shopping_session")
        return {"ok": True}

    @app.get("/api/overview")
    async def overview():
        port = int(os.environ.get("SHOPPING_PORT", "18766"))
        return {"tasks": store.tasks(), "events": store.events(), "account": await browser.status(),
                "lan_urls": [f"http://{ip}:{port}" for ip in lan_ips()],
                "busy_task": engine.task_id if engine.busy else None,
                "server_time": datetime.now(CHINA).isoformat()}

    @app.get("/api/account/status")
    async def account():
        return await browser.status()

    @app.post("/api/account/login")
    async def account_login(open_only: bool = False):
        if engine.busy:
            raise HTTPException(409, "请先停止运行任务，再打开登录浏览器")
        try:
            await browser.login(open_only=open_only)
        except Exception:
            raise HTTPException(503, "登录浏览器启动失败，请检查 Chromium 安装和应用日志") from None
        paused = browser.protection.state()['paused']
        return {"ok": True, "message": "已显示应用独立浏览器窗口，未请求京东页面" if open_only else "已显示应用浏览器；访问暂停期间不再请求登录，请人工处理异常" if paused else "已在电脑打开京东登录浏览器"}

    @app.post("/api/products/resolve")
    async def resolve(body: Source):
        if engine.busy:
            raise HTTPException(409, "任务运行时暂不打开额外商品页面，请稍后解析")
        try:
            return await inspect_product(body.source, browser)
        except (ValueError, NeedsHuman) as exc:
            raise HTTPException(422, str(exc)) from None
        except Exception:
            raise HTTPException(502, "京东商品读取失败，请检查网络或稍后重试") from None

    @app.post("/api/products/preflight")
    async def preflight(body: Source):
        if engine.busy:
            raise HTTPException(409, "任务运行时不能检查其他页面")
        adapter = JDAdapter(browser, data)
        try:
            product = await inspect_product(body.source, browser)
            result = await adapter.probe(product)
            (data / "preflight.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            return result
        except NeedsHuman as exc:
            raise HTTPException(422, str(exc)) from None
        except Exception as exc:
            stage = getattr(adapter, "probe_step", "读取商品信息")
            raise HTTPException(502, f"{stage}未完成（{type(exc).__name__}），请检查应用浏览器中的提示，可能需要校准页面") from None

    @app.post("/api/account/close")
    async def close_browser():
        if engine.busy:
            raise HTTPException(409, "请先停止运行任务")
        await browser.close()
        return {"ok": True}

    @app.get("/api/tasks")
    async def tasks(deleted: bool = False):
        return store.tasks(deleted=deleted)

    def future_config(config):
        if config.purchase_start <= datetime.now(CHINA):
            raise HTTPException(422, "实际抢购开始时间已过去，请调整开始时间或提前量后保存")

    @app.post("/api/tasks", status_code=201)
    async def create(config: TaskConfig):
        future_config(config)
        if not config.product.sku.isdigit() or not 8 <= len(config.product.sku) <= 16:
            raise HTTPException(422, "无效京东 SKU")
        task = store.create(config)
        engine.emit({"type": "task", "task": task})
        return task

    def get_task(task_id):
        task = store.get(task_id)
        if task is None:
            raise HTTPException(404, "任务不存在")
        return task

    def editable(task_id):
        task = get_task(task_id)
        if (engine.busy and engine.task_id == task_id) or task["status"] in ACTIVE:
            raise HTTPException(409, "请先停止任务")
        if task["submit_intent"]:
            raise HTTPException(409, "任务有提交记录，需保留原任务核验订单；新场次请新建任务")
        return task

    @app.get("/api/tasks/{task_id}")
    async def task(task_id: str):
        return get_task(task_id)

    @app.put("/api/tasks/{task_id}")
    async def update(task_id: str, config: TaskConfig):
        editable(task_id)
        future_config(config)
        updated = store.update(task_id, config=config.model_dump(mode="json"), status=Status.DRAFT,
                               message="任务已更新，请重新启动", result=None)
        engine.emit({"type": "task", "task": updated})
        return updated

    @app.delete("/api/tasks/{task_id}")
    async def remove(task_id: str):
        get_task(task_id)
        if engine.busy and engine.task_id == task_id:
            await engine.stop(task_id)
            if engine.busy and engine.task_id == task_id:
                try:
                    await asyncio.wait_for(asyncio.shield(engine.job), timeout=30)
                except asyncio.TimeoutError:
                    raise HTTPException(409, "已停止后续购买，正在核验已有提交，请稍后再删除") from None
        if engine.busy and engine.task_id == task_id:
            raise HTTPException(409, "任务状态发生变化，请先停止后再删除")
        store.delete(task_id)
        engine.emit({"type": "deleted", "task_id": task_id})
        return {"ok": True}

    @app.post("/api/tasks/{task_id}/restore")
    async def restore(task_id: str):
        if store.get(task_id, include_deleted=True) is None:
            raise HTTPException(404, "任务不存在")
        task = store.restore(task_id)
        engine.emit({"type": "task", "task": task})
        return task

    @app.post("/api/tasks/{task_id}/start")
    async def start(task_id: str):
        get_task(task_id)
        try:
            browser.protection.check()
            engine.start(task_id)
        except (ValueError, NeedsHuman) as exc:
            raise HTTPException(409, str(exc)) from None
        return store.get(task_id)

    @app.post("/api/tasks/{task_id}/resume")
    async def resume(task_id: str):
        get_task(task_id)
        try:
            browser.protection.check()
            engine.start(task_id, resume=True)
        except (ValueError, NeedsHuman) as exc:
            raise HTTPException(409, str(exc)) from None
        return store.get(task_id)

    @app.post("/api/tasks/{task_id}/reset")
    async def reset_task(task_id: str, body: ResetTask):
        get_task(task_id)
        if not body.confirm_no_order:
            raise HTTPException(422, "请先检查京东订单列表，确认本次没有生成订单")
        try:
            return await engine.reset(task_id)
        except (ValueError, NeedsHuman) as exc:
            raise HTTPException(409, str(exc)) from None
        except Exception:
            raise HTTPException(502, "订单列表检查未完成，未重置；请检查京东登录和网络后重试") from None

    @app.post("/api/tasks/{task_id}/stop")
    async def stop(task_id: str):
        get_task(task_id)
        return await engine.stop(task_id)

    @app.get("/api/events")
    async def events(task_id: str | None = None, after: int = 0):
        return store.events(task_id, after)

    @app.get("/api/tasks/{task_id}/screenshot")
    async def screenshot(task_id: str):
        task = get_task(task_id)
        if not task["screenshot"]:
            raise HTTPException(404, "暂无截图")
        path = data / "screenshots" / f"{task_id}.png"
        if not path.exists():
            raise HTTPException(404, "截图不存在")
        return FileResponse(path, headers={"Cache-Control": "no-store"})

    @app.websocket("/ws")
    async def websocket(ws: WebSocket):
        origin, host = ws.headers.get("origin"), ws.headers.get("host")
        if not access.valid(ws.cookies.get("shopping_session")) or origin != f"{'https' if ws.url.scheme == 'wss' else 'http'}://{host}":
            await ws.close(code=1008)
            return
        await ws.accept()
        queue = asyncio.Queue(maxsize=300)
        engine.subscribers.add(queue)
        try:
            while access.valid(ws.cookies.get("shopping_session")):
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    event = {"type": "heartbeat", "time": datetime.now(CHINA).isoformat()}
                await ws.send_json(event)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            engine.subscribers.discard(queue)

    dist = BUNDLE_ROOT / "frontend" / "dist"
    if dist.exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="console")
    else:
        @app.get("/")
        async def unbuilt():
            return JSONResponse({"message": "请先运行 install.ps1 构建控制台"}, status_code=503)
    return app
