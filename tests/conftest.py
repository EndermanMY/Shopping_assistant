import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(Path(__file__).resolve().parent.parent / "data" / "browsers"))

from assistant.models import CHINA, Product, TaskConfig
from assistant.store import Store


@pytest.fixture
def config():
    return TaskConfig(
        product=Product(sku="100327520619", title="雷鸟 GT Max bilibili 限定版 M码", spec="雷鸟GT Max bilibili 限定版 / M码",
                        url="https://item.jd.com/100327520619.html"),
        scheduled_at=datetime.now(CHINA) - timedelta(milliseconds=100),
        max_total_cents=269900, address_hint="测试小区", window_seconds=5,
    )


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / "test.db")
    yield result
    result.close()
