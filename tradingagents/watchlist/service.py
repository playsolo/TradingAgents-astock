"""UI / 外部调用的观察池服务入口。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from tradingagents.watchlist.baseline import extract_baseline
from tradingagents.watchlist.models import Baseline, WatchItem
from tradingagents.watchlist.store import WatchlistStore, default_store

logger = logging.getLogger(__name__)


def prior_context_from_baseline(baseline: Baseline) -> str:
    """把冻结基准编成 past_context，供过期后完整再分析注入 PM。"""
    parts = [
        f"[观察池基准 | {baseline.ticker} | 分析日 {baseline.trade_date} | 立场 {baseline.stance}",
    ]
    if baseline.position_pct is not None:
        parts[0] += f" | 仓位 {baseline.position_pct:g}%"
    if baseline.baseline_price is not None:
        parts[0] += f" | 基准价 {baseline.baseline_price:g}"
    parts[0] += "]"
    if baseline.thesis_summary:
        parts.append(f"原逻辑：{baseline.thesis_summary}")
    if baseline.entry_price is not None:
        parts.append(f"入场价：{baseline.entry_price:g}")
    if baseline.stop_loss is not None:
        parts.append(f"止损：{baseline.stop_loss:g}")
    if baseline.major_risks:
        parts.append("原重大风险：" + "；".join(baseline.major_risks))
    parts.append("请在以上基准上做增量再评估，说明结论是否仍成立及需修正之处。")
    return "\n".join(parts)


def _infer_market(ticker: str, explicit: str | None = None) -> str:
    if explicit in {"CN", "US"}:
        return explicit
    code = (ticker or "").strip().upper()
    if code.isdigit() and len(code) == 6:
        return "CN"
    if any(ch.isalpha() for ch in code):
        return "US"
    return "CN"


def add_from_analysis(
    state: dict[str, Any],
    *,
    ticker: str,
    trade_date: str,
    log_path: str = "",
    store: WatchlistStore | None = None,
    market: str | None = None,
) -> WatchItem:
    """将一次分析结果加入观察池（覆盖同代码旧条目）。支持 CN / US。"""
    store = store or default_store()
    resolved = _infer_market(ticker, market)
    price = _current_price(ticker, market=resolved)
    baseline = extract_baseline(
        state,
        ticker=ticker,
        trade_date=trade_date,
        price=price,
        log_path=log_path,
        market=resolved,
    )
    item = WatchItem(baseline=baseline, enabled=True)
    store.add(item)
    return item


def refresh_from_analysis(
    state: dict[str, Any],
    *,
    ticker: str,
    trade_date: str,
    log_path: str = "",
    store: WatchlistStore | None = None,
    price: float | None = None,
    market: str | None = None,
) -> WatchItem:
    """用新一轮完整分析覆盖基准，保留 enabled / alerts / 观察摘要。"""
    store = store or default_store()
    existing = store.get(ticker)
    resolved = _infer_market(
        ticker,
        market or (existing.baseline.market if existing else None),
    )
    if price is None:
        price = _current_price(ticker, market=resolved)
    baseline = extract_baseline(
        state,
        ticker=ticker,
        trade_date=trade_date,
        price=price,
        log_path=log_path,
        market=resolved,
    )
    item = WatchItem(
        baseline=baseline,
        enabled=existing.enabled if existing else True,
        alerts=list(existing.alerts) if existing else [],
        # 基准已换新：旧轻量 briefing 作废，避免与新立场错位展示
        last_observed_at=None,
        last_slot_key=None,
        last_summary=None,
        last_briefing=None,
    )
    store.add(item)
    return item


def _current_price(ticker: str, *, market: str = "CN") -> float | None:
    try:
        if market == "US":
            from tradingagents.watchlist.snapshot import fetch_snapshot

            snap = fetch_snapshot(ticker, market="US")
            return snap.price if snap.price > 0 else None

        from tradingagents.dataflows import a_stock

        code = ticker.upper()
        q = a_stock._tencent_quote([code]).get(code) or {}
        # 顺带把中文名写入本地缓存，列表展示以后不用再拉
        name = str(q.get("name") or "").strip()
        if name:
            try:
                from web.stock_display import _NAME_CACHE

                _NAME_CACHE.set(code, name)
            except Exception:
                pass
        p = float(q.get("price") or 0)
        return p if p > 0 else None
    except Exception as e:
        logger.warning("price at watchlist add failed for %s: %s", ticker, e)
        return None


def resolve_log_path(ticker: str, trade_date: str) -> str:
    path = (
        Path.home()
        / ".tradingagents"
        / "logs"
        / ticker.upper()
        / "TradingAgentsStrategy_logs"
        / f"full_states_log_{trade_date}.json"
    )
    return str(path) if path.exists() else ""
