"""扫描跳过深分析后的跟进：观察池轻量观察，高优告警则升级全量。"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from tradingagents.analysis.mode_router import HIGH_PRIORITY_ALERT_KINDS

logger = logging.getLogger(__name__)


def follow_up_scan_skip(
    ticker: str,
    *,
    trade_date: str,
    market: str = "CN",
    watch_store: Any | None = None,
    llm: Any = None,
    escalate: bool = True,
) -> dict[str, Any]:
    """If ticker is on a watchlist, run one light observe after scan skip.

    Returns ``{observed, alerts, escalated}``. High-priority alerts can enqueue
    a forced full_reeval job when ``escalate=True``.
    """
    result: dict[str, Any] = {
        "observed": False,
        "alerts": [],
        "escalated": False,
    }
    if watch_store is None:
        return result
    try:
        item = watch_store.get(ticker)
    except Exception:  # noqa: BLE001
        logger.exception("watch_store.get failed in skip follow-up for %s", ticker)
        return result
    if item is None:
        return result

    try:
        from tradingagents.watchlist.observe import observe_item

        slot = f"scan-skip-{trade_date or date.today().isoformat()}"
        alerts = observe_item(
            item,
            store=watch_store,
            llm=llm,
            slot_key=slot,
            force=True,
        )
    except Exception:  # noqa: BLE001
        logger.exception("skip follow-up observe failed for %s", ticker)
        return result

    result["observed"] = True
    result["alerts"] = list(alerts or [])
    high = [a for a in result["alerts"] if getattr(a, "kind", "") in HIGH_PRIORITY_ALERT_KINDS]
    if not high or not escalate:
        return result

    try:
        from web.analysis_queue import AnalysisJob, AnalysisQueueStore

        job = AnalysisJob(
            ticker=str(ticker).strip().upper(),
            trade_date=trade_date or date.today().isoformat(),
            market=(market or "CN").upper(),
            fresh=True,
            force_full_reeval=True,
            analysis_mode="full_reeval",
            source="scan",
        )
        added = AnalysisQueueStore().append_atomic([job])
        result["escalated"] = added > 0
        if added:
            from tradingagents.inbox import emit

            emit(
                "analysis.escalated",
                f"{ticker} 跳过后观察告警，已升级全量",
                severity="warning",
                detail="；".join(
                    f"{getattr(a, 'kind', '')}:{getattr(a, 'title', '')}" for a in high[:3]
                ),
                ticker=ticker,
                trade_date=trade_date,
                link_view="home",
                dedupe_key=f"escalate:{ticker}:{trade_date}",
            )
            logger.info("escalated full_reeval after skip observe for %s", ticker)
    except Exception:  # noqa: BLE001
        logger.exception("escalate after skip failed for %s", ticker)
    return result
