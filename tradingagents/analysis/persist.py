"""Persist / seed calibration anchors."""

from __future__ import annotations

import json
import logging
from typing import Any

from tradingagents.analysis.calibration import CalibrationStore, default_calibration_store
from tradingagents.watchlist.baseline import extract_baseline
from tradingagents.watchlist.models import Baseline
from tradingagents.watchlist.service import resolve_log_path

logger = logging.getLogger(__name__)


def save_calibration_from_state(
    state: dict[str, Any],
    *,
    ticker: str,
    trade_date: str,
    market: str = "CN",
    price: float | None = None,
    log_path: str = "",
    store: CalibrationStore | None = None,
) -> Baseline | None:
    """Write a new calibration anchor from a completed full analysis state."""
    if not state:
        return None
    cal = store or default_calibration_store()
    path = log_path or resolve_log_path(ticker, trade_date)
    try:
        if price is None:
            try:
                from tradingagents.watchlist.snapshot import fetch_snapshot

                snap = fetch_snapshot(ticker, market=market, max_headlines=0)
                price = float(snap.price) if snap.price else None
            except Exception:  # noqa: BLE001
                price = None
        baseline = extract_baseline(
            state,
            ticker=ticker,
            trade_date=trade_date,
            price=price,
            log_path=path,
            market=market,
        )
        cal.save(baseline)
        logger.info(
            "calibration saved %s %s (%s) stance=%s",
            ticker,
            trade_date,
            market,
            baseline.stance,
        )
        return baseline
    except Exception:  # noqa: BLE001
        logger.exception("failed to save calibration for %s", ticker)
        return None


def seed_calibration_from_history(
    ticker: str,
    *,
    market: str = "CN",
    store: CalibrationStore | None = None,
) -> Baseline | None:
    """If no calibration yet, seed from the latest completed report for ticker."""
    cal = store or default_calibration_store()
    existing = cal.get(ticker, market)
    if existing is not None:
        return existing

    try:
        from web.history import lookup_latest_action_plans
    except Exception:  # noqa: BLE001
        return None

    plans = lookup_latest_action_plans([ticker])
    summary = plans.get(str(ticker).strip().upper()) or {}
    path = str(summary.get("path") or "")
    trade_date = str(summary.get("date") or "")
    if not path or not trade_date:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(state, dict):
        return None
    return save_calibration_from_state(
        state,
        ticker=ticker,
        trade_date=trade_date,
        market=market,
        log_path=path,
        store=cal,
    )
