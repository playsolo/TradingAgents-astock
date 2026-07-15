"""A 股交易日与观察时段。

一期用「周一至周五」近似交易日（不含法定节假日日历）；本机开机时由调度器轮询。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from enum import Enum
from zoneinfo import ZoneInfo


# 产品确认默认：开盘确认 / 午后 / 收盘
OBSERVE_SLOTS: list[tuple[int, int]] = [(9, 35), (13, 5), (15, 5)]
# 每个时段的命中窗口（分钟），避免整点秒级漏检
_SLOT_WINDOW_MINUTES = 5

# 完整分析过期：距 Baseline.trade_date 的交易日数严格大于该阈值则升级再分析
FULL_ANALYSIS_STALE_TRADING_DAYS = 3

CN_TZ = ZoneInfo("Asia/Shanghai")

# A 股连续竞价边界（分钟自 00:00）
_CN_MORNING_OPEN_MIN = 9 * 60 + 30
_CN_MORNING_CLOSE_MIN = 11 * 60 + 30
_CN_AFTERNOON_OPEN_MIN = 13 * 60
_CN_AFTERNOON_CLOSE_MIN = 15 * 60


class CnSessionPhase(str, Enum):
    """Wall-clock A-share session relative to continuous auction."""

    IN_SESSION = "in_session"
    LUNCH_BREAK = "lunch_break"
    PRE_MARKET = "pre_market"
    AFTER_HOURS = "after_hours"
    NON_TRADING_DAY = "non_trading_day"


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


def to_cn_datetime(dt: datetime | None = None) -> datetime:
    """Normalize to Asia/Shanghai. Naive datetimes are treated as Beijing time."""
    if dt is None:
        return datetime.now(CN_TZ)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=CN_TZ)
    return dt.astimezone(CN_TZ)


def cn_today(dt: datetime | None = None) -> date:
    """Calendar date in Asia/Shanghai."""
    return to_cn_datetime(dt).date()


def cn_session_phase(dt: datetime | None = None) -> CnSessionPhase:
    """Classify current moment into an A-share session phase (Beijing time)."""
    now = to_cn_datetime(dt)
    if not is_cn_trading_day(now):
        return CnSessionPhase.NON_TRADING_DAY
    minutes = now.hour * 60 + now.minute
    if minutes < _CN_MORNING_OPEN_MIN:
        return CnSessionPhase.PRE_MARKET
    if _CN_MORNING_OPEN_MIN <= minutes < _CN_MORNING_CLOSE_MIN:
        return CnSessionPhase.IN_SESSION
    if _CN_MORNING_CLOSE_MIN <= minutes < _CN_AFTERNOON_OPEN_MIN:
        return CnSessionPhase.LUNCH_BREAK
    if _CN_AFTERNOON_OPEN_MIN <= minutes < _CN_AFTERNOON_CLOSE_MIN:
        return CnSessionPhase.IN_SESSION
    return CnSessionPhase.AFTER_HOURS


def next_cn_trading_day(value: datetime | date | str) -> date:
    """First approximate trading day strictly after ``value``."""
    day = _as_date(value) + timedelta(days=1)
    while not is_cn_trading_day(day):
        day += timedelta(days=1)
    return day


def actionable_cn_trading_day(dt: datetime | None = None) -> date:
    """Next session on which new orders can still be placed (wall-clock).

    Pre-market / lunch / continuous auction → that calendar day.
    After close or non-trading day → next Mon–Fri approximating the next session.
    """
    now = to_cn_datetime(dt)
    phase = cn_session_phase(now)
    today = now.date()
    if phase in (
        CnSessionPhase.IN_SESSION,
        CnSessionPhase.LUNCH_BREAK,
        CnSessionPhase.PRE_MARKET,
    ):
        return today
    return next_cn_trading_day(today)


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
