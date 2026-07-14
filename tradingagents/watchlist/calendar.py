"""A 股交易日与观察时段。

一期用「周一至周五」近似交易日（不含法定节假日日历）；本机开机时由调度器轮询。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

# 产品确认默认：开盘确认 / 午后 / 收盘
OBSERVE_SLOTS: list[tuple[int, int]] = [(9, 35), (13, 5), (15, 5)]
# 每个时段的命中窗口（分钟），避免整点秒级漏检
_SLOT_WINDOW_MINUTES = 5

# 完整分析过期：距 Baseline.trade_date 的交易日数严格大于该阈值则升级再分析
FULL_ANALYSIS_STALE_TRADING_DAYS = 3


def is_cn_trading_day(dt: datetime | date) -> bool:
    """True if weekday Mon–Fri. Holidays treated as trading days in v1."""
    d = dt.date() if isinstance(dt, datetime) else dt
    return d.weekday() < 5


def _as_date(value: datetime | date | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def cn_trading_days_since(
    trade_date: datetime | date | str,
    as_of: datetime | date | str,
) -> int:
    """统计 trade_date 之后至 as_of（若为交易日则含当日）的 A 股近似交易日数。"""
    start = _as_date(trade_date)
    end = _as_date(as_of)
    if end <= start:
        return 0
    count = 0
    day = start + timedelta(days=1)
    while day <= end:
        if is_cn_trading_day(day):
            count += 1
        day += timedelta(days=1)
    return count


def is_full_analysis_stale(
    trade_date: datetime | date | str,
    as_of: datetime | date | str | None = None,
    *,
    threshold: int = FULL_ANALYSIS_STALE_TRADING_DAYS,
) -> bool:
    """完整分析是否过期：交易日差严格大于 threshold（默认 >3）。"""
    when = as_of if as_of is not None else datetime.now()
    return cn_trading_days_since(trade_date, when) > threshold


def slot_key_for(dt: datetime) -> str | None:
    """若当前时刻落在某个观察窗口内，返回稳定 slot key，否则 None。"""
    if not is_cn_trading_day(dt):
        return None
    for hour, minute in OBSERVE_SLOTS:
        start = hour * 60 + minute
        now = dt.hour * 60 + dt.minute
        if start <= now < start + _SLOT_WINDOW_MINUTES:
            return f"{dt.strftime('%Y-%m-%d')}T{hour:02d}:{minute:02d}"
    return None
