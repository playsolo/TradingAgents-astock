"""A 股观察时段与交易日判断。"""

from datetime import datetime

from tradingagents.watchlist.calendar import (
    OBSERVE_SLOTS,
    is_cn_trading_day,
    slot_key_for,
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
