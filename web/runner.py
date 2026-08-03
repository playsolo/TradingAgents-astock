"""Background thread runner for TradingAgentsGraph pipeline."""

from __future__ import annotations

import json
import re
import threading
import traceback
from pathlib import Path
from typing import Any

from tradingagents.dataflows.utils import safe_ticker_component
from web.history import clear_incomplete_task, record_incomplete_task
from web.progress import PIPELINE_STAGES, ProgressTracker
from web.stock_display import normalize_report_state_mentions, normalize_stock_mentions


_REPORT_KEY_TO_STAGE = {s["report_key"]: s["id"] for s in PIPELINE_STAGES}

_ANALYST_REPORT_KEYS = [
    "market_report", "sentiment_report", "news_report",
    "fundamentals_report", "policy_report", "hot_money_report", "lockup_report",
]


def _discard_stopped_run(
    ticker: str,
    trade_date: str,
    config: dict,
    tracker: ProgressTracker,
) -> None:
    """Clear resumable artifacts for a user-stopped run."""
    from tradingagents.graph.checkpointer import clear_checkpoint

    clear_incomplete_task(ticker, trade_date)
    clear_checkpoint(config["data_cache_dir"], ticker, trade_date)
    tracker.mark_stopped()


def _strip_think_tags(text: str) -> str:
    """Remove <think>...</think> blocks from LLM output."""
    return re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL).strip()


def _detect_completed_stages(
    chunk: dict[str, Any],
    tracker: ProgressTracker,
) -> None:
    """Check the streamed chunk for newly completed stages."""
    for report_key in _ANALYST_REPORT_KEYS:
        stage_id = _REPORT_KEY_TO_STAGE[report_key]
        content = chunk.get(report_key, "")
        if content and tracker.stage_status(stage_id) != "done":
            report = normalize_stock_mentions(str(content), tracker.ticker, chunk)
            tracker.mark_stage_done(stage_id, _strip_think_tags(report))

    dqs = chunk.get("data_quality_summary", "")
    if dqs and tracker.stage_status("quality_gate") != "done":
        tracker.mark_stage_done("quality_gate", normalize_stock_mentions(str(dqs), tracker.ticker, chunk))

    debate = chunk.get("investment_debate_state")
    if debate and isinstance(debate, dict):
        judge = debate.get("judge_decision", "")
        if judge and tracker.stage_status("debate") != "done":
            tracker.mark_stage_done("debate", normalize_stock_mentions(str(judge), tracker.ticker, chunk))

    trader_plan = chunk.get("trader_investment_plan", "")
    if trader_plan and tracker.stage_status("trader") != "done":
        report = normalize_stock_mentions(str(trader_plan), tracker.ticker, chunk)
        tracker.mark_stage_done("trader", _strip_think_tags(report))

    risk = chunk.get("risk_debate_state")
    if risk and isinstance(risk, dict):
        risk_judge = risk.get("judge_decision", "")
        if risk_judge and tracker.stage_status("risk") != "done":
            tracker.mark_stage_done("risk", normalize_stock_mentions(str(risk_judge), tracker.ticker, chunk))

    final = chunk.get("final_trade_decision", "")
    if final and tracker.stage_status("pm") != "done":
        report = normalize_stock_mentions(str(final), tracker.ticker, chunk)
        tracker.mark_stage_done("pm", _strip_think_tags(report))


def _infer_active_stage(tracker: ProgressTracker) -> None:
    """Set the current_stage to the first non-completed stage."""
    from web.progress import STAGE_IDS
    for sid in STAGE_IDS:
        if tracker.stage_status(sid) == "pending":
            tracker.mark_stage_active(sid)
            return


