"""Earnings calendar helpers (yfinance) for US filing preview windows."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any


def _to_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text or text.lower() in {"nan", "nat", "none"}:
        return None
    # yfinance may return Timestamp-like or ISO strings
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def next_earnings_date(ticker: str, *, today: date | None = None) -> date | None:
    """Return the next upcoming earnings date for ``ticker``, if known.

    Uses ``yfinance.Ticker.earnings_dates`` / ``calendar`` when available.
    Returns None when Yahoo has no schedule (common for some names).
    """
    today = today or date.today()
    try:
        import yfinance as yf
    except ImportError:
        return None

    sym = (ticker or "").strip().upper()
    if not sym:
        return None

    t = yf.Ticker(sym)

    # Prefer earnings_dates (indexed by datetime).
    try:
        ed = t.earnings_dates
    except Exception:  # noqa: BLE001
        ed = None

    candidates: list[date] = []
    if ed is not None and hasattr(ed, "index"):
        for idx in list(ed.index)[:12]:
            d = _to_date(idx)
            if d and d >= today:
                candidates.append(d)

    if not candidates:
        try:
            cal = t.calendar
        except Exception:  # noqa: BLE001
            cal = None
        if isinstance(cal, dict):
            raw = cal.get("Earnings Date") or cal.get("earningsDate")
            if isinstance(raw, (list, tuple)):
                for item in raw:
                    d = _to_date(item)
                    if d and d >= today:
                        candidates.append(d)
            else:
                d = _to_date(raw)
                if d and d >= today:
                    candidates.append(d)

    if not candidates:
        return None
    return min(candidates)


def earnings_window_status(
    ticker: str,
    *,
    today: date | None = None,
    preview_days: int = 7,
    dense_days: int = 1,
) -> dict[str, Any]:
    """Classify proximity to next earnings for polling density.

    Returns keys: ``next_date``, ``days_until``, ``phase`` where phase is
    ``none`` | ``preview`` | ``dense`` | ``day_of``.
    """
    today = today or date.today()
    nxt = next_earnings_date(ticker, today=today)
    if nxt is None:
        return {"next_date": None, "days_until": None, "phase": "none"}
    delta = (nxt - today).days
    if delta == 0:
        phase = "day_of"
    elif 0 < delta <= dense_days:
        phase = "dense"
    elif 0 < delta <= preview_days:
        phase = "preview"
    else:
        phase = "none"
    return {"next_date": nxt.isoformat(), "days_until": delta, "phase": phase}


def is_in_earnings_window(
    ticker: str,
    *,
    today: date | None = None,
    preview_days: int = 7,
) -> bool:
    st = earnings_window_status(ticker, today=today, preview_days=preview_days)
    return st["phase"] in {"preview", "dense", "day_of"}
