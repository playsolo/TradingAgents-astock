"""抓取观察用行情快照与近期标题（轻量，不跑完整 Agent）。"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from tradingagents.watchlist.models import MarketSnapshot

logger = logging.getLogger(__name__)


def fetch_snapshot(
    ticker: str,
    *,
    market: str = "CN",
    max_headlines: int = 8,
) -> MarketSnapshot:
    """按市场抓取快照：A 股走腾讯/东财，美股走 yfinance。"""
    if (market or "CN").upper() == "US":
        return _fetch_us_snapshot(ticker, max_headlines=max_headlines)
    return _fetch_cn_snapshot(ticker, max_headlines=max_headlines)


def _fetch_cn_snapshot(ticker: str, *, max_headlines: int = 8) -> MarketSnapshot:
    """腾讯行情 + 个股新闻标题 + 当日主力净流入（失败时尽量降级）。"""
    from tradingagents.dataflows import a_stock

    code = str(ticker).strip().upper()
    price = 0.0
    change_pct = 0.0
    name = code
    pe_ttm = None
    turnover_pct = None

    try:
        quotes = a_stock._tencent_quote([code])
        q = quotes.get(code) or {}
        price = float(q.get("price") or 0)
        change_pct = float(q.get("change_pct") or 0)
        name = str(q.get("name") or code)
        pe_ttm = float(q["pe_ttm"]) if q.get("pe_ttm") else None
        turnover_pct = float(q["turnover_pct"]) if q.get("turnover_pct") else None
    except Exception as e:
        logger.warning("watchlist quote failed for %s: %s", code, e)

    headlines: list[str] = []
    try:
        end = datetime.now()
        start = end - timedelta(days=7)
        raw = a_stock.get_news(code, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        headlines = _extract_headlines(str(raw), max_headlines)
    except Exception as e:
        logger.warning("watchlist news failed for %s: %s", code, e)

    main_net_inflow = None
    try:
        main_net_inflow = a_stock.get_realtime_main_net_inflow(code)
    except Exception as e:
        logger.warning("watchlist fund flow failed for %s: %s", code, e)

    return MarketSnapshot(
        price=price,
        change_pct=change_pct,
        name=name,
        pe_ttm=pe_ttm,
        turnover_pct=turnover_pct,
        headlines=headlines,
        main_net_inflow=main_net_inflow,
    )


def _fetch_us_snapshot(ticker: str, *, max_headlines: int = 8) -> MarketSnapshot:
    """yfinance 最新价 + 近 7 日新闻；美股无主力净流入。"""
    from tradingagents.dataflows.stockstats_utils import yf_retry
    from tradingagents.dataflows.yfinance_news import get_news_yfinance
    import yfinance as yf

    symbol = str(ticker).strip().upper()
    price = 0.0
    change_pct = 0.0
    name = symbol
    pe_ttm = None

    try:
        stock = yf.Ticker(symbol)
        hist = yf_retry(lambda: stock.history(period="5d"))
        if hist is not None and not hist.empty and "Close" in hist.columns:
            closes = hist["Close"].dropna()
            if len(closes) >= 1:
                price = float(closes.iloc[-1])
            if len(closes) >= 2 and float(closes.iloc[-2]) > 0:
                prev = float(closes.iloc[-2])
                change_pct = (price - prev) / prev * 100.0
        try:
            info = yf_retry(lambda: stock.info) or {}
            name = str(info.get("shortName") or info.get("longName") or symbol)
            trailing = info.get("trailingPE")
            if trailing is not None:
                pe_ttm = float(trailing)
        except Exception:
            pass
    except Exception as e:
        logger.warning("watchlist US quote failed for %s: %s", symbol, e)

    headlines: list[str] = []
    try:
        end = datetime.now()
        start = end - timedelta(days=7)
        raw = get_news_yfinance(
            symbol,
            start.strftime("%Y-%m-%d"),
            end.strftime("%Y-%m-%d"),
        )
        headlines = _extract_headlines(str(raw), max_headlines)
    except Exception as e:
        logger.warning("watchlist US news failed for %s: %s", symbol, e)

    return MarketSnapshot(
        price=price,
        change_pct=change_pct,
        name=name,
        pe_ttm=pe_ttm,
        turnover_pct=None,
        headlines=headlines,
        main_net_inflow=None,
    )


def _extract_headlines(text: str, limit: int) -> list[str]:
    """优先取 `###` 标题行，避免把资金榜正文数字堆进盘面标题。"""
    titled: list[str] = []
    fallback: list[str] = []
    for line in text.splitlines():
        raw = line.strip()
        if raw.startswith("### "):
            title = re.sub(r"\s*\(source:.*?\)\s*$", "", raw[4:], flags=re.I).strip()
            title = re.sub(r"\s+", " ", title)[:160]
            if len(title) >= 8 and title not in titled:
                titled.append(title)
            if len(titled) >= limit:
                return titled
            continue

        s = raw.lstrip("-•*").strip()
        if len(s) < 8:
            continue
        if s.lower().startswith(("http", "note", "warning", "total", "##")):
            continue
        if re.match(r"^-?\d+(\.\d+)?\s+\d{6}\b", s):
            continue
        s = re.sub(r"\s+", " ", s)[:160]
        if s not in fallback:
            fallback.append(s)

    out = list(titled)
    for s in fallback:
        if len(out) >= limit:
            break
        if s not in out:
            out.append(s)
    return out[:limit]
