"""对单只观察标的执行一次复核（轻量快照 + 相对基准告警）。

操作建议时限过期后自动跳过；续命靠新报告加入/刷新基准，不再自动完整再分析。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from tradingagents.watchlist.calendar import is_action_validity_expired
from tradingagents.watchlist.judge import briefing_from_judgment, judge_vs_baseline
from tradingagents.watchlist.models import LEAN_LABELS, Alert, WatchItem
from tradingagents.watchlist.rules import detect_changes
from tradingagents.watchlist.snapshot import fetch_snapshot
from tradingagents.watchlist.store import WatchlistStore

logger = logging.getLogger(__name__)


def _mirror_alerts_to_inbox(ticker: str, alerts: list[Alert]) -> None:
    """Best-effort：把观察告警镜像到站内事件中心，失败不影响观察本身。"""
    if not alerts:
        return
    try:
        from tradingagents import inbox

        inbox.emit_watch_alerts(ticker, alerts)
    except Exception:  # noqa: BLE001 - inbox 是非关键旁路
        logger.warning("mirror watch alerts to inbox failed for %s", ticker, exc_info=True)


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

    操作建议时限已过则跳过（即使 ``force=True``）；续命请走新完整分析回写基准。
    ``analysis_config`` 保留签名兼容，已不再触发自动完整再分析。

    force=True 时忽略 enabled（用于「立即观察一次」手动触发）。
    """
    del analysis_config  # legacy kw; auto full-refresh removed
    if not item.enabled and not force:
        return []
    if item.baseline.market != "CN":
        logger.info("skip non-CN watch item %s", item.baseline.ticker)
        return []

    dt = now or datetime.now()
    observed_at = dt.isoformat(timespec="seconds")
    ticker = item.baseline.ticker

    if is_action_validity_expired(item.baseline, dt):
        logger.info(
            "skip expired watch item %s (trade_date=%s valid_days=%s)",
            ticker,
            item.baseline.trade_date,
            item.baseline.valid_trading_days,
        )
        return []

    if slot_key and item.last_slot_key == slot_key:
        return []  # 本时段已跑过

    return _light_observe(
        item,
        store=store,
        llm=llm,
        slot_key=slot_key,
        observed_at=observed_at,
    )


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
        _mirror_alerts_to_inbox(ticker, alerts)
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
    """跑一遍全部 enabled 且未过期的标的。"""
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
