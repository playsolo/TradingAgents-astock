"""分析模式路由：硬闸 → 全量；窄口径 → 伪增量/扫描跳过；不确定默认全量。

偏风控（怕漏翻盘）：取价失败、无锚点、灰区一律 full_reeval。
伪增量注入的是「最近一次全量校准锚点」，不是上一次伪增量结论。

扫描入队（source=scan）在窄口径下走 skip_reuse：不跑深分析，沿用校准。
手工分析即使窄口径仍跑伪增量图（用户点了「开始分析」即期望出报告）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable

from tradingagents.analysis.calibration import CalibrationStore, default_calibration_store
from tradingagents.watchlist.calendar import (
    FULL_ANALYSIS_STALE_TRADING_DAYS,
    cn_trading_days_since,
)
from tradingagents.watchlist.models import Baseline
from tradingagents.watchlist.service import prior_context_from_baseline

logger = logging.getLogger(__name__)

MODE_FULL = "full_reeval"
MODE_INCREMENTAL = "pseudo_incremental"
MODE_SKIP = "skip_reuse"
MODE_AUTO = "auto"

SOURCE_MANUAL = "manual"
SOURCE_SCAN = "scan"

HIGH_PRIORITY_ALERT_KINDS = frozenset({"stop_loss", "stance", "risk"})


@dataclass(frozen=True)
class AnalysisRouteDecision:
    mode: str
    reason: str
    extra_past_context: str = ""
    anchor: Baseline | None = None

    @property
    def updates_calibration(self) -> bool:
        """全量（非 resume）成功后应写入新的校准锚点。"""
        return self.mode == MODE_FULL and self.reason != "resume"

    @property
    def skips_deep_analysis(self) -> bool:
        return self.mode == MODE_SKIP


def build_calibration_prior(baseline: Baseline) -> str:
    """伪增量 prior：标明校准锚点，避免与观察池措辞混淆。"""
    text = prior_context_from_baseline(baseline)
    lines = text.splitlines()
    if lines and lines[0].startswith("[观察池基准"):
        rest = lines[0].split("|", 1)[-1].strip().rstrip("]")
        lines[0] = f"[校准锚点 | {rest}]"
    else:
        lines.insert(0, f"[校准锚点 | {baseline.ticker} | 分析日 {baseline.trade_date}]")
    return "\n".join(lines)


def _seed_anchor_from_history(
    ticker: str,
    market: str,
    *,
    calibration_store: CalibrationStore,
) -> Baseline | None:
    """无校准/观察池时，用最近一次完成报告回填校准锚点。"""
    try:
        from tradingagents.analysis.persist import seed_calibration_from_history

        return seed_calibration_from_history(
            ticker, market=market, store=calibration_store
        )
    except Exception:  # noqa: BLE001
        logger.exception("seed calibration from history failed for %s", ticker)
        return None


def _resolve_anchor(
    ticker: str,
    market: str,
    *,
    calibration_store: CalibrationStore,
    watch_store: Any | None,
    seed_from_history: bool = True,
) -> Baseline | None:
    anchor = calibration_store.get(ticker, market)
    if anchor is not None:
        return anchor
    if watch_store is not None:
        try:
            item = watch_store.get(ticker)
        except Exception:  # noqa: BLE001
            logger.exception("watch_store.get failed for %s", ticker)
            item = None
        if item is not None:
            return item.baseline
    if seed_from_history:
        return _seed_anchor_from_history(
            ticker, market, calibration_store=calibration_store
        )
    return None


def _high_priority_alerts(watch_store: Any | None, ticker: str) -> bool:
    if watch_store is None:
        return False
    try:
        item = watch_store.get(ticker)
    except Exception:  # noqa: BLE001
        return False
    if item is None:
        return False
    return any(a.kind in HIGH_PRIORITY_ALERT_KINDS for a in (item.alerts or []))


def _fetch_current_price(ticker: str, market: str) -> float | None:
    try:
        from tradingagents.watchlist.snapshot import fetch_snapshot

        snap = fetch_snapshot(ticker, market=market, max_headlines=0)
        price = float(snap.price or 0)
        return price if price > 0 else None
    except Exception:  # noqa: BLE001
        logger.warning("price fetch failed for %s (%s)", ticker, market, exc_info=True)
        return None


def resolve_analysis_mode(
    *,
    ticker: str,
    trade_date: str,
    market: str = "CN",
    force_full_reeval: bool = False,
    analysis_mode: str = MODE_AUTO,
    source: str = SOURCE_MANUAL,
    fresh: bool = True,
    as_of: datetime | date | str | None = None,
    calibration_store: CalibrationStore | None = None,
    watch_store: Any | None = None,
    current_price: float | None = None,
    fetch_price: bool = True,
    price_threshold_pct: float = 5.0,
    price_soft_threshold_pct: float = 3.0,
    max_calibration_age_trading_days: int = FULL_ANALYSIS_STALE_TRADING_DAYS,
    price_fetcher: Callable[[str, str], float | None] | None = None,
    seed_from_history: bool = True,
    gray_zone_llm: Any | None = None,
    gray_zone_headlines: list[str] | None = None,
) -> AnalysisRouteDecision:
    """Decide full_reeval / pseudo_incremental / skip_reuse.

    Conservative default: when uncertain → full_reeval (empty extra context).
    Gray zone (soft < |move| <= hard): LLM may allow reuse; else full.
    """
    market = (market or "CN").upper()
    ticker = (ticker or "").strip().upper()
    mode_req = (analysis_mode or MODE_AUTO).strip().lower()
    src = (source or SOURCE_MANUAL).strip().lower() or SOURCE_MANUAL
    cal = calibration_store or default_calibration_store()
    when = as_of if as_of is not None else trade_date

    if not fresh:
        return AnalysisRouteDecision(mode=MODE_FULL, reason="resume")

    if force_full_reeval or mode_req == MODE_FULL:
        return AnalysisRouteDecision(
            mode=MODE_FULL,
            reason="force" if force_full_reeval else "explicit_full",
        )

    anchor = _resolve_anchor(
        ticker,
        market,
        calibration_store=cal,
        watch_store=watch_store,
        seed_from_history=seed_from_history,
    )

    if mode_req == MODE_INCREMENTAL:
        if anchor is None:
            return AnalysisRouteDecision(mode=MODE_FULL, reason="no_anchor")
        return AnalysisRouteDecision(
            mode=MODE_INCREMENTAL,
            reason="explicit_incremental",
            extra_past_context=build_calibration_prior(anchor),
            anchor=anchor,
        )

    if anchor is None:
        return AnalysisRouteDecision(mode=MODE_FULL, reason="no_anchor")

    age = cn_trading_days_since(anchor.trade_date, when)
    if age > max(1, int(max_calibration_age_trading_days)):
        return AnalysisRouteDecision(
            mode=MODE_FULL, reason="stale_calibration", anchor=anchor
        )

    if _high_priority_alerts(watch_store, ticker):
        return AnalysisRouteDecision(mode=MODE_FULL, reason="watch_alert", anchor=anchor)

    price = current_price
    if price is None and fetch_price:
        fetcher = price_fetcher or _fetch_current_price
        price = fetcher(ticker, market)

    if price is None or price <= 0:
        return AnalysisRouteDecision(mode=MODE_FULL, reason="no_price", anchor=anchor)

    if anchor.stop_loss is not None and price < float(anchor.stop_loss):
        return AnalysisRouteDecision(mode=MODE_FULL, reason="stop_loss", anchor=anchor)

    move_pct: float | None = None
    if anchor.baseline_price is not None and float(anchor.baseline_price) > 0:
        move_pct = (
            abs(price - float(anchor.baseline_price))
            / float(anchor.baseline_price)
            * 100.0
        )
        hard = float(price_threshold_pct)
        soft = float(price_soft_threshold_pct)
        if soft > hard:
            soft = hard
        if move_pct > hard:
            return AnalysisRouteDecision(
                mode=MODE_FULL, reason="price_move", anchor=anchor
            )
        if move_pct > soft:
            # Gray zone: soft < move <= hard → LLM vote; no/uncertain → full.
            vote = _gray_zone_vote(
                anchor=anchor,
                current_price=float(price),
                move_pct=float(move_pct),
                gray_zone_llm=gray_zone_llm,
                headlines=gray_zone_headlines,
            )
            if vote is None or vote.prefers_full:
                reason = "gray_zone"
                if vote is not None and vote.reason:
                    reason = f"gray_zone:{vote.decision}"
                return AnalysisRouteDecision(
                    mode=MODE_FULL, reason=reason, anchor=anchor
                )
            # vote reuse with enough confidence → fall through to narrow path

    prior = build_calibration_prior(anchor)
    if src == SOURCE_SCAN:
        return AnalysisRouteDecision(
            mode=MODE_SKIP,
            reason="narrow_ok_scan_skip",
            extra_past_context=prior,
            anchor=anchor,
        )
    return AnalysisRouteDecision(
        mode=MODE_INCREMENTAL,
        reason="narrow_ok",
        extra_past_context=prior,
        anchor=anchor,
    )


def _gray_zone_vote(
    *,
    anchor: Baseline,
    current_price: float,
    move_pct: float,
    gray_zone_llm: Any | None,
    headlines: list[str] | None,
):
    from tradingagents.analysis.gray_zone import GrayZoneVote, ask_gray_zone_llm

    if gray_zone_llm is None:
        return GrayZoneVote(decision="uncertain", reason="no_llm")
    return ask_gray_zone_llm(
        llm=gray_zone_llm,
        anchor=anchor,
        current_price=current_price,
        move_pct=move_pct,
        headlines=headlines,
    )


def apply_route_to_job_fields(
    *,
    force_full_reeval: bool = False,
    analysis_mode: str = MODE_AUTO,
    source: str = SOURCE_MANUAL,
) -> dict[str, Any]:
    """Normalize fields persisted on AnalysisJob / start_request."""
    mode = (analysis_mode or MODE_AUTO).strip().lower()
    if mode not in {MODE_AUTO, MODE_FULL, MODE_INCREMENTAL}:
        mode = MODE_AUTO
    src = (source or SOURCE_MANUAL).strip().lower() or SOURCE_MANUAL
    if src not in {SOURCE_MANUAL, SOURCE_SCAN}:
        src = SOURCE_MANUAL
    return {
        "force_full_reeval": bool(force_full_reeval),
        "analysis_mode": mode,
        "source": src,
    }
