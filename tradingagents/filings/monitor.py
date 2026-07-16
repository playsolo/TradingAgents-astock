"""Poll watchlist US tickers for new SEC filings and earnings previews."""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from tradingagents.filings.earnings_calendar import earnings_window_status
from tradingagents.filings.sec_edgar import TARGET_FORMS, list_recent_filings
from tradingagents.filings.store import FilingSeenStore

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")


def _today_et() -> date:
    return datetime.now(_ET).date()


def _trade_date_str() -> str:
    return _today_et().isoformat()


def collect_us_watch_tickers(
    *,
    iter_stores: Callable[[], Any] | None = None,
) -> list[tuple[str, str]]:
    """Return ``[(ticker, user_id_or_empty)]`` for enabled US watch items."""
    if iter_stores is None:
        from tradingagents.watchlist.store import iter_user_stores

        iter_stores = iter_user_stores

    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for store in iter_stores():
        user_id = getattr(store, "user_id", "") or ""
        for item in store.list_items():
            if not getattr(item, "enabled", True):
                continue
            baseline = item.baseline
            if (baseline.market or "CN").upper() != "US":
                continue
            ticker = (baseline.ticker or "").strip().upper()
            if not ticker or ticker in seen:
                continue
            seen.add(ticker)
            out.append((ticker, str(user_id)))
    return out


def poll_ticker_filings(
    ticker: str,
    *,
    store: FilingSeenStore,
    forms: frozenset[str] | set[str] = TARGET_FORMS,
    limit: int = 5,
    list_fn: Callable[..., list[dict[str, Any]]] = list_recent_filings,
    max_age_days: int = 5,
    today: date | None = None,
    enqueue_on_first_seen: bool = True,
) -> list[dict[str, Any]]:
    """Return new (unseen) filings for ``ticker`` and mark them seen.

    Filings older than ``max_age_days`` are recorded as seen but not returned
    (avoids flooding the analysis queue on first monitor start).
    """
    today = today or date.today()
    filings = list_fn(ticker, forms=forms, limit=limit)
    new_ones: list[dict[str, Any]] = []
    for f in filings:
        acc = str(f.get("accession") or "").strip()
        if not acc or store.has_accession(acc):
            continue
        store.mark_accession(
            acc,
            {
                "ticker": ticker.upper(),
                "form": f.get("form"),
                "filing_date": f.get("filing_date"),
            },
        )
        fdate_raw = str(f.get("filing_date") or "").strip()
        fdate = None
        try:
            fdate = date.fromisoformat(fdate_raw[:10]) if fdate_raw else None
        except ValueError:
            fdate = None
        if fdate is not None and (today - fdate).days > max_age_days:
            continue
        if not enqueue_on_first_seen:
            continue
        new_ones.append(f)
    return new_ones


def enqueue_us_filing_analysis(
    ticker: str,
    filing: dict[str, Any],
    *,
    append_job: Callable[[Any], Any] | None = None,
) -> Any:
    """Enqueue a forced full US analysis for a new filing."""
    from web.analysis_queue import AnalysisJob, AnalysisQueueStore

    job = AnalysisJob(
        ticker=ticker.upper(),
        trade_date=_trade_date_str(),
        market="US",
        fresh=True,
        force_full_reeval=True,
        analysis_mode="full_reeval",
        source="filing",
    )
    if append_job is not None:
        return append_job(job)
    return AnalysisQueueStore().append_atomic(job)


def emit_filing_inbox(ticker: str, filing: dict[str, Any]) -> None:
    from tradingagents import inbox

    form = filing.get("form") or "?"
    fdate = filing.get("filing_date") or ""
    acc = filing.get("accession") or ""
    inbox.emit(
        "watch.alert",
        title=f"{ticker} 新 SEC 公告 {form}",
        severity="warning",
        detail=f"filing_date={fdate} accession={acc} — 已触发美股深度分析",
        ticker=ticker,
        trade_date=_trade_date_str(),
        link_view="watch",
        dedupe_key=f"filing:{acc}",
    )


def emit_earnings_preview(ticker: str, next_date: str, days_until: int) -> None:
    from tradingagents import inbox

    inbox.emit(
        "watch.alert",
        title=f"{ticker} 财报预告 {next_date}",
        severity="info",
        detail=f"距离预计财报日约 {days_until} 天；将提高 EDGAR 巡检频率，文件落地后自动深度分析",
        ticker=ticker,
        trade_date=_trade_date_str(),
        link_view="watch",
        dedupe_key=f"earnings-preview:{ticker}:{next_date}",
    )


def run_filing_poll_once(
    *,
    seen_store: FilingSeenStore | None = None,
    tickers: list[str] | None = None,
    enqueue: bool = True,
    notify: bool = True,
    today: date | None = None,
) -> dict[str, Any]:
    """One poll cycle: earnings previews + new filings → optional enqueue.

    Returns a summary dict for logging/tests.
    """
    seen_store = seen_store or FilingSeenStore()
    today = today or _today_et()

    if tickers is None:
        pairs = collect_us_watch_tickers()
        tickers = [t for t, _ in pairs]

    summary: dict[str, Any] = {
        "tickers": list(tickers),
        "previews": [],
        "new_filings": [],
        "enqueued": [],
    }

    for ticker in tickers:
        sym = ticker.strip().upper()
        if not sym:
            continue

        # Earnings preview (calendar advance notice)
        try:
            st = earnings_window_status(sym, today=today)
        except Exception as exc:  # noqa: BLE001
            logger.warning("earnings calendar failed for %s: %s", sym, exc)
            st = {"phase": "none", "next_date": None, "days_until": None}

        if st.get("phase") in {"preview", "dense", "day_of"} and st.get("next_date"):
            nd = str(st["next_date"])
            if not seen_store.has_earnings_preview(sym, nd):
                seen_store.mark_earnings_preview(sym, nd)
                summary["previews"].append({"ticker": sym, **st})
                if notify:
                    try:
                        emit_earnings_preview(sym, nd, int(st.get("days_until") or 0))
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("inbox preview failed: %s", exc)

        # New SEC filings → deep analysis
        try:
            new_filings = poll_ticker_filings(sym, store=seen_store, today=today)
        except Exception as exc:  # noqa: BLE001
            logger.warning("EDGAR poll failed for %s: %s", sym, exc)
            continue

        for filing in new_filings:
            summary["new_filings"].append({"ticker": sym, **filing})
            if notify:
                try:
                    emit_filing_inbox(sym, filing)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("inbox filing failed: %s", exc)
            if enqueue:
                try:
                    enqueue_us_filing_analysis(sym, filing)
                    summary["enqueued"].append(sym)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("enqueue failed for %s: %s", sym, exc)

    return summary