def _run(
    ticker: str,
    trade_date: str,
    config: dict,
    tracker: ProgressTracker,
    extra_past_context: str = "",
) -> None:
    """Execute the full pipeline in the current thread."""
    from cli.stats_handler import StatsCallbackHandler
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    stats = StatsCallbackHandler()

    graph = TradingAgentsGraph(
        debug=True,
        config=config,
        callbacks=[stats],
    )

    init_state, args, _ = graph.prepare_graph_run(
        ticker,
        trade_date,
        callbacks=[stats],
        extra_past_context=extra_past_context,
    )

    last_chunk: dict[str, Any] = {}

    try:
        def _close_and_discard() -> None:
            graph.close_graph_run()
            _discard_stopped_run(ticker, trade_date, config, tracker)

        if tracker.stop_requested:
            _close_and_discard()
            return

        stream = graph.graph.stream(init_state, **args)
        while True:
            tracker.wait_if_paused()
            if tracker.stop_requested:
                _close_and_discard()
                return
            try:
                chunk = next(stream)
            except StopIteration:
                break

            if tracker.stop_requested:
                _close_and_discard()
                return

            last_chunk = chunk
            _detect_completed_stages(chunk, tracker)
            _infer_active_stage(tracker)
            record_incomplete_task(
                ticker,
                trade_date,
                status="paused" if tracker.is_paused else "running",
                completed_stages=tracker.completed_stages,
            )

            s = stats.get_stats()
            tracker.update_stats(s["llm_calls"], s["tool_calls"], s["tokens_in"], s["tokens_out"])

        if tracker.stop_requested:
            _close_and_discard()
            return

        if not last_chunk:
            raise RuntimeError("分析没有返回任何结果，请清理断点后重试。")

        # #55: 报告标的统一显示为「代码+名称」，须在 finalize 落盘前归一化 last_chunk
        normalize_report_state_mentions(last_chunk, ticker)

        signal = graph.finalize_graph_run(ticker, trade_date, last_chunk)
        if tracker.stop_requested:
            _close_and_discard()
            return

        tracker.mark_complete(last_chunk, signal)
        clear_incomplete_task(ticker, trade_date)
    finally:
        graph.close_graph_run()


def _notify_inbox_terminal(tracker: ProgressTracker) -> None:
    """Best-effort：向站内事件中心发分析终态通知，失败不影响分析本身。"""
    try:
        from web.events import notify_tracker_terminal

        notify_tracker_terminal(tracker)
    except Exception:  # noqa: BLE001 - inbox 是非关键旁路
        traceback.print_exc()


def _run_us(ticker: str, trade_date: str, config: dict, tracker: ProgressTracker) -> None:
    from web.us_bridge.client import run_us_analysis

    llm_config = {
        "llm_provider": config.get("llm_provider"),
        "deep_think_llm": config.get("deep_think_llm"),
        "quick_think_llm": config.get("quick_think_llm"),
        "backend_url": config.get("backend_url"),
        "output_language": config.get("output_language") or "Chinese",
        "max_debate_rounds": config.get("max_debate_rounds"),
        "max_risk_discuss_rounds": config.get("max_risk_discuss_rounds"),
        # Bug D: forward the fallback chain so the US bridge can degrade
        # to deepseek when the primary provider (e.g. minimax) is
        # unhealthy. The upstream TradingAgents does not understand
        # fallback_chain on its own — see ``web.us_bridge.health``.
        "fallback_chain": list(config.get("fallback_chain") or []),
    }
    run_us_analysis(
        ticker=ticker,
        trade_date=trade_date,
        llm_config=llm_config,
        tracker=tracker,
    )
    # Bridge may rewrite provider/model after preflight health check — copy
    # the effective provenance back onto the runner config used by finalize.
    for key in (
        "llm_provider",
        "deep_think_llm",
        "quick_think_llm",
        "llm_providers_used",
        "llm_models_used",
        "llm_provider_configured",
    ):
        if key in llm_config:
            config[key] = llm_config[key]


def _setup_tracker_for_run(
    ticker: str,
    trade_date: str,
    tracker: ProgressTracker,
    market: str,
) -> None:
    """Prime the tracker + record a running marker before the pipeline starts."""
    tracker.ticker = ticker
    tracker.trade_date = trade_date
    tracker.market = market if market in {"CN", "US"} else "CN"
    tracker.is_running = True

    if tracker.market == "US":
        from web.us_bridge.protocol import US_PIPELINE_STAGES

        tracker.stages = list(US_PIPELINE_STAGES)
    else:
        tracker.stages = list(PIPELINE_STAGES)
    tracker.mark_stage_active("market")

    record_incomplete_task(
        ticker,
        trade_date,
        status="running",
        completed_stages=tracker.completed_stages,
    )


def _build_quick_llm_from_config(config: dict) -> Any:
    """Build a quick-think LLM from a runtime config dict (worker / Streamlit)."""
    from tradingagents.llm_clients import create_llm_client_with_fallback

    client = create_llm_client_with_fallback(
        provider=config.get("llm_provider") or "deepseek",
        model=config.get("quick_think_llm") or "deepseek-v4-flash",
        base_url=config.get("backend_url"),
        fallback_chain=config.get("fallback_chain") or [],
    )
    return client.get_llm()


