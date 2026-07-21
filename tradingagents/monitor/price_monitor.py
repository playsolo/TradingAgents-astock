"""Background price monitor that scans saved analysis results for buy-zone
opportunities and writes triggered signals to a persistent JSON file.

Architecture
------------
- Runs as a daemon thread inside the Streamlit Web process (or standalone).
- Every 5 minutes during A-share trading hours (9:30-11:30, 13:00-15:00, Mon-Fri):
  1. Scan all completed analysis results (Buy + Hold signals) for ``buy_zone_low``
     and ``buy_zone_high`` in their ``action_plan.levels``.
  2. Fetch current spot prices via Tencent Finance batch API.
  3. For each stock whose current price falls within [buy_zone_low, buy_zone_high],
     write a trigger record into ``~/.tradingagents/watchlist_signals.json``.
- UI reads the same JSON to populate the "关注-待买入" tab.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Any

from tradingagents.watchlist.calendar import CN_TZ, is_cn_trading_day

logger = logging.getLogger(__name__)

# ---- paths ----------------------------------------------------------------

_WATCHLIST_SIGNALS_FILE = Path.home() / ".tradingagents" / "watchlist_signals.json"
_SIGNALS_LOCK = threading.Lock()

# A-share continuous auction boundaries (Beijing time).
_MORNING_START = dtime(9, 30)
_MORNING_END = dtime(11, 30)
_AFTERNOON_START = dtime(13, 0)
_AFTERNOON_END = dtime(15, 0)

# Default scan interval in seconds.
_DEFAULT_INTERVAL = 300  # 5 minutes


# ---- public helpers -------------------------------------------------------


def is_cn_trading_hours(now: datetime | None = None) -> bool:
    """Check whether *now* (Beijing time) falls within A-share trading hours.

    Returns True only on trading days during 9:30-11:30 or 13:00-15:00.
    """
    dt = datetime.now(CN_TZ) if now is None else now
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CN_TZ)
    if not is_cn_trading_day(dt):
        return False
    t = dt.time()
    return (_MORNING_START <= t <= _MORNING_END) or (_AFTERNOON_START <= t <= _AFTERNOON_END)


def _signal_path(*, path: str | Path | None = None) -> Path:
    p = Path(path) if path else _WATCHLIST_SIGNALS_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def read_signals(path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """Read the current watchlist signals file.

    Returns ``{ticker: signal_record}``. A ticker is only present when its
    buy-zone was triggered.
    """
    p = _signal_path(path=path)
    with _SIGNALS_LOCK:
        if not p.exists():
            return {}
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}


def dismiss_signal(ticker: str, path: str | Path | None = None) -> bool:
    """Remove a ticker from watchlist signals (user confirmed / dismissed).

    Returns True when the ticker existed and was removed.
    """
    p = _signal_path(path=path)
    with _SIGNALS_LOCK:
        signals = _read_signals_raw(p)
        removed = signals.pop(ticker, None)
        if removed is not None:
            _write_signals_raw(p, signals)
        return removed is not None


# ---- internal helpers -----------------------------------------------------


def _read_signals_raw(p: Path) -> dict[str, dict[str, Any]]:
    """Unlocked read from disk. Caller must hold ``_SIGNALS_LOCK``."""
    if not p.exists():
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_signals_raw(p: Path, signals: dict[str, dict[str, Any]]) -> None:
    """Unlocked atomic write. Caller must hold ``_SIGNALS_LOCK``."""
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(signals, f, ensure_ascii=False, indent=2)
    tmp.replace(p)


def _scan_history_for_buy_zones() -> list[dict[str, Any]]:
    """Scan completed analysis JSON files for stocks with buy_zone_low/high.

    Returns entries where ``action_plan.levels`` defines a buy zone.
    Each entry: ``{"ticker", "date", "buy_zone_low", "buy_zone_high", "signal"}``
    Only includes Buy and Hold signals (the categories the user wants monitored).
    """
    from web.history import get_history, load_analysis

    candidates: list[dict[str, Any]] = []
    for entry in get_history():
        signal = entry.get("signal", "N/A")
        if signal not in ("Buy", "Hold"):
            continue
        path = entry.get("path", "")
        if not path:
            continue
        try:
            state = load_analysis(path)
        except Exception:
            continue
        plan = state.get("action_plan")
        if not isinstance(plan, dict):
            continue
        levels = plan.get("levels")
        if not isinstance(levels, dict):
            continue
        low = levels.get("buy_zone_low")
        high = levels.get("buy_zone_high")
        if low is not None and high is not None and isinstance(low, (int, float)) and isinstance(high, (int, float)):
            candidates.append({
                "ticker": entry["ticker"],
                "date": entry["date"],
                "buy_zone_low": float(low),
                "buy_zone_high": float(high),
                "signal": signal,
            })
    return candidates


def _needle_buy_zones() -> dict[str, dict[str, Any]]:
    """Run one scan pass: fetch prices, compare with buy zones, return triggers.

    Returns dict of triggered signals: ``{ticker: record_dict}``.
    """
    candidates = _scan_history_for_buy_zones()
    if not candidates:
        logger.debug("price_monitor: no candidates with buy_zone_low/high found")
        return {}

    tickers = list({c["ticker"] for c in candidates})
    from tradingagents.dataflows.a_stock import batch_get_spot_prices

    prices = batch_get_spot_prices(tickers)
    if not prices:
        logger.debug("price_monitor: batch_get_spot_prices returned empty")
        return {}

    now_iso = datetime.now(CN_TZ).isoformat(timespec="seconds")
    triggered: dict[str, dict[str, Any]] = {}

    for cand in candidates:
        t = cand["ticker"]
        price = prices.get(t)
        if price is None or price <= 0:
            continue
        low = cand["buy_zone_low"]
        high = cand["buy_zone_high"]
        if low <= price <= high:
            # Keep the most recent analysis date for this ticker
            existing = triggered.get(t)
            if existing and existing.get("date", "") >= cand["date"]:
                continue
            triggered[t] = {
                "ticker": t,
                "date": cand["date"],
                "buy_zone_low": low,
                "buy_zone_high": high,
                "current_price": round(price, 2),
                "signal": cand["signal"],
                "signaled_at": now_iso,
            }

    return triggered


def _merge_signals(
    existing: dict[str, dict[str, Any]],
    new_triggered: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Merge newly triggered signals into existing signals.

    - New tickers are added.
    - Existing tickers are kept (don't re-trigger the same ticker).
    - A ticker whose price*was* triggered but the new scan shows no trigger
      is *kept* (it only disappears when the user dismisses it).
    """
    merged = dict(existing)
    for ticker, record in new_triggered.items():
        if ticker not in merged:
            merged[ticker] = record
    return merged


