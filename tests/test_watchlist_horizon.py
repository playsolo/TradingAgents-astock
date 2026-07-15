"""操作建议时限解析与观察池有效期判定。"""

from datetime import date, datetime

from tradingagents.watchlist.calendar import (
    FULL_ANALYSIS_STALE_TRADING_DAYS,
    add_cn_trading_days,
    cn_trading_days_since,
    is_action_validity_expired,
)
from tradingagents.watchlist.horizon import parse_horizon_trading_days
from tradingagents.watchlist.models import Baseline


def test_parse_horizon_uses_upper_bound_for_day_range():
    assert parse_horizon_trading_days("3-5个交易日") == 5
    assert parse_horizon_trading_days("时限：3～5个交易日") == 5


def test_parse_horizon_weeks_to_trading_days():
    assert parse_horizon_trading_days("1-2个交易周") == 10
    assert parse_horizon_trading_days("覆盖未来至少1-2个交易周。") == 10
    assert parse_horizon_trading_days("1-2w") == 10


def test_parse_horizon_single_and_fallback():
    assert parse_horizon_trading_days("3个交易日") == 3
    assert parse_horizon_trading_days("") == FULL_ANALYSIS_STALE_TRADING_DAYS
    assert parse_horizon_trading_days(None) == FULL_ANALYSIS_STALE_TRADING_DAYS
    assert parse_horizon_trading_days("视情况而定") == FULL_ANALYSIS_STALE_TRADING_DAYS


def test_add_cn_trading_days_skips_weekend():
    # Mon 7/13 + 5 trading days = Mon 7/20
    assert add_cn_trading_days("2026-07-13", 5) == date(2026, 7, 20)


def test_is_action_validity_expired_uses_baseline_days():
    base = Baseline(
        ticker="300253",
        trade_date="2026-07-15",  # Wed
        market="CN",
        stance="Sell",
        position_pct=60.0,
        baseline_price=7.1,
        entry_price=None,
        stop_loss=6.73,
        thesis_summary="减持",
        major_risks=[],
        log_path="",
        horizon_raw="3-5个交易日",
        valid_trading_days=5,
    )
    # Wed + 5 sessions: Thu Fri Mon Tue Wed(22) → days_since=5 still valid
    assert (
        is_action_validity_expired(base, datetime(2026, 7, 22, 15, 0)) is False
    )
    # Next day Thu 7/23 → 6 trading days since → expired
    assert is_action_validity_expired(base, datetime(2026, 7, 23, 9, 35)) is True


def test_legacy_baseline_without_horizon_falls_back_to_3():
    base = Baseline(
        ticker="002648",
        trade_date="2026-07-13",
        market="CN",
        stance="Hold",
        position_pct=10.0,
        baseline_price=100.0,
        entry_price=None,
        stop_loss=None,
        thesis_summary="",
        major_risks=[],
        log_path="",
    )
    assert base.valid_trading_days is None
    assert is_action_validity_expired(base, datetime(2026, 7, 16, 15, 0)) is False
    assert is_action_validity_expired(base, datetime(2026, 7, 17, 9, 35)) is True


def test_cn_trading_days_since_unchanged():
    assert cn_trading_days_since("2026-07-13", date(2026, 7, 16)) == 3
