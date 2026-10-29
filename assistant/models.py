from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator

CHINA = ZoneInfo("Asia/Shanghai")


def now_iso() -> str:
    return datetime.now(CHINA).isoformat()


class Status(StrEnum):
    DRAFT = "draft"
    PREPARING = "preparing"
    RESERVING = "reserving"
    CART = "preparing_cart"
    SCHEDULED = "scheduled"
    RUNNING = "running"
    WAITING = "waiting_human"
    SUBMITTING = "submitting"
    PAYMENT = "entering_payment"
    LOCKING = "locking"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STOPPED = "stopped"
    INTERRUPTED = "interrupted"


ACTIVE = {Status.PREPARING, Status.RESERVING, Status.CART, Status.SCHEDULED, Status.RUNNING,
          Status.SUBMITTING, Status.PAYMENT, Status.LOCKING, Status.VERIFYING}


class Product(BaseModel):
    platform: Literal["jd"] = "jd"
    sku: str = Field(pattern=r"^\d{8,16}$")
    title: str
    spec: str = ""
    url: str
    reservation_start: str | None = None
    reservation_end: str | None = None
    sale_start: str | None = None
    sale_end: str | None = None
    reservation_required: bool = False
    checked_at: str = Field(default_factory=now_iso)


class TaskConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product: Product
    quantity: int = Field(default=1, ge=1, le=1)
    submit_mode: Literal['dom', 'api'] = 'dom'
    scheduled_at: datetime
    advance_ms: int = Field(default=0, ge=0, le=86_400_000, strict=True)
    max_total_cents: int = Field(gt=0, le=100_000_000)
    address_hint: str = Field(min_length=2, max_length=120)
    window_seconds: int = Field(default=60, ge=1, le=86400, strict=True)

    @property
    def purchase_start(self) -> datetime:
        return self.scheduled_at - timedelta(milliseconds=self.advance_ms)

    @property
    def purchase_end(self) -> datetime:
        return self.purchase_start + timedelta(seconds=self.window_seconds)

    @field_validator("scheduled_at")
    @classmethod
    def china_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("开售时间必须带时区")
        return value.astimezone(CHINA)

    @field_validator("address_hint")
    @classmethod
    def nonempty_address(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 2:
            raise ValueError("请填写用于核对的收货地址关键字")
        return value


class OrderResult(BaseModel):
    outcome: Literal["created", "unknown", "failed"]
    message: str
    order_id: str | None = None
    sku: str | None = None
    total_cents: int | None = None
    url: str | None = None
    payment_flow: Literal["not_attempted", "entered", "entered_and_exited", "not_confirmed"] = "not_attempted"
    payment_entered: bool = False


class Checkout(BaseModel):
    sku: str
    quantity: int
    total_cents: int
    address: str


class NeedsHuman(Exception):
    """A recoverable condition; submitting again requires explicit resumption."""


class NotReady(Exception):
    """The target product is not yet selectable."""
