"""Aggregated feature snapshots from HiThink API (+ derived ratios).

Phase 0: cache TTL snapshots for single-stock and market-wide context.
Agents and scan funnels can consume the same structures in later phases.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from .hithink_client import (
    HiThinkAPIError,
    HiThinkClient,
    code_to_thscode,
    get_hithink_client,
    is_hithink_enabled,
)
from .utils import safe_ticker_component

logger = logging.getLogger(__name__)

_TTL_SECONDS = 300
_cache_lock = threading.RLock()
_cache: dict[str, tuple[float, Any]] = {}


def _cache_get(key: str) -> Any | None:
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        ts, value = entry
        if time.time() - ts >= _TTL_SECONDS:
            del _cache[key]
            return None
        return value


def _cache_set(key: str, value: Any) -> None:
    with _cache_lock:
        _cache[key] = (time.time(), value)


@dataclass
class StockFeatureSnapshot:
    code: str
    thscode: str
    retrieved_at: str
    source: str = "hithink-finance"
    valuation: dict[str, Any] = field(default_factory=dict)
    hot_rank: int | None = None
    hot_heat: float | None = None
    hot_rank_change: int | None = None
    in_skyrocket: bool = False
    anomaly_tags: list[str] = field(default_factory=list)
    anomaly_keywords: list[str] = field(default_factory=list)
    anomaly_content: str = ""
    auction: dict[str, Any] = field(default_factory=dict)
    financial_quality: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MarketRegimeSnapshot:
    retrieved_at: str
    source: str = "hithink-finance"
    hot_top: list[dict[str, Any]] = field(default_factory=list)
    skyrocket_top: list[dict[str, Any]] = field(default_factory=list)
    limit_up_count: int | None = None
    limit_down_count: int | None = None
    limit_break_count: int | None = None
    max_continue_board: int | None = None
    dragon_tiger_stock_count: int | None = None
    org_net_total: float | None = None
    hot_money_net_total: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _pagination_total(pool: dict[str, Any]) -> int | None:
    pag = pool.get("pagination") if isinstance(pool, dict) else None
    if isinstance(pag, dict) and pag.get("total") is not None:
        try:
            return int(pag["total"])
        except (TypeError, ValueError):
            return None
    items = pool.get("item") if isinstance(pool, dict) else None
    if isinstance(items, list):
        return len(items)
    return None


def _compute_financial_quality(
    income_rows: list[dict[str, Any]],
    balance_rows: list[dict[str, Any]],
    cash_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Derive cash-conversion style ratios from latest aligned periods."""
    out: dict[str, Any] = {}
    if not income_rows:
        return out
    inc = income_rows[0]
    bal = balance_rows[0] if balance_rows else {}
    cf = cash_rows[0] if cash_rows else {}

    net_profit = inc.get("net_profit") or inc.get("parent_holder_net_profit")
    ocf = cf.get("act_cash_flow_net")
    revenue = inc.get("operating_income")
    capex = cf.get("pay_fixed_assets_etc_cash")
    assets = bal.get("assets_total")
    receivable = bal.get("accounts_receivable")
    cash = bal.get("cash")
    debt = bal.get("total_debt")

    def _ratio(num, den, label: str) -> None:
        if num is None or den in (None, 0):
            return
        try:
            out[label] = round(float(num) / float(den), 4)
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    _ratio(ocf, net_profit, "cash_conversion_ratio")
    if ocf is not None and capex is not None and revenue not in (None, 0):
        try:
            fcf = float(ocf) - float(capex or 0)
            out["fcf_margin"] = round(fcf / float(revenue), 4)
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    if net_profit is not None and ocf is not None and assets not in (None, 0):
        try:
            out["accrual_ratio"] = round(
                (float(net_profit) - float(ocf)) / float(assets), 4
            )
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    _ratio(receivable, revenue, "receivable_pressure")
    if cash is not None and debt is not None and assets not in (None, 0):
        try:
            out["net_cash_ratio"] = round(
                (float(cash) - float(debt)) / float(assets), 4
            )
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    return out


