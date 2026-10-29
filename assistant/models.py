from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

CHINA = ZoneInfo("Asia/Shanghai")


def now_iso() -> str:
    return datetime.now(CHINA).isoformat()


class Status(StrEnum):
    DRAFT = "draft"
    PREPARING = "preparing"
    RESERVING = "reserving"
    CART = "preparing_cart"
    PRECHECKING = "preparing_checkout"
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


ACTIVE = {Status.PREPARING, Status.RESERVING, Status.CART, Status.PRECHECKING, Status.SCHEDULED, Status.RUNNING,
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
    use_api: bool = Field(default=False, strict=True)
    scheduled_at: datetime
    advance_ms: int = Field(default=0, ge=0, le=86_400_000, strict=True)
    max_total_cents: int | None = Field(default=None, gt=0, le=100_000_000)
    ignore_price: bool = Field(default=False, strict=True)
    skip_checks: bool = Field(default=False, strict=True)
    address_hint: str = Field(default='', max_length=120)
    window_seconds: int = Field(default=60, ge=1, le=86400, strict=True)

    @model_validator(mode='after')
    def require_budget_unless_ignored(self):
        if not self.skip_checks and not self.ignore_price and self.max_total_cents is None:
            raise ValueError('未开启无视价格下单时，必须填写最高应付总额')
        if not self.skip_checks and len(self.address_hint) < 2:
            raise ValueError('请填写用于核对的收货地址关键字')
        return self

    @model_validator(mode='before')
    @classmethod
    def migrate_legacy_api_mode(cls, value):
        if isinstance(value, dict):
            value = dict(value)
            legacy = value.pop('submit_mode', 'dom')
            if legacy not in ('dom','api'):
                raise ValueError('提交方式必须是 dom 或 api')
        # Old API branch settings alone never activate the new option.
        return value

    @computed_field
    @property
    def submit_mode(self) -> Literal['dom','api']:
        return 'api' if self.use_api else 'dom'

    def price_allowed(self, total: int | None) -> bool:
        return self.skip_checks or self.ignore_price or (total is not None and self.max_total_cents is not None
                                     and 0 < total <= self.max_total_cents)

    @property
    def purchase_start(self) -> datetime:
        return self.scheduled_at - timedelta(milliseconds=self.advance_ms)

    @property
    def purchase_end(self) -> datetime:
        return self.purchase_start + timedelta(seconds=self.window_seconds)

    @property
    def checkout_prepare_at(self) -> datetime:
        return self.purchase_start - timedelta(seconds=20)

    @property
    def checkout_ready_by(self) -> datetime:
        return self.purchase_start - timedelta(seconds=10)

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
    total_cents: int | None
    address: str


class NeedsHuman(Exception):
    """A recoverable condition; submitting again requires explicit resumption."""


class NotReady(Exception):
    """The target product is not yet selectable."""