def _ensure_us_action_plan(
    serialized: dict[str, Any],
    config: dict,
    tracker: ProgressTracker,
) -> dict[str, Any] | None:
    """Extract action_plan on the A-stock side when the US worker omitted it."""
    existing = serialized.get("action_plan")
    if isinstance(existing, dict) and existing.get("rating"):
        return existing

    decision = str(serialized.get("final_trade_decision") or "")
    if not decision.strip():
        return None

    try:
        from tradingagents.agents.utils.action_plan import (
            extract_action_plan,
            rating_to_sidebar_signal,
        )

        llm = _build_quick_llm_from_config(config)
        plan = extract_action_plan(llm, decision)
    except Exception:
        traceback.print_exc()
        return None

    if not plan:
        return None

    serialized["action_plan"] = plan
    if isinstance(tracker.final_state, dict):
        tracker.final_state["action_plan"] = plan
    rating = plan.get("rating")
    if rating:
        tracker.signal = rating_to_sidebar_signal(rating)
    return plan


def _finalize_us_run(
    ticker: str,
    trade_date: str,
    config: dict,
    tracker: ProgressTracker,
) -> None:
    """Persist a completed US analysis to the same log location as A-stock.

    The US bridge subprocess runs the (older) TradingAgents graph which may
    not have a working ``finalize_graph_run`` on the server. Even when it
    does, its ``_log_state`` writes to ``full_states_log_{date}.json`` in the
    same ``~/.tradingagents/logs/`` tree — but the entry lacks A-stock-specific
    keys (policy_report, hot_money_report, lockup_report…) and the
    ``company_of_interest`` may be an empty string.

    This function writes a compatible log entry so that ``get_history()``
    can find it via ``logs/<ticker>/TradingAgentsStrategy_logs/full_states_log_*.json``,
    and runs ``extract_action_plan`` so the report card / watchlist get levels.
    """
    final_state = tracker.final_state
    if not final_state or not final_state.get("final_trade_decision"):
        # Not really complete — skip.
        return

    from web.us_bridge.protocol import serialize_final_state

    serialized = serialize_final_state(final_state)
    action_plan = _ensure_us_action_plan(serialized, config, tracker)

    # A+B stance continuity (same gate as A-share finalize_graph_run).
    try:
        from tradingagents.archive.continuity import apply_stance_continuity_to_state

        # Keep tracker + serialized views aligned for logging / UI.
        target = tracker.final_state if isinstance(tracker.final_state, dict) else serialized
        if isinstance(tracker.final_state, dict) and action_plan:
            tracker.final_state["action_plan"] = action_plan
        apply_stance_continuity_to_state(
            target, ticker=ticker, market="US", trade_date=str(trade_date)
        )
        if isinstance(tracker.final_state, dict):
            for key in ("action_plan", "final_trade_decision", "stance_continuity"):
                if key in tracker.final_state:
                    serialized[key] = tracker.final_state[key]
            if tracker.final_state.get("action_plan"):
                action_plan = tracker.final_state["action_plan"]
                from tradingagents.agents.utils.action_plan import (
                    rating_to_sidebar_signal,
                )

                rating = action_plan.get("rating")
                if rating:
                    tracker.signal = rating_to_sidebar_signal(rating)
    except Exception:
        traceback.print_exc()

    safe_ticker = safe_ticker_component(ticker)
    results_dir = config.get("results_dir") or str(
        Path.home() / ".tradingagents" / "logs"
    )
    directory = (
        Path(results_dir) / safe_ticker / "TradingAgentsStrategy_logs"
    )
    directory.mkdir(parents=True, exist_ok=True)

    # Build a log entry compatible with history.py's _load_state() expectations.
    from tradingagents.llm_clients.provenance import provenance_fields_from_config

    log_entry: dict[str, Any] = {
        "company_of_interest": serialized.get("company_of_interest", ticker),
        "trade_date": serialized.get("trade_date", trade_date),
        "market_report": serialized.get("market_report", ""),
        "sentiment_report": serialized.get("sentiment_report", ""),
        "news_report": serialized.get("news_report", ""),
        "fundamentals_report": serialized.get("fundamentals_report", ""),
        "policy_report": "",
        "hot_money_report": "",
        "lockup_report": "",
        "data_quality_summary": "",
        "investment_debate_state": serialized.get("investment_debate_state", {}),
        "trader_investment_decision": serialized.get("trader_investment_decision", ""),
        "risk_debate_state": serialized.get("risk_debate_state", {}),
        "investment_plan": serialized.get("investment_plan", ""),
        "final_trade_decision": serialized.get("final_trade_decision", ""),
        # LLM provenance for audit + UI — actual providers when preflight
        # swapped the primary (see us_bridge.client).
        **provenance_fields_from_config(config),
    }
    if action_plan:
        log_entry["action_plan"] = action_plan
    sc = serialized.get("stance_continuity")
    if isinstance(sc, dict):
        log_entry["stance_continuity"] = sc

    # Keep the live tracker in sync with disk for the signal-card line.
    if isinstance(tracker.final_state, dict):
        for key in (
            "llm_provider",
            "deep_think_llm",
            "quick_think_llm",
            "llm_backend_url",
            "llm_fallback_chain",
            "llm_providers_used",
            "llm_models_used",
        ):
            if key in log_entry:
                tracker.final_state[key] = log_entry[key]

    log_path = directory / f"full_states_log_{trade_date}.json"
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(log_entry, f, indent=4, ensure_ascii=False)