def build_stock_feature_snapshot(
    symbol: str,
    *,
    client: HiThinkClient | None = None,
    include_financials: bool = True,
) -> StockFeatureSnapshot | None:
    """Build a cached per-stock feature bundle. Returns None when HiThink disabled."""
    if not is_hithink_enabled():
        return None

    code = safe_ticker_component(symbol)
    cache_key = f"stock_features:{code}:{include_financials}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    cli = client or get_hithink_client()
    if not cli.available:
        return None

    thscode = code_to_thscode(code)
    snap = StockFeatureSnapshot(
        code=code,
        thscode=thscode,
        retrieved_at=_now_iso(),
    )

    try:
        valuations = cli.valuations_snapshot([thscode])
        if valuations:
            snap.valuation = valuations[0]
    except HiThinkAPIError as exc:
        logger.warning("HiThink valuation failed for %s: %s", code, exc)

    try:
        hot_list = cli.hot_stock_list(period="day")
        for row in hot_list:
            if row.get("thscode") == thscode or row.get("ticker") == code:
                snap.hot_rank = row.get("rank")
                snap.hot_heat = row.get("heat")
                snap.hot_rank_change = row.get("rank_change")
                break
    except HiThinkAPIError as exc:
        logger.warning("HiThink hot list failed: %s", exc)

    try:
        sky = cli.skyrocket_list(period="day")
        snap.in_skyrocket = any(
            r.get("thscode") == thscode or r.get("ticker") == code for r in sky
        )
    except HiThinkAPIError as exc:
        logger.warning("HiThink skyrocket list failed: %s", exc)

    try:
        anomalies = cli.anomaly_analysis_stock([thscode])
        if anomalies:
            row = anomalies[0]
            tag = row.get("tag_name") or ""
            snap.anomaly_tags = [tag] if tag else []
            snap.anomaly_keywords = list(row.get("keyword_list") or [])
            snap.anomaly_content = str(row.get("analysis_content") or "")
    except HiThinkAPIError as exc:
        logger.warning("HiThink anomaly failed for %s: %s", code, exc)

    try:
        auction_rows = cli.auction_snapshot([thscode])
        if auction_rows:
            snap.auction = auction_rows[0]
    except HiThinkAPIError as exc:
        logger.warning("HiThink auction failed for %s: %s", code, exc)

    if include_financials:
        try:
            income = cli.income_statements(thscode, period="quarterly", limit=4)
            balance = cli.balance_sheets(thscode, period="quarterly", limit=4)
            cashflow = cli.cash_flow_statements(thscode, period="quarterly", limit=4)
            snap.financial_quality = _compute_financial_quality(
                income, balance, cashflow
            )
        except HiThinkAPIError as exc:
            logger.warning("HiThink financials failed for %s: %s", code, exc)

    _cache_set(cache_key, snap)
    return snap


def build_market_regime_snapshot(
    *,
    client: HiThinkClient | None = None,
) -> MarketRegimeSnapshot | None:
    """Market-wide sentiment / limit-up structure snapshot."""
    if not is_hithink_enabled():
        return None

    cache_key = "market_regime"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    cli = client or get_hithink_client()
    if not cli.available:
        return None

    snap = MarketRegimeSnapshot(retrieved_at=_now_iso())

    try:
        hot = cli.hot_stock_list(period="day")
        snap.hot_top = hot[:15]
    except HiThinkAPIError as exc:
        logger.warning("HiThink hot list failed: %s", exc)

    try:
        sky = cli.skyrocket_list(period="day")
        snap.skyrocket_top = sky[:15]
    except HiThinkAPIError as exc:
        logger.warning("HiThink skyrocket failed: %s", exc)

    try:
        up_pool = cli.limit_up_pool(size=1, page=1)
        snap.limit_up_count = _pagination_total(up_pool)
        items = up_pool.get("item") or []
        if items:
            snap.max_continue_board = max(
                int(i.get("continue_day_cnt") or 0) for i in items
            )
    except HiThinkAPIError as exc:
        logger.warning("HiThink limit-up pool failed: %s", exc)

    try:
        down_pool = cli.limit_down_pool(size=1, page=1)
        snap.limit_down_count = _pagination_total(down_pool)
    except HiThinkAPIError as exc:
        logger.warning("HiThink limit-down pool failed: %s", exc)

    try:
        break_pool = cli.limit_break_pool(size=1, page=1)
        snap.limit_break_count = _pagination_total(break_pool)
    except HiThinkAPIError as exc:
        logger.warning("HiThink limit-break pool failed: %s", exc)

    try:
        dt = cli.dragon_tiger_list(board_type="all")
        snap.dragon_tiger_stock_count = dt.get("stock_count")
        org_total = 0.0
        hm_total = 0.0
        for row in dt.get("stock_items") or []:
            try:
                org_total += float(row.get("org_net_value") or 0)
            except (TypeError, ValueError):
                pass
            try:
                hm_total += float(row.get("hot_money_net_value") or 0)
            except (TypeError, ValueError):
                pass
        snap.org_net_total = round(org_total, 2)
        snap.hot_money_net_total = round(hm_total, 2)
    except HiThinkAPIError as exc:
        logger.warning("HiThink dragon-tiger failed: %s", exc)

    _cache_set(cache_key, snap)
    return snap


