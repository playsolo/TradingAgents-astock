"""单只股票的分析执行（进程内、无 Streamlit）。

复用 ``web.runner.execute_analysis_run`` 的核心管线，但配置只来自磁盘
（``model_config.json`` / 环境变量），因为 worker 进程没有 Streamlit session。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable

from tradingagents.default_config import DEFAULT_CONFIG

logger = logging.getLogger(__name__)


def _default_watch_store():
    try:
        from tradingagents.watchlist.store import default_store

        return default_store()
    except Exception:  # noqa: BLE001
        return None


def build_worker_config() -> dict[str, Any]:
    """Build a runtime config from disk-only sources (no session_state).

    Model choice priority: admin ``model_config.json`` → env fallback.
    Mirrors ``web.app._build_config`` (Deep depth: 5 debate / 5 risk rounds).
    """
    from tradingagents.auth.model_config import load_model_config, model_config_exists

    config = DEFAULT_CONFIG.copy()

    if model_config_exists():
        persisted = load_model_config()
        config["llm_provider"] = persisted["llm_provider"]
        config["deep_think_llm"] = persisted["deep_think_llm"]
        config["quick_think_llm"] = persisted["quick_think_llm"]
        backend_url = (persisted.get("backend_url") or os.getenv("BACKEND_URL") or "").strip()
        config["backend_url"] = backend_url or None
    else:
        # No admin config yet: fall back to env so a fresh install still runs.
        config["llm_provider"] = os.getenv("DEFAULT_LLM_PROVIDER", "deepseek").strip() or "deepseek"
        config["deep_think_llm"] = os.getenv("DEEP_THINK_LLM", "deepseek-chat").strip() or "deepseek-chat"
        config["quick_think_llm"] = (
            os.getenv("QUICK_THINK_LLM", "deepseek-chat").strip() or "deepseek-chat"
        )
        backend_url = (os.getenv("BACKEND_URL") or "").strip()
        config["backend_url"] = backend_url or None

    config["data_vendors"] = {
        "core_stock_apis": "a_stock",
        "technical_indicators": "a_stock",
        "fundamental_data": "a_stock",
        "news_data": "a_stock",
        "signal_data": "a_stock",
    }
    # Align with CLI Research Depth=Deep.
    config["max_debate_rounds"] = 5
    config["max_risk_discuss_rounds"] = 5
    config["checkpoint_enabled"] = True
    config["output_language"] = "Chinese"
    return config


def run_one_job(job: Any, config: dict[str, Any]) -> None:
    """Execute one :class:`AnalysisJob` synchronously to a terminal state.

    Never raises: pipeline errors are recorded in the incomplete index by the
    underlying runner. Runs inside a worker pool thread.

    Honors ``job.fresh``: when True, clears checkpoint + incomplete so a new
    analysis does not resume a stale graph. Auto-resume jobs use ``fresh=False``.

    Mode routing runs at start. Scan jobs on the narrow path skip the deep graph.
    Successful ``full_reeval`` runs refresh the calibration anchor.
    """
    from tradingagents.analysis.mode_router import resolve_analysis_mode
    from tradingagents.analysis.persist import save_calibration_from_state
    from tradingagents.inbox import emit_analysis_skipped
    from web.history import clear_incomplete_task, record_incomplete_task
    from web.progress import ProgressTracker
    from web.runner import execute_analysis_run

    market = job.market if job.market in {"CN", "US"} else "CN"
    fresh = bool(getattr(job, "fresh", True))
    try:
        resume_count = int(getattr(job, "resume_count", 0) or 0)
    except (TypeError, ValueError):
        resume_count = 0
    force_full = bool(getattr(job, "force_full_reeval", False))
    analysis_mode = str(getattr(job, "analysis_mode", None) or "auto")
    source = str(getattr(job, "source", None) or "manual")

    if fresh:
        clear_incomplete_task(job.ticker, job.trade_date)
        if market == "CN":
            try:
                from tradingagents.graph.checkpointer import clear_checkpoint

                clear_checkpoint(config["data_cache_dir"], job.ticker, job.trade_date)
            except Exception:  # noqa: BLE001
                logger.exception("clear_checkpoint failed for %s", job.ticker)
    else:
        # Seed incomplete row with resume_count before the runner overwrites stages.
        record_incomplete_task(
            job.ticker,
            job.trade_date,
            status="running",
            completed_stages=[],
            resume_count=resume_count,
        )

    gray_llm = None
    headlines: list[str] | None = None
    live_price: float | None = None
    try:
        from tradingagents.watchlist.scheduler import build_quick_llm
        from tradingagents.watchlist.snapshot import fetch_snapshot

        gray_llm = build_quick_llm(config)
        snap = fetch_snapshot(job.ticker, market=market, max_headlines=6)
        if snap.price and float(snap.price) > 0:
            live_price = float(snap.price)
        headlines = list(snap.headlines or [])
    except Exception:  # noqa: BLE001
        logger.debug("gray-zone prep failed for %s", job.ticker, exc_info=True)

    decision = resolve_analysis_mode(
        ticker=job.ticker,
        trade_date=job.trade_date,
        market=market,
        force_full_reeval=force_full,
        analysis_mode=analysis_mode,
        source=source,
        fresh=fresh,
        watch_store=_default_watch_store(),
        current_price=live_price,
        fetch_price=live_price is None,
        gray_zone_llm=gray_llm,
        gray_zone_headlines=headlines,
    )
    logger.info(
        "analyze route %s %s -> %s (%s) source=%s",
        job.ticker,
        job.trade_date,
        decision.mode,
        decision.reason,
        source,
    )

    if decision.skips_deep_analysis:
        anchor = decision.anchor
        emit_analysis_skipped(
            job.ticker,
            job.trade_date,
            reason=decision.reason,
            anchor_date=getattr(anchor, "trade_date", "") if anchor else "",
            stance=getattr(anchor, "stance", "") if anchor else "",
        )
        try:
            from tradingagents.analysis.skip_followup import follow_up_scan_skip

            follow_up_scan_skip(
                job.ticker,
                trade_date=job.trade_date,
                market=market,
                watch_store=_default_watch_store(),
                llm=gray_llm,
                escalate=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception("skip follow-up failed for %s", job.ticker)
        logger.info(
            "analyze skip deep %s %s (%s)",
            job.ticker,
            job.trade_date,
            decision.reason,
        )
        return

    tracker = ProgressTracker(
        ticker=job.ticker,
        trade_date=job.trade_date,
        market=market,
    )
    logger.info(
        "analyze start %s %s (%s) fresh=%s resume_count=%d",
        job.ticker,
        job.trade_date,
        market,
        fresh,
        resume_count,
    )
    execute_analysis_run(
        job.ticker,
        job.trade_date,
        config,
        tracker,
        market=market,
        extra_past_context=decision.extra_past_context,
    )
    if tracker.error:
        logger.warning("analyze error %s: %s", job.ticker, tracker.error)
    else:
        logger.info("analyze done %s -> %s", job.ticker, tracker.signal or "N/A")
        if decision.updates_calibration and getattr(tracker, "final_state", None):
            save_calibration_from_state(
                tracker.final_state,
                ticker=job.ticker,
                trade_date=job.trade_date,
                market=market,
            )
        final_state = getattr(tracker, "final_state", None)
        if final_state:
            try:
                from tradingagents.watchlist.service import (
                    maybe_auto_watch_from_scan_analysis,
                    refresh_watched_across_stores,
                )

                refresh_watched_across_stores(
                    final_state,
                    ticker=job.ticker,
                    trade_date=job.trade_date,
                    market=market,
                )
                if source == "scan":
                    maybe_auto_watch_from_scan_analysis(
                        final_state,
                        ticker=job.ticker,
                        trade_date=job.trade_date,
                        market=market,
                        source=source,
                        store=_default_watch_store(),
                    )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "watchlist sync after analysis failed for %s", job.ticker
                )


# Type alias for daemon injection / testing.
RunFn = Callable[[Any, dict[str, Any]], None]
