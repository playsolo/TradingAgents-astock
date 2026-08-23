"""A-share close series for forward-return evaluation."""

from __future__ import annotations

import logging
from typing import Sequence

from tradingagents.agents.utils.signal_accuracy import return_at_horizon

logger = logging.getLogger(__name__)

BENCHMARK_CODE = "000300"
DEFAULT_HORIZONS: tuple[int, ...] = (5, 20)


def sina_close_fetcher(ticker: str, trade_date: str, need_bars: int) -> list[float] | None:
    """Daily closes on/after ``trade_date`` via Sina HTTP (A-share friendly)."""
    code = str(ticker).strip()
    if len(code) == 6 and code.isdigit():
        pass
    elif "." in code:
        code = code.split(".", 1)[0]
    try:
        from tradingagents.dataflows.a_stock import _sina_kline_fallback

        df = _sina_kline_fallback(code, start_date=trade_date)
        if df is None or df.empty:
            return None
        df = df.sort_values("Date")
        closes = [float(x) for x in df["Close"].tolist()]
        if not closes:
            return None
        return closes[: need_bars + 1]
    except Exception as exc:
        logger.debug("sina close fetch failed %s@%s: %s", ticker, trade_date, exc)
        return None


def forward_returns(
    ticker: str,
    trade_date: str,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    *,
    fetcher=sina_close_fetcher,
) -> dict[int, float] | None:
    """Absolute forward returns keyed by horizon (trading-day steps)."""
    if not horizons:
        return None
    need = max(int(h) for h in horizons)
    closes = fetcher(ticker, trade_date, need)
    if not closes:
        return None
    out: dict[int, float] = {}
    for h in horizons:
        ret = return_at_horizon(closes, int(h))
        if ret is not None:
            out[int(h)] = float(ret)
    return out or None
