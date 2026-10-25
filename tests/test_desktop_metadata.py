from datetime import datetime

import pytest

from assistant.adapters.jd import desktop_time, parse_product, reservation_state
from assistant.models import CHINA


@pytest.mark.parametrize("text,expected",[
    ("预约 明天09:55截止预约 明天10:00开始抢购","2026-10-26T10:00:00+08:00"),
    ("今天10:00:15开始抢购","2026-10-25T10:00:15+08:00"),
    ("2026-11-01 10:00开始抢购","2026-11-01T10:00:00+08:00"),
    ("11月1日10:00开始抢购","2026-11-01T10:00:00+08:00"),
    ("预约尚未开始",None),
])
def test_desktop_dates_use_explicit_china_day(text,expected):
    assert desktop_time(text,"开始抢购",datetime(2026,10,25,15,0,tzinfo=CHINA)) == expected


@pytest.mark.parametrize("start,end",[("window._itemOnly = (", ");"),("window._itemOnly = ", ";"),("_itemOnly = (", ")\n")])
def test_initial_data_accepts_optional_parentheses_and_semicolon(start,end):
    html=start+'{"item":{"skuId":"100327520619","skuName":"目标商品"}}'+end
    assert parse_product(html,"100327520619").sku == "100327520619"


@pytest.mark.parametrize("text,expected",[
    ("已设置开抢提醒\n明天10:00开抢","reminder_set"),
    ("已预约抢购资格 明天10:00开抢","reserved"),
    ("立即预约",None),
])
def test_reservation_and_reminder_are_distinct_states(text,expected):
    assert reservation_state(text)==expected
