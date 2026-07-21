"""A 股观察时段与交易日判断。"""

from datetime import datetime
from zoneinfo import ZoneInfo

from tradingagents.watchlist.calendar import (
    OBSERVE_SLOTS,
    effective_trade_date_for_market,
    is_cn_trading_day,
    slot_key_for,
    us_today,
)


def test_weekend_not_trading_day():
    assert is_cn_trading_day(datetime(2026, 7, 11)) is False  # Saturday
    assert is_cn_trading_day(datetime(2026, 7, 13)) is True   # Monday


def test_observe_slots_are_agreed_defaults():
    assert OBSERVE_SLOTS == [(9, 35), (13, 5), (15, 5)]


def test_slot_key_matches_within_window():
    # 09:35–09:39 命中上午档
    assert slot_key_for(datetime(2026, 7, 13, 9, 36)) == "2026-07-13T09:35"
    assert slot_key_for(datetime(2026, 7, 13, 9, 42)) is None
    assert slot_key_for(datetime(2026, 7, 13, 15, 5)) == "2026-07-13T15:05"


def test_slot_key_for_converts_et_aware_to_beijing():
    """ET-aware input must not make CN slots fire on US wall-clock hours."""
    et = ZoneInfo("America/New_York")
    # 21:36 Beijing Monday = 09:36 ET Monday — CN afternoon/evening, no CN morning slot
    beijing_evening = datetime(2026, 7, 13, 21, 36, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert slot_key_for(beijing_evening) is None
    # Same instant via ET must agree
    assert slot_key_for(beijing_evening.astimezone(et)) is None


def test_effective_trade_date_today_splits_by_market():
    """Beijing Monday 08:00 → CN 07-20, US still 07-19 Eastern."""
    cn = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 7, 20, 8, 0, tzinfo=cn)
    selected = now.date()  # Beijing today
    assert effective_trade_date_for_market("CN", selected, now=now) == "2026-07-20"
    assert effective_trade_date_for_market("US", selected, now=now) == "2026-07-19"
    assert us_today(now).isoformat() == "2026-07-19"


def test_effective_trade_date_explicit_override_shared():
    cn = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 7, 20, 8, 0, tzinfo=cn)
    assert effective_trade_date_for_market("CN", "2026-07-15", now=now) == "2026-07-15"
    assert effective_trade_date_for_market("US", "2026-07-15", now=now) == "2026-07-15"