def format_stock_features_text(snap: StockFeatureSnapshot) -> str:
    """Human-readable block for agent prompts / tool output."""
    lines = [
        f"# HiThink Feature Snapshot | {snap.code} ({snap.thscode})",
        f"# Retrieved: {snap.retrieved_at}",
        f"# Source: {snap.source}",
        "",
    ]
    if snap.valuation:
        v = snap.valuation
        lines.append("## Valuation")
        for key in ("pe_ttm", "pe_mrq", "pb_mrq", "ps_ttm", "pcf_ttm"):
            if v.get(key) is not None:
                lines.append(f"  {key}: {v[key]}")
    if snap.hot_rank is not None:
        lines.append(
            f"\n## Attention\n"
            f"  hot_rank: {snap.hot_rank} | heat: {snap.hot_heat} | "
            f"rank_change: {snap.hot_rank_change}"
        )
    if snap.in_skyrocket:
        lines.append("  skyrocket_list: yes")
    if snap.anomaly_tags or snap.anomaly_content:
        lines.append("\n## Anomaly (today)")
        if snap.anomaly_tags:
            lines.append(f"  tags: {', '.join(snap.anomaly_tags)}")
        if snap.anomaly_keywords:
            lines.append(f"  keywords: {', '.join(snap.anomaly_keywords)}")
        if snap.anomaly_content:
            snippet = snap.anomaly_content[:400]
            lines.append(f"  content: {snippet}")
    if snap.auction:
        a = snap.auction
        lines.append("\n## Auction")
        for key in (
            "auction_pct",
            "auction_volume_ratio",
            "auction_turnover_pct",
            "auction_amount",
        ):
            if a.get(key) is not None:
                lines.append(f"  {key}: {a[key]}")
    if snap.financial_quality:
        lines.append("\n## Financial quality (derived)")
        for k, val in snap.financial_quality.items():
            lines.append(f"  {k}: {val}")
    return "\n".join(lines)


def format_market_regime_text(snap: MarketRegimeSnapshot) -> str:
    lines = [
        f"# HiThink Market Regime | {snap.retrieved_at}",
        f"# Source: {snap.source}",
        "",
        "## Structure",
        f"  limit_up_count: {snap.limit_up_count}",
        f"  limit_down_count: {snap.limit_down_count}",
        f"  limit_break_count: {snap.limit_break_count}",
        f"  max_continue_board (sample): {snap.max_continue_board}",
        "",
        "## Dragon-Tiger (latest)",
        f"  stock_count: {snap.dragon_tiger_stock_count}",
        f"  org_net_total: {snap.org_net_total}",
        f"  hot_money_net_total: {snap.hot_money_net_total}",
    ]
    if snap.hot_top:
        lines.append("\n## Hot stocks (top 5)")
        for row in snap.hot_top[:5]:
            lines.append(
                f"  {row.get('rank')}. {row.get('ticker')} {row.get('name')} "
                f"heat={row.get('heat')}"
            )
    return "\n".join(lines)
