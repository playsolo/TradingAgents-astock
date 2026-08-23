"""HiThink data helpers for strategy scan funnels (Phase 2).

Batch-friendly cache: one hot list + market regime + dragon-tiger board per scan,
then per-stock auction lookups in batches of ≤100.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from tradingagents.dataflows.feature_snapshot import build_market_regime_snapshot
from tradingagents.dataflows.hithink_client import (
    HiThinkAPIError,
    HiThinkClient,
    code_to_thscode,
    get_hithink_client,
    is_hithink_enabled,
)

logger = logging.getLogger(__name__)

_AUCTION_VOL_RATIO_MIN = 1.2
_DRAGON_TIGER_INST_MIN_WAN = 100.0  # 100 万元
_PS_TTM_MAX_VALUE = 20.0  # value-swing L1b soft cap when HiThink available
_MARKET_FRAGILE_BREAK_RATIO = 0.40  # 炸板/涨停 ≥ 此比例 → 全市场 −1 分

_cache_lock = threading.Lock()
_scan_cache: HiThinkScanCache | None = None


@dataclass
class HiThinkScanCache:
    scan_date: str
    market_limit_up: int | None = None
    market_limit_down: int | None = None
    market_limit_break: int | None = None
    market_penalty: int = 0
    hot_rank_by_code: dict[str, int] = field(default_factory=dict)
    skyrocket_codes: set[str] = field(default_factory=set)
    dragon_tiger_org_wan: dict[str, float] = field(default_factory=dict)
    dragon_tiger_net_wan: dict[str, float] = field(default_factory=dict)
    auction_volume_ratio: dict[str, float] = field(default_factory=dict)
    auction_pct: dict[str, float] = field(default_factory=dict)
    valuations: dict[str, dict[str, Any]] = field(default_factory=dict)

    def hot_rank(self, code: str) -> int | None:
        return self.hot_rank_by_code.get(code)

    def is_skyrocket(self, code: str) -> bool:
        return code in self.skyrocket_codes


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _wan_from_raw(amount: float | int | None) -> float | None:
    if amount is None:
        return None
    try:
        v = float(amount)
    except (TypeError, ValueError):
        return None
    # Heuristic: large absolute values are likely yuan → 万元
    if abs(v) >= 10_000:
        return v / 10_000.0
    return v


def clear_hithink_scan_cache() -> None:
    global _scan_cache
    with _cache_lock:
        _scan_cache = None


def build_hithink_scan_cache(
    codes: list[str] | None = None,
    *,
    client: HiThinkClient | None = None,
    fetch_valuations: bool = False,
) -> HiThinkScanCache | None:
    """Build or extend same-day scan cache. ``codes`` drives auction/valuation batches."""
    global _scan_cache
    if not is_hithink_enabled():
        return None

    today = _today()
    cli = client or get_hithink_client()
    if not cli.available:
        return None

    with _cache_lock:
        if _scan_cache is not None and _scan_cache.scan_date == today:
            cache = _scan_cache
        else:
            cache = HiThinkScanCache(scan_date=today)
            _scan_cache = cache

    if cache.market_limit_up is None and cache.market_limit_down is None:
        try:
            regime = build_market_regime_snapshot(client=cli)
            if regime:
                cache.market_limit_up = regime.limit_up_count
                cache.market_limit_down = regime.limit_down_count
                cache.market_limit_break = regime.limit_break_count
                up = regime.limit_up_count or 0
                br = regime.limit_break_count or 0
                if up > 0 and br / up >= _MARKET_FRAGILE_BREAK_RATIO:
                    cache.market_penalty = -1
                for row in regime.hot_top:
                    ticker = str(row.get("ticker") or "").strip()
                    rank = row.get("rank")
                    if ticker and rank is not None:
                        cache.hot_rank_by_code[ticker] = int(rank)
        except Exception as exc:
            logger.warning("HiThink scan: market regime failed: %s", exc)

    if not cache.skyrocket_codes:
        try:
            for row in cli.skyrocket_list(period="day"):
                ticker = str(row.get("ticker") or "").strip()
                if ticker:
                    cache.skyrocket_codes.add(ticker)
        except HiThinkAPIError as exc:
            logger.warning("HiThink scan: skyrocket failed: %s", exc)

    if not cache.dragon_tiger_org_wan:
        try:
            dt = cli.dragon_tiger_list(board_type="all")
            for row in dt.get("stock_items") or []:
                ticker = str(row.get("ticker") or "").strip()
                if not ticker:
                    continue
                org_wan = _wan_from_raw(row.get("org_net_value"))
                net_wan = _wan_from_raw(row.get("net_value"))
                if org_wan is not None:
                    cache.dragon_tiger_org_wan[ticker] = org_wan
                if net_wan is not None:
                    cache.dragon_tiger_net_wan[ticker] = net_wan
        except HiThinkAPIError as exc:
            logger.warning("HiThink scan: dragon-tiger failed: %s", exc)

    if codes:
        need_auction = [
            c for c in codes if c not in cache.auction_volume_ratio and c not in cache.auction_pct
        ]
        if need_auction:
            thscodes = [code_to_thscode(c) for c in need_auction]
            for i in range(0, len(thscodes), 100):
                chunk = thscodes[i : i + 100]
                try:
                    rows = cli.auction_snapshot(chunk)
                    for row in rows:
                        ticker = str(row.get("ticker") or "").strip()
                        if not ticker:
                            continue
                        vr = row.get("auction_volume_ratio")
                        ap = row.get("auction_pct")
                        if vr is not None:
                            try:
                                cache.auction_volume_ratio[ticker] = float(vr)
                            except (TypeError, ValueError):
                                pass
                        if ap is not None:
                            try:
                                cache.auction_pct[ticker] = float(ap)
                            except (TypeError, ValueError):
                                pass
                except HiThinkAPIError as exc:
                    logger.warning("HiThink scan: auction batch failed: %s", exc)

        if fetch_valuations:
            need_val = [c for c in codes if c not in cache.valuations]
            if need_val:
                thscodes = [code_to_thscode(c) for c in need_val]
                for i in range(0, len(thscodes), 100):
                    chunk = thscodes[i : i + 100]
                    try:
                        rows = cli.valuations_snapshot(chunk)
                        for row in rows:
                            ticker = str(row.get("ticker") or "").strip()
                            if ticker:
                                cache.valuations[ticker] = row
                    except HiThinkAPIError as exc:
                        logger.warning("HiThink scan: valuations batch failed: %s", exc)

    return cache


def auction_fund_flow_proxy(cache: HiThinkScanCache | None, code: str) -> float | None:
    """Positive proxy when auction volume ratio and pct suggest inflow."""
    if cache is None:
        return None
    vr = cache.auction_volume_ratio.get(code)
    ap = cache.auction_pct.get(code)
    if vr is None or ap is None:
        return None
    if vr >= _AUCTION_VOL_RATIO_MIN and ap > 0:
        return vr
    return None


def dragon_tiger_inst_net_wan(cache: HiThinkScanCache | None, code: str) -> float | None:
    if cache is None:
        return None
    return cache.dragon_tiger_org_wan.get(code)


def ps_ttm(cache: HiThinkScanCache | None, code: str) -> float | None:
    if cache is None:
        return None
    row = cache.valuations.get(code) or {}
    val = row.get("ps_ttm")
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def merge_hithink_hot_topics(hot_stocks: dict[str, list[str]]) -> dict[str, list[str]]:
    """Augment 同花顺热股题材 dict with HiThink hot-list membership."""
    cache = build_hithink_scan_cache()
    if cache is None:
        return hot_stocks
    merged = {k: list(v) for k, v in hot_stocks.items()}
    for code, rank in cache.hot_rank_by_code.items():
        tag = f"HiThink热榜#{rank}"
        merged.setdefault(tag, [])
        if code not in merged[tag]:
            merged[tag].append(code)
    return merged


def market_regime_summary(cache: HiThinkScanCache | None) -> str:
    if cache is None:
        return ""
    parts = [
        f"涨停{cache.market_limit_up}",
        f"跌停{cache.market_limit_down}",
        f"炸板{cache.market_limit_break}",
    ]
    if cache.market_penalty:
        parts.append("市场脆弱(炸板偏高,候选−1)")
    return " | ".join(parts)


def value_swing_ps_ttm_too_high(cache: HiThinkScanCache | None, code: str) -> bool:
    ps = ps_ttm(cache, code)
    return ps is not None and ps > _PS_TTM_MAX_VALUE
