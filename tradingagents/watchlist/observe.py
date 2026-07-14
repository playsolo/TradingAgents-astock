"""对单只观察标的执行一次复核（轻量或完整分析过期升级）。"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from tradingagents.watchlist.calendar import is_full_analysis_stale
from tradingagents.watchlist.judge import briefing_from_judgment, judge_vs_baseline
from tradingagents.watchlist.models import LEAN_LABELS, Alert, WatchItem
from tradingagents.watchlist.refresh import run_full_refresh_analysis
from tradingagents.watchlist.rules import detect_changes
from tradingagents.watchlist.service import refresh_from_analysis, resolve_log_path
from tradingagents.watchlist.snapshot import fetch_snapshot
from tradingagents.watchlist.store import WatchlistStore

logger = logging.getLogger(__name__)

_STALE_REFRESH_FAIL_KIND = "stale_refresh"


def observe_item(
    item: WatchItem,
    *,
    store: WatchlistStore,
    llm: Any = None,
    slot_key: str | None = None,
    now: datetime | None = None,
    force: bool = False,
    analysis_config: dict[str, Any] | None = None,
) -> list[Alert]:
    """抓快照 → LLM 轻判 → 规则 diff → 持久化告警与 briefing。返回本次新增 alerts。

    若 Baseline.trade_date 距今超过完整分析过期阈值且提供 analysis_config，
    则在旧基准先验上跑一轮完整分析并回写基准（方案 A）。

    force=True 时忽略 enabled（用于「立即观察一次」手动触发）。
    """
    if not item.enabled and not force:
        return []
    if item.baseline.market != "CN":
        logger.info("skip non-CN watch item %s", item.baseline.ticker)
        return []

    dt = now or datetime.now()
    observed_at = dt.isoformat(timespec="seconds")
    ticker = item.baseline.ticker

    if slot_key and item.last_slot_key == slot_key:
        return []  # 本时段已跑过

    if analysis_config is not None and is_full_analysis_stale(
        item.baseline.trade_date, dt
    ):
        return _full_refresh_observe(
            item,
            store=store,
            llm=llm,
            slot_key=slot_key,
            observed_at=observed_at,
            trade_date=dt.strftime("%Y-%m-%d"),
            analysis_config=analysis_config,
        )

    return _light_observe(
        item,
        store=store,
        llm=llm,
        slot_key=slot_key,
        observed_at=observed_at,
    )


def _full_refresh_observe(
    item: WatchItem,
    *,
    store: WatchlistStore,
    llm: Any,
    slot_key: str | None,
    observed_at: str,
    trade_date: str,
    analysis_config: dict[str, Any],
) -> list[Alert]:
    ticker = item.baseline.ticker
    logger.info(
        "watchlist full refresh for %s (baseline %s → %s)",
        ticker,
        item.baseline.trade_date,
        trade_date,
    )
    try:
        state = run_full_refresh_analysis(
            item, trade_date=trade_date, config=analysis_config
        )
    except Exception as exc:
        logger.exception("full refresh failed for %s; falling back to light observe", ticker)
        store.append_alerts(
            ticker,
            [
                Alert(
                    kind=_STALE_REFRESH_FAIL_KIND,
                    title="完整再分析失败",
                    detail=f"已回退轻量观察：{exc}",
                    observed_at=observed_at,
                )
            ],
        )
        # 不写入 slot_key，便于同窗或下一窗重试完整升级
        return _light_observe(
            item,
            store=store,
            llm=llm,
            slot_key=None,
            observed_at=observed_at,
        )

    log_path = resolve_log_path(ticker, trade_date)
    refreshed = refresh_from_analysis(
        state,
        ticker=ticker,
        trade_date=trade_date,
        log_path=log_path,
        store=store,
    )
    stance = refreshed.baseline.stance
    summary = (
        f"完整再分析已完成（基准日 {trade_date}）| 立场 {stance}"
        + (
            f" | {refreshed.baseline.thesis_summary}"
            if refreshed.baseline.thesis_summary
            else ""
        )
    )
    store.mark_observed(
        ticker,
        observed_at,
        slot_key=slot_key,
        summary=summary,
        briefing=None,
    )
    return []


def _light_observe(
    item: WatchItem,
    *,
    store: WatchlistStore,
    llm: Any,
    slot_key: str | None,
    observed_at: str,
) -> list[Alert]:
    ticker = item.baseline.ticker
    snapshot = fetch_snapshot(ticker)
    judgment = judge_vs_baseline(
        item.baseline,
        snapshot,
        llm=llm,
        as_of=observed_at[:10],
    )
    alerts = detect_changes(
        item.baseline,
        snapshot,
        suggested_stance=str(judgment.get("suggested_stance") or item.baseline.stance),
        suggested_position_pct=judgment.get("suggested_position_pct"),
        new_major_risks=list(judgment.get("new_major_risks") or []),
        observed_at=observed_at,
    )

    briefing = briefing_from_judgment(judgment)
    lean_label = LEAN_LABELS.get(briefing.lean, "中性")
    summary_parts = [str(judgment.get("summary") or "").strip()]
    if briefing.market_brief:
        summary_parts.insert(0, f"盘面：{briefing.market_brief}")
    summary_parts.append(f"今日倾向：{lean_label}")
    if briefing.lean_reason:
        summary_parts.append(briefing.lean_reason)
    if judgment.get("watch_point"):
        summary_parts.append(f"今日只看：{judgment['watch_point']}")
    if judgment.get("avoid"):
        summary_parts.append(f"不做：{judgment['avoid']}")
    summary = " | ".join(p for p in summary_parts if p)

    if alerts:
        store.append_alerts(ticker, alerts)
    store.mark_observed(
        ticker,
        observed_at,
        slot_key=slot_key,
        summary=summary or None,
        briefing=briefing,
    )
    return alerts


def observe_all(
    store: WatchlistStore,
    *,
    llm: Any = None,
    slot_key: str | None = None,
    analysis_config: dict[str, Any] | None = None,
) -> dict[str, list[Alert]]:
    """跑一遍全部 enabled 标的。"""
    results: dict[str, list[Alert]] = {}
    for item in store.list_items():
        if not item.enabled:
            continue
        try:
            results[item.baseline.ticker] = observe_item(
                item,
                store=store,
                llm=llm,
                slot_key=slot_key,
                analysis_config=analysis_config,
            )
        except Exception as e:
            logger.exception("observe failed for %s: %s", item.baseline.ticker, e)
            results[item.baseline.ticker] = []
    return results
