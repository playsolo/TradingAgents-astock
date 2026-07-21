"""A 股 / 美股交易日与观察时段。

用「周一至周五」近似交易日（不含法定节假日日历）；本机开机时由调度器轮询。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from enum import Enum
from zoneinfo import ZoneInfo


# 产品确认默认：开盘确认 / 午后 / 收盘（北京时间）
OBSERVE_SLOTS: list[tuple[int, int]] = [(9, 35), (13, 5), (15, 5)]
# 美股：开盘+5 / 午间 / 收盘+5（美东时间）
US_OBSERVE_SLOTS: list[tuple[int, int]] = [(9, 35), (12, 5), (16, 5)]
# 每个时段的命中窗口（分钟），避免整点秒级漏检
_SLOT_WINDOW_MINUTES = 5

# 操作建议时限缺省 / 解析失败回退：交易日数
FULL_ANALYSIS_STALE_TRADING_DAYS = 3

CN_TZ = ZoneInfo("Asia/Shanghai")
US_TZ = ZoneInfo("America/New_York")

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


def us_today(dt: datetime | None = None) -> date:
    """Calendar date in America/New_York."""
    return to_us_datetime(dt).date()


def effective_trade_date_for_market(
    market: str,
    selected: datetime | date | str,
    *,
    now: datetime | None = None,
) -> str:
    """Map the sidebar analysis date onto the correct market calendar.

    The UI date picker is Beijing-oriented. When the selected day equals
    Beijing *today*, CN jobs keep that date and US jobs use Eastern today
    instead — so a Beijing Monday morning does not stamp US runs with a
    session that has not opened yet in New York, and CN runs never inherit
    Eastern "yesterday". Any other selected date is an explicit override
    (same ISO string for both markets).
    """
    sel = _as_date(selected)
    if sel == cn_today(now):
        if (market or "CN").upper() == "US":
            return us_today(now).isoformat()
        return cn_today(now).isoformat()
    return sel.isoformat()


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
    """完整分析是否过期：交易日差严格大于 threshold（默认 >3）。

    遗留 API：观察池已改为操作建议时限（见 ``is_action_validity_expired``）。
    """
    when = as_of if as_of is not None else datetime.now()
    return cn_trading_days_since(trade_date, when) > threshold


def add_cn_trading_days(start: datetime | date | str, n: int) -> date:
    """日历日：``start`` 之后第 ``n`` 个交易日（``n<=0`` 时返回 start 的日期）。"""
    day = _as_date(start)
    if n <= 0:
        return day
    count = 0
    while count < n:
        day += timedelta(days=1)
        if is_cn_trading_day(day):
            count += 1
    return day


def effective_valid_trading_days(valid_trading_days: int | None) -> int:
    """Baseline 未写入时限时回退到默认 3 个交易日。"""
    if valid_trading_days is None:
        return FULL_ANALYSIS_STALE_TRADING_DAYS
    return max(1, int(valid_trading_days))


def is_action_validity_expired(
    baseline: object,
    as_of: datetime | date | str | None = None,
) -> bool:
    """操作建议时限是否已过：交易日差严格大于 ``valid_trading_days``。

    ``baseline`` 需有 ``trade_date`` 与可选 ``valid_trading_days``（duck-typed，
    避免 calendar ↔ models 循环 import）。
    """
    trade_date = getattr(baseline, "trade_date")
    days = effective_valid_trading_days(getattr(baseline, "valid_trading_days", None))
    when = as_of if as_of is not None else datetime.now()
    return cn_trading_days_since(trade_date, when) > days


def action_validity_expires_on(baseline: object) -> date:
    """时限末日（含）：分析日之后第 N 个交易日。"""
    trade_date = getattr(baseline, "trade_date")
    days = effective_valid_trading_days(getattr(baseline, "valid_trading_days", None))
    return add_cn_trading_days(trade_date, days)


def slot_key_for(dt: datetime | None = None) -> str | None:
    """若当前时刻落在某个 A 股观察窗口内，返回稳定 slot key，否则 None.

    Always evaluates against Asia/Shanghai wall clock so a UTC/ET-aware
    ``dt`` (or a mis-set process TZ) cannot shift CN slots onto US hours.
    """
    now = to_cn_datetime(dt)
    if not is_cn_trading_day(now):
        return None
    for hour, minute in OBSERVE_SLOTS:
        start = hour * 60 + minute
        minutes = now.hour * 60 + now.minute
        if start <= minutes < start + _SLOT_WINDOW_MINUTES:
            return f"{now.strftime('%Y-%m-%d')}T{hour:02d}:{minute:02d}"
    return None


def is_us_trading_day(dt: datetime | date) -> bool:
    """True if weekday Mon–Fri. Holidays treated as trading days in v1."""
    d = dt.date() if isinstance(dt, datetime) else dt
    return d.weekday() < 5


def to_us_datetime(dt: datetime | None = None) -> datetime:
    """Normalize to America/New_York.

    - ``None`` → current US Eastern time
    - aware → convert to US Eastern
    - naive → treat as US Eastern wall clock (for tests / explicit ET inputs)
    """
    if dt is None:
        return datetime.now(US_TZ)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=US_TZ)
    return dt.astimezone(US_TZ)


def slot_key_for_us(dt: datetime | None = None) -> str | None:
    """若美东时间落在美股观察窗口内，返回 ``US:YYYY-MM-DDTHH:MM`` slot key。"""
    now = to_us_datetime(dt)
    if not is_us_trading_day(now):
        return None
    for hour, minute in US_OBSERVE_SLOTS:
        start = hour * 60 + minute
        minutes = now.hour * 60 + now.minute
        if start <= minutes < start + _SLOT_WINDOW_MINUTES:
            return f"US:{now.strftime('%Y-%m-%d')}T{hour:02d}:{minute:02d}"
    return None


def active_observe_slots(dt: datetime | None = None) -> list[tuple[str, str]]:
    """Return currently active ``(market, slot_key)`` pairs for CN and/or US.

    CN slots always use Asia/Shanghai. US slots always use America/New_York.
    A naive ``dt`` is interpreted as Beijing local wall clock.
    """
    out: list[tuple[str, str]] = []
    cn_key = slot_key_for(dt)
    if cn_key:
        out.append(("CN", cn_key))
    if dt is None:
        us_dt: datetime | None = None
    elif dt.tzinfo is not None:
        us_dt = dt
    else:
        us_dt = dt.replace(tzinfo=CN_TZ)
    us_key = slot_key_for_us(us_dt)
    if us_key:
        out.append(("US", us_key))
    return out
