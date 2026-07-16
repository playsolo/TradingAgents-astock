"""对单只观察标的执行一次复核（轻量快照 + 相对基准告警）。

操作建议时限过期后自动跳过；续命靠新报告加入/刷新基准，不再自动完整再分析。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from tradingagents.watchlist.calendar import is_action_validity_expired
from tradingagents.watchlist.judge import briefing_from_judgment, judge_vs_baseline
from tradingagents.watchlist.models import LEAN_LABELS, Alert, WatchItem
from tradingagents.watchlist.rules import detect_changes
from tradingagents.watchlist.snapshot import fetch_snapshot
from tradingagents.watchlist.store import WatchlistStore

logger = logging.getLogger(__name__)


@dataclass
class ObserveBatchResult:
    """``observe_all`` 批处理结果：成功 / 过期跳过 / 失败分开计。"""

    alerts: dict[str, list[Alert]] = field(default_factory=dict)
    observed: list[str] = field(default_factory=list)
    skipped_expired: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)

    @property
    def alert_count(self) -> int:
        return sum(len(a) for a in self.alerts.values())


def format_observe_batch_summary(result: ObserveBatchResult) -> str:
    """观察全部结束后给 UI / 日志用的一行摘要。"""
    parts = [f"已观察 {len(result.observed)} 只"]
    if result.alert_count:
        parts.append(f"新告警 {result.alert_count} 条")
    else:
        parts.append("无新告警")
    if result.skipped_expired:
        parts.append(f"跳过过期 {len(result.skipped_expired)} 只")
    if result.failed:
        failed_list = "、".join(result.failed)
        parts.append(f"失败 {len(result.failed)} 只：{failed_list}")
    return "；".join(parts)


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
    if item.baseline.market not in {"CN", "US"}:
        logger.info("skip unsupported market %s for %s", item.baseline.market, item.baseline.ticker)
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
    snapshot = fetch_snapshot(ticker, market=item.baseline.market or "CN")
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
    now: datetime | None = None,
    market: str | None = None,
) -> ObserveBatchResult:
    """串行跑一遍全部 enabled 且未过期的标的；单票失败跳过继续。

    手动「观察全部」请传入新的 ``slot_key``（如 ``manual-batch-…``）以强制重跑。
    ``market`` 非空时只观察该市场（``CN`` / ``US``）。
    """
    dt = now or datetime.now()
    out = ObserveBatchResult()
    for item in store.list_items():
        if not item.enabled:
            continue
        if market and (item.baseline.market or "CN") != market:
            continue
        ticker = item.baseline.ticker
        if is_action_validity_expired(item.baseline, dt):
            out.skipped_expired.append(ticker)
            continue
        try:
            alerts = observe_item(
                item,
                store=store,
                llm=llm,
                slot_key=slot_key,
                now=dt,
                analysis_config=analysis_config,
            )
            out.alerts[ticker] = alerts
            out.observed.append(ticker)
        except Exception as e:
            logger.exception("observe failed for %s: %s", ticker, e)
            out.failed[ticker] = str(e)
            out.alerts[ticker] = []
    return out
