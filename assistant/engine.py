from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timedelta

from .models import CHINA, NeedsHuman, NotReady, OrderResult, Status, TaskConfig


class Engine:
    def __init__(self, store, adapter_factory):
        self.store, self.adapter_factory = store, adapter_factory
        self.job = None
        self.task_id = None
        self.stop_event = asyncio.Event()
        self.subscribers: set[asyncio.Queue] = set()
        self.now = lambda: datetime.now(CHINA)
        self.monotonic = time.monotonic

    def emit(self, event):
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # The client reloads the durable event log when reconnecting.
                self.subscribers.discard(queue)

    def log(self, task_id, message, level="info"):
        self.emit({"type": "log", "event": self.store.event(task_id, message, level)})

    def state(self, task_id, status, message, **fields):
        task = self.store.update(task_id, status=status, message=message, **fields)
        self.emit({"type": "task", "task": task})
        self.log(task_id, message)
        return task

    @property
    def busy(self):
        return self.job is not None and not self.job.done()

    def start(self, task_id, resume=False):
        if self.busy:
            raise ValueError("已有任务运行，请先停止或等待完成；同一账号串行执行")
        task = self.store.get(task_id)
        if task is None:
            raise ValueError("任务不存在")
        if task["status"] == Status.SUCCEEDED:
            raise ValueError("此任务已创建订单，请直接付款")
        if task["submit_intent"] and not resume:
            raise ValueError("此任务曾尝试提交，只能通过恢复操作先核验订单")
        config = TaskConfig.model_validate(task["config"])
        if not task["submit_intent"] and self.now() >= config.purchase_end:
            raise ValueError("本场次执行窗口已结束，请修改时间并重新启用，不能自动跨日运行")
        if not task["submit_intent"] and self.store.unresolved_submission(config.product.sku, task_id):
            raise ValueError("该商品存在未核验的提交记录（可能已删除），请从已删除任务恢复后仅核验订单")
        self.task_id = task_id
        self.stop_event = asyncio.Event()
        self.state(task_id, Status.PREPARING, "正在检查京东会话、商品与预约资格")
        self.job = asyncio.create_task(self.run(task_id, config))

    async def delay(self, seconds):
        if self.stop_event.is_set():
            raise asyncio.CancelledError
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=max(0.001, seconds))
            raise asyncio.CancelledError
        except asyncio.TimeoutError:
            return

    async def wait_for_start(self, start, monotonic_end=None):
        # Re-read wall time after every wait. A clock correction must not allow
        # checkout/submission before the configured effective start.
        while self.now() < start:
            remaining = (start - self.now()).total_seconds()
            if monotonic_end is not None:
                remaining = min(remaining, monotonic_end - self.monotonic())
                if remaining <= 0:
                    return False
            await self.delay(min(1, remaining))
        return True

    async def run(self, task_id, config):
        adapter = self.adapter_factory()
        try:
            stored = self.store.get(task_id)
            if stored["submit_intent"]:
                self.state(task_id, Status.VERIFYING, "检测到历史提交记录，先核对订单，禁止自动重复提交")
                since = datetime.fromisoformat(stored["submitted_at"]) if stored.get("submitted_at") else config.scheduled_at
                result = await adapter.verify(config, stored["baseline"] or [], since=since)
                self.finish(task_id, result)
                return
            fresh = await adapter.prepare(config)
            if fresh.sale_start and abs((datetime.fromisoformat(fresh.sale_start) - config.scheduled_at).total_seconds()) > 2:
                raise NeedsHuman("商品最新开售时间与任务场次不一致，请更新任务后启动")
            config = config.model_copy(update={"product": fresh})
            self.store.update(task_id, config=config.model_dump(mode="json"))
            if fresh.reservation_required:
                self.state(task_id, Status.RESERVING, "商品需要预约，正在确认并办理预约")
                booking = await adapter.reserve(config)
            else:
                booking = "not_required"
                self.log(task_id, "商品无需预约，继续准备购物车")
            # Capture known order IDs before the time-critical checkout phase.
            baseline = await adapter.baseline(config)
            self.store.update(task_id, baseline=baseline)
            self.state(task_id, Status.CART, "正在将精确规格加入购物车，并确认数量为 1")
            await adapter.prepare_cart(config)
            message = "开抢提醒已设置，已在购物车等待；资格在结算时校验" if booking == "reminder_set" else "无需预约，已在购物车等待抢购时间" if booking == "not_required" else "预约已确认，已在购物车等待抢购时间"
            self.state(task_id, Status.SCHEDULED, message)
            self.log(task_id, f"实际抢购区间 {config.purchase_start.isoformat(timespec='milliseconds')} 至 {config.purchase_end.isoformat(timespec='milliseconds')}；提前 {config.advance_ms} 毫秒，持续 {config.window_seconds} 秒")
            await self.wait_for_start(config.purchase_start)
            until = config.purchase_end
            if fresh.sale_end:
                until = min(until, datetime.fromisoformat(fresh.sale_end))
            monotonic_end = self.monotonic() + max(0, (until - self.now()).total_seconds())
            self.state(task_id, Status.RUNNING, "已到实际抢购开始时间，等待目标商品解锁并核对结算")
            attempt = 0
            while self.now() < until and self.monotonic() < monotonic_end:
                if not await self.wait_for_start(config.purchase_start, monotonic_end):
                    break
                if self.stop_event.is_set():
                    raise asyncio.CancelledError
                attempt += 1
                try:
                    checkout = await adapter.checkout(config)
                    if checkout.sku != config.product.sku or checkout.quantity != config.quantity:
                        raise NeedsHuman("结算商品或数量不符")
                    if checkout.total_cents <= 0 or checkout.total_cents > config.max_total_cents:
                        raise NeedsHuman("结算金额超过上限或无法确认")
                    if self.stop_event.is_set():
                        raise asyncio.CancelledError
                    if self.now() < config.purchase_start:
                        raise NeedsHuman("电脑时间发生回调，尚未到实际抢购开始时间，未提交订单")
                    if self.now() >= until or self.monotonic() >= monotonic_end:
                        raise NeedsHuman("结算耗时超过本场次尝试窗口，未执行提交")
                    # Durable intent MUST be written before clicking. Even if
                    # the process dies at the next instruction, recovery only
                    # verifies and never blindly submits again.
                    self.state(task_id, Status.SUBMITTING, "商品、数量、地址和金额已核对，正在提交一次订单",
                               submit_intent=uuid.uuid4().hex, submitted_at=self.now().isoformat())
                    try:
                        await adapter.submit(config)
                    except Exception:
                        self.log(task_id, "提交响应不明确，先核验订单", "warning")
                    self.state(task_id, Status.PAYMENT, "正在确认已进入付款界面；未确认前不报告抢购成功")
                    try:
                        order_id = await adapter.enter_payment(config)
                        self.store.update(task_id, payment_entered_at=self.now().isoformat(), payment_order_id=order_id)
                        self.state(task_id, Status.LOCKING, "已进入付款界面，正在锁单；退出支付并保留订单")
                        try:
                            await adapter.lock_order(config)
                        except Exception:
                            self.log(task_id, "付款界面已确认，但退出支付状态需人工核对；继续查询订单号", "warning")
                    except Exception:
                        self.log(task_id, "未能确认付款界面，查询是否已创建订单；不会重复提交", "warning")
                    self.state(task_id, Status.VERIFYING, "正在查看订单号并核对商品、数量、金额和待付款状态")
                    result = await adapter.verify(config, baseline, since=datetime.fromisoformat(self.store.get(task_id)["submitted_at"]))
                    self.finish(task_id, result)
                    return
                except NotReady as exc:
                    self.log(task_id, f"第 {attempt} 次检查：{exc}")
                    await self.delay(min(5, 1.45 ** min(attempt - 1, 5), max(0.001, (until - self.now()).total_seconds()), max(0.001, monotonic_end - self.monotonic())))
            self.state(task_id, Status.FAILED, "本场次尝试窗口结束，未提交订单")
        except asyncio.CancelledError:
            status = Status.WAITING if self.store.get(task_id)["submit_intent"] else Status.STOPPED
            self.state(task_id, status, "已停止后续购买，请核对本次订单" if status == Status.WAITING else "任务已停止，未提交订单")
        except NeedsHuman as exc:
            shot = await adapter.screenshot(task_id)
            self.state(task_id, Status.WAITING, str(exc), screenshot=shot)
        except Exception:
            # Avoid putting URLs, account details or cookies from raw exception
            # strings into the event stream.
            shot = await adapter.screenshot(task_id)
            self.state(task_id, Status.WAITING, "页面操作或网络请求失败，请检查应用浏览器并恢复", screenshot=shot)

    def finish(self, task_id, result: OrderResult):
        stored = self.store.get(task_id)
        config = TaskConfig.model_validate(stored["config"])
        if stored.get("payment_entered_at"):
            result = result.model_copy(update={"payment_entered": True})
        if result.outcome == "created" and stored.get("payment_order_id") and result.order_id != stored["payment_order_id"]:
            result = OrderResult(outcome="unknown", message="付款界面订单号与订单列表不一致，请人工核对")
        if result.outcome == "created" and (not result.order_id or result.sku != config.product.sku
                or result.total_cents is None or not 0 < result.total_cents <= config.max_total_cents):
            result = OrderResult(outcome="unknown", message="缺少明确订单号、匹配商品或金额证据，请人工核对")
        if result.outcome == "created" and result.payment_entered:
            suffix = "；支付退出状态需人工核对" if result.payment_flow == "not_confirmed" else ""
            result = result.model_copy(update={"message": f"抢购成功，锁单成功！订单号：{result.order_id}，待付款 ¥{result.total_cents / 100:.2f}" + suffix})
            status = Status.SUCCEEDED
        elif result.outcome == "created":
            result = result.model_copy(update={"message": f"已创建订单 {result.order_id}，但未确认进入付款界面，请人工核对；不会再次提交"})
            status = Status.WAITING
        else:
            status = Status.WAITING if result.outcome == "unknown" else Status.FAILED
        self.state(task_id, status, result.message, result=result.model_dump())

    async def stop(self, task_id):
        if not self.busy or self.task_id != task_id:
            return self.store.get(task_id)
        self.stop_event.set()
        task = self.store.get(task_id)
        if task["submit_intent"]:
            self.log(task_id, "已停止后续购买动作；正在完成本次订单核验", "warning")
        else:
            self.job.cancel()
            try:
                await self.job
            except asyncio.CancelledError:
                pass
            if self.store.get(task_id)["status"] in {Status.PREPARING, Status.RESERVING, Status.CART, Status.SCHEDULED, Status.RUNNING}:
                self.state(task_id, Status.STOPPED, "任务已停止，未提交订单")
        return self.store.get(task_id)

    async def close(self):
        if self.busy:
            self.job.cancel()
            try:
                await self.job
            except asyncio.CancelledError:
                pass