def _run_pipeline_body(
    ticker: str,
    trade_date: str,
    config: dict,
    tracker: ProgressTracker,
    extra_past_context: str = "",
) -> None:
    """Run the pipeline synchronously; record error/stop terminal state.

    Assumes the tracker was already primed via :func:`_setup_tracker_for_run`.
    Never raises — terminal state is written to the tracker + incomplete index.
    """
    try:
        if tracker.market == "US":
            _run_us(ticker, trade_date, config, tracker)
            _finalize_us_run(ticker, trade_date, config, tracker)
        else:
            _run(
                ticker,
                trade_date,
                config,
                tracker,
                extra_past_context=extra_past_context,
            )
    except Exception as exc:
        if tracker.stop_requested:
            try:
                if tracker.market == "US":
                    clear_incomplete_task(ticker, trade_date)
                    tracker.mark_stopped()
                else:
                    _discard_stopped_run(ticker, trade_date, config, tracker)
            except Exception:
                traceback.print_exc()
            return
        traceback.print_exc()
        record_incomplete_task(
            ticker,
            trade_date,
            status="error",
            error=str(exc),
            completed_stages=tracker.completed_stages,
        )
        tracker.mark_error(str(exc))


def execute_analysis_run(
    ticker: str,
    trade_date: str,
    config: dict,
    tracker: ProgressTracker,
    market: str = "CN",
    extra_past_context: str = "",
    *,
    notify_inbox: bool = True,
) -> ProgressTracker:
    """Run one analysis synchronously in the current thread (no Streamlit).

    This is the out-of-process worker entry point: it primes the tracker,
    runs the pipeline to a terminal state, and optionally posts an inbox event.
    """
    from tradingagents.runtime.arrow_safety import ensure_arrow_safe_for_current_thread

    ensure_arrow_safe_for_current_thread()
    _setup_tracker_for_run(ticker, trade_date, tracker, market)
    _run_pipeline_body(
        ticker, trade_date, config, tracker, extra_past_context=extra_past_context
    )
    if notify_inbox:
        _notify_inbox_terminal(tracker)
    return tracker


def run_analysis_in_thread(
    ticker: str,
    trade_date: str,
    config: dict,
    tracker: ProgressTracker,
    market: str = "CN",
    extra_past_context: str = "",
) -> threading.Thread:
    """Launch the pipeline in a daemon thread. Returns the thread handle."""
    # Prime synchronously so the sidebar immediately reflects the running task.
    _setup_tracker_for_run(ticker, trade_date, tracker, market)

    def _target() -> None:
        # Worker threads hit pandas→pyarrow; ensure safe allocator before DF work.
        from tradingagents.runtime.arrow_safety import ensure_arrow_safe_for_current_thread

        ensure_arrow_safe_for_current_thread()
        _run_pipeline_body(
            ticker,
            trade_date,
            config,
            tracker,
            extra_past_context=extra_past_context,
        )
        # 站内事件中心：在 runner 线程（session 无关）发终态通知，浏览器刷新也不丢。
        # notify_tracker_terminal 只对 complete/error 发事件，被停止的运行会被跳过。
        _notify_inbox_terminal(tracker)

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    return t
