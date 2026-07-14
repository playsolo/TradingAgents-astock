"""抓取观察用行情快照与近期标题（轻量，不跑完整 Agent）。"""

from __future__ import annotations

import logging
import re
from datetime import datetime

from tradingagents.watchlist.models import MarketSnapshot

logger = logging.getLogger(__name__)


def fetch_snapshot(ticker: str, *, max_headlines: int = 8) -> MarketSnapshot:
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
        from datetime import timedelta

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
