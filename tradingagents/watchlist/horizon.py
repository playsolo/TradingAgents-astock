"""Parse free-text action-plan horizons into trading-day validity."""

from __future__ import annotations

import re

from tradingagents.watchlist.calendar import FULL_ANALYSIS_STALE_TRADING_DAYS

# Cap absurd LLM outputs (≈3 calendar months of sessions).
_MAX_VALID_TRADING_DAYS = 60

_RANGE_SEP = r"[-~～〜—–至到]"
_WEEK_UNIT = r"(?:个?交易周|个?周|weeks?|w)\b"
_DAY_UNIT = r"(?:个?交易日|个?交易天|交易日|天|days?|d)\b"

_RANGE_WEEK_RE = re.compile(
    rf"(\d+)\s*{_RANGE_SEP}\s*(\d+)\s*{_WEEK_UNIT}",
    re.IGNORECASE,
)
_RANGE_DAY_RE = re.compile(
    rf"(\d+)\s*{_RANGE_SEP}\s*(\d+)\s*{_DAY_UNIT}",
    re.IGNORECASE,
)
_SINGLE_WEEK_RE = re.compile(rf"(\d+)\s*{_WEEK_UNIT}", re.IGNORECASE)
_SINGLE_DAY_RE = re.compile(rf"(\d+)\s*{_DAY_UNIT}", re.IGNORECASE)
# Bare range / number without unit → assume trading days (UI copy: 「3-5个交易日」).
_BARE_RANGE_RE = re.compile(rf"(\d+)\s*{_RANGE_SEP}\s*(\d+)")
_BARE_NUM_RE = re.compile(r"(\d+)")


def parse_horizon_trading_days(
    horizon: str | None,
    *,
    default: int = FULL_ANALYSIS_STALE_TRADING_DAYS,
) -> int:
    """Return validity length in approximate CN trading days.

    Ranges use the **upper** bound (e.g. ``3-5个交易日`` → 5). Weeks map to
    ``n * 5`` trading days. Unparseable text falls back to ``default``.
    """
    text = (horizon or "").strip()
    if not text:
        return int(default)

    for pattern, weekish in (
        (_RANGE_WEEK_RE, True),
        (_RANGE_DAY_RE, False),
    ):
        m = pattern.search(text)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            n = max(lo, hi)
            return _clamp(n * 5 if weekish else n)

    for pattern, weekish in (
        (_SINGLE_WEEK_RE, True),
        (_SINGLE_DAY_RE, False),
    ):
        m = pattern.search(text)
        if m:
            n = int(m.group(1))
            return _clamp(n * 5 if weekish else n)

    m = _BARE_RANGE_RE.search(text)
    if m:
        return _clamp(max(int(m.group(1)), int(m.group(2))))

    m = _BARE_NUM_RE.search(text)
    if m:
        return _clamp(int(m.group(1)))

    return int(default)


def _clamp(n: int) -> int:
    return max(1, min(int(n), _MAX_VALID_TRADING_DAYS))