def scan_once(*, signals_path: str | Path | None = None) -> int:
    """Run one scan-and-merge cycle. Returns how many new signals were added."""
    p = _signal_path(path=signals_path)
    new_triggers = _needle_buy_zones()
    with _SIGNALS_LOCK:
        existing = _read_signals_raw(p)
        before = len(existing)
        merged = _merge_signals(existing, new_triggers)
        after = len(merged)
        if merged != existing:
            _write_signals_raw(p, merged)
    added = after - before
    if added:
        logger.info("price_monitor: %d new buy-zone signal(s)", added)
    return added


# ---- background loop ------------------------------------------------------


class PriceMonitor:
    """Background daemon that periodically scans buy zones during trading hours."""

    def __init__(
        self,
        *,
        interval: int = _DEFAULT_INTERVAL,
        signals_path: str | Path | None = None,
        auto_start: bool = True,
    ):
        self.interval = interval
        self.signals_path = signals_path
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        if auto_start:
            self.start()

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_alive:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="PriceMonitor",
            daemon=True,
        )
        self._thread.start()
        logger.info("price_monitor: background thread started (interval=%ds)", self.interval)

    def stop(self) -> None:
        self._stop_event.set()

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                if is_cn_trading_hours():
                    scan_once(signals_path=self.signals_path)
                else:
                    logger.debug("price_monitor: outside trading hours, skipping")
            except Exception:
                logger.warning("price_monitor: scan cycle failed", exc_info=True)
            # Wait for interval (checking stop_event every second for responsive shutdown)
            for _ in range(self.interval):
                if self._stop_event.is_set():
                    return
                time.sleep(1)


# ---- global singleton -----------------------------------------------------

_monitor_instance: PriceMonitor | None = None
_monitor_lock = threading.Lock()


def ensure_monitor_running(
    *,
    interval: int = _DEFAULT_INTERVAL,
    signals_path: str | Path | None = None,
) -> PriceMonitor:
    """Get or create the global PriceMonitor singleton."""
    global _monitor_instance
    with _monitor_lock:
        if _monitor_instance is None or not _monitor_instance.is_alive:
            _monitor_instance = PriceMonitor(
                interval=interval,
                signals_path=signals_path,
                auto_start=True,
            )
        return _monitor_instance
