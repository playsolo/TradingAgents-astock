"""Web-scrape fallback for OHLCV rows where yfinance returns NaN Close.

yfinance (both ``ticker.history()`` and ``yf.download(auto_adjust=True)``) can
return a row with valid Open/High/Low/Volume but a NaN Close for the latest
trading day when a recent dividend confuses the adjustment algorithm.  The
Yahoo Finance *quote page* always has the correct last traded price, so we
scrape it as a lightweight, no-API-key fallback.
"""

from __future__ import annotations

import logging
import re

import pandas as pd
import requests

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _fetch_quote_page_close(symbol: str) -> float | None:
    """Scrape the Yahoo Finance quote page for the last trade price.

    Returns ``None`` on any failure (network, parse, rate-limit) so the caller
    can fall through gracefully.
    """
    url = f"https://finance.yahoo.com/quote/{symbol}/"
    try:
        resp = requests.get(url, headers={"User-Agent": _USER_AGENT}, timeout=15)
        resp.raise_for_status()
    except Exception as exc:
        logger.warning("Quote-page fetch failed for %s: %s", symbol, exc)
        return None

    # Pattern A:  data-testid="qsp-price">983.12</span>
    m = re.search(
        r'data-testid="qsp-price">\s*([\d,]+\.\d+)',
        resp.text,
    )
    if m:
        return float(m.group(1).replace(",", ""))

    # Pattern B:  data-field="regularMarketPrice" ... data-value="983.12"
    m = re.search(
        r'data-field="regularMarketPrice"[^>]*?data-value="([\d.]+)"',
        resp.text,
    )
    if m:
        return float(m.group(1))

    # Pattern C:  "983.12<arrow/>+46.12 (+4.92%)"
    m = re.search(r"([\d,]+\.\d+)\s*<arrow", resp.text)
    if m:
        return float(m.group(1).replace(",", ""))

    return None


def fill_nan_close_from_web(
    data: pd.DataFrame,
    symbol: str,
    *,
    max_rows_to_check: int = 5,
) -> pd.DataFrame:
    """When the latest OHLCV row has a NaN Close but valid OHLC, try to fill
    it from the Yahoo Finance quote page.

    Returns a **copy** of ``data`` with NaN Close values filled (in-place when
    the scrape succeeds) so callers don't have to handle mutated inputs.

    Only checks the last ``max_rows_to_check`` rows to avoid unnecessary web
    calls for deep-historical NaN values.
    """
    if data is None or data.empty:
        return data

    df = data.copy()
    recent = df.tail(max_rows_to_check)
    nan_mask = (
        recent["Close"].isna() if "Close" in recent.columns
        else pd.Series(False, index=recent.index)
    )
    if not nan_mask.any():
        return df

    # Only attempt the scrape when at least one NaN row has market activity.
    # yfinance sometimes returns a row where ALL of OHLC are NaN but Volume
    # is non-zero (e.g. MU on 2026-07-14 after a dividend).  In that case
    # the market was open and the quote page has the correct last price.
    has_volume = recent.get("Volume", pd.Series(0)).fillna(0) > 0
    candidates = nan_mask & has_volume
    if not candidates.any():
        return df

    price = _fetch_quote_page_close(symbol)
    if price is None:
        return df

    df.loc[candidates[candidates].index, "Close"] = price
    logger.info("Filled NaN Close for %s from quote page: %.2f", symbol, price)
    return df


def estimate_close_from_typical_price(data: pd.DataFrame) -> pd.DataFrame:
    """Fill NaN Close rows that have valid Open/High/Low with (O+H+L)/3.

    This is a no-request fallback when the web scrape fails.  The typical
    price is empirically within ~2.5 % of the true Close for actively traded
    equities, which is good enough to keep the latest trading day visible in
    reports.
    """
    if data is None or data.empty or "Close" not in data.columns:
        return data
    df = data.copy()
    nan_close = df["Close"].isna()
    if not nan_close.any():
        return df
    ohlc_ok = (
        df["Open"].notna() & df["High"].notna() & df["Low"].notna()
    )
    fill_mask = nan_close & ohlc_ok
    if fill_mask.any():
        df.loc[fill_mask, "Close"] = (
            df.loc[fill_mask, ["Open", "High", "Low"]].sum(axis=1) / 3
        )
    return df
