"""Helpers to mirror resolved memory outcomes into per-ticker lessons.jsonl."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from tradingagents.archive.models import Lesson
from tradingagents.archive.store import StockArchiveStore, default_archive_store

logger = logging.getLogger(__name__)


def infer_market(ticker: str) -> str:
    t = (ticker or "").strip().upper()
    if t.isdigit() and len(t) == 6:
        return "CN"
    return "US"


def record_lesson(
    *,
    ticker: str,
    trade_date: str,
    rating: str,
    raw_return: float,
    alpha_return: float,
    holding_days: int,
    reflection: str,
    market: str | None = None,
    store: StockArchiveStore | None = None,
) -> Lesson | None:
    """Append one resolved lesson. Failures are logged, never raised."""
    t = (ticker or "").strip().upper()
    if not t or not trade_date:
        return None
    archives = store or default_archive_store()
    mkt = (market or infer_market(t)).upper()
    lesson = Lesson(
        ticker=t,
        trade_date=str(trade_date),
        market=mkt,
        rating=str(rating or "Hold"),
        raw_return=float(raw_return),
        alpha_return=float(alpha_return),
        holding_days=int(holding_days),
        reflection=str(reflection or "").strip(),
        resolved_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    try:
        return archives.append_lesson(lesson)
    except Exception:  # noqa: BLE001
        logger.exception("archive append_lesson failed for %s %s", t, trade_date)
        return None


def record_lessons(
    updates: list[dict[str, Any]],
    *,
    store: StockArchiveStore | None = None,
) -> int:
    """Mirror a batch of resolved memory outcomes. Each dict needs rating."""
    n = 0
    for upd in updates or []:
        if record_lesson(
            ticker=str(upd.get("ticker") or ""),
            trade_date=str(upd.get("trade_date") or ""),
            rating=str(upd.get("rating") or "Hold"),
            raw_return=float(upd.get("raw_return") or 0.0),
            alpha_return=float(upd.get("alpha_return") or 0.0),
            holding_days=int(upd.get("holding_days") or 0),
            reflection=str(upd.get("reflection") or ""),
            market=upd.get("market"),
            store=store,
        ):
            n += 1
    return n
