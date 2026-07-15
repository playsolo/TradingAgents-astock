#!/usr/bin/env python3
"""Standalone worker executed under the US TradingAgents PYTHONPATH.

Emits NDJSON progress events on stdout. Must not import the A-stock web package.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any


def _prefer_us_on_sys_path() -> Path:
    """Ensure the US checkout's ``tradingagents`` package wins over A-stock editable installs."""
    us_root = Path(
        os.environ.get("US_BRIDGE_PROJECT_ROOT")
        or os.environ.get("US_TRADINGAGENTS_ROOT")
        or os.getcwd()
    ).expanduser().resolve()
    cleaned: list[str] = [str(us_root)]
    for entry in sys.path:
        if not entry:
            continue
        resolved = str(Path(entry).resolve()) if entry not in {".", ""} else entry
        if "tradingagents-astock" in resolved.replace("\\", "/"):
            continue
        if resolved == str(us_root):
            continue
        cleaned.append(entry)
    sys.path[:] = cleaned
    return us_root


_prefer_us_on_sys_path()


def _emit(event: str, **payload: Any) -> None:
    print(json.dumps({"event": event, **payload}, ensure_ascii=False, default=str), flush=True)


def _strip_think(text: str) -> str:
    import re

    return re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL).strip()


US_STAGES = [
    ("market", "market_report"),
    ("social", "sentiment_report"),
    ("news", "news_report"),
    ("fundamentals", "fundamentals_report"),
    ("debate", "investment_debate_state"),
    ("trader", "trader_investment_plan"),
    ("risk", "risk_debate_state"),
    ("pm", "final_trade_decision"),
]


def _report_for(stage_id: str, state: dict[str, Any]) -> str:
    if stage_id == "debate":
        debate = state.get("investment_debate_state")
        if isinstance(debate, dict):
            return _strip_think(str(debate.get("judge_decision") or ""))
        return ""
    if stage_id == "risk":
        risk = state.get("risk_debate_state")
        if isinstance(risk, dict):
            return _strip_think(str(risk.get("judge_decision") or ""))
        return ""
    key = dict(US_STAGES)[stage_id]
    return _strip_think(str(state.get(key) or ""))


def _detect(state: dict[str, Any], already: set[str]) -> None:
    for stage_id, _ in US_STAGES:
        if stage_id in already:
            continue
        report = _report_for(stage_id, state)
        if report:
            already.add(stage_id)
            _emit("stage_done", stage=stage_id, report=report)
    for stage_id, _ in US_STAGES:
        if stage_id not in already:
            _emit("stage_active", stage=stage_id)
            return


def _serialize(state: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "company_of_interest",
        "trade_date",
        "market_report",
        "sentiment_report",
        "news_report",
        "fundamentals_report",
        "investment_plan",
        "trader_investment_plan",
        "final_trade_decision",
        "investment_debate_state",
        "risk_debate_state",
    )
    debate_keys = ("bull_history", "bear_history", "history", "current_response", "judge_decision")
    risk_keys = (
        "aggressive_history",
        "conservative_history",
        "neutral_history",
        "history",
        "judge_decision",
    )
    out: dict[str, Any] = {}
    for key in keys:
        if key not in state:
            continue
        value = state[key]
        if key == "investment_debate_state" and isinstance(value, dict):
            out[key] = {k: str(value.get(k) or "") for k in debate_keys if k in value}
        elif key == "risk_debate_state" and isinstance(value, dict):
            out[key] = {k: str(value.get(k) or "") for k in risk_keys if k in value}
        else:
            out[key] = "" if value is None else str(value)
    if out.get("trader_investment_plan"):
        out["trader_investment_decision"] = out["trader_investment_plan"]
    return out


def main() -> int:
    ticker = (os.environ.get("US_BRIDGE_TICKER") or "").strip()
    trade_date = (os.environ.get("US_BRIDGE_TRADE_DATE") or "").strip()
    if not ticker or not trade_date:
        _emit("error", message="缺少 US_BRIDGE_TICKER / US_BRIDGE_TRADE_DATE")
        return 2

    project_root = Path(os.environ.get("US_BRIDGE_PROJECT_ROOT") or os.getcwd())
    try:
        from dotenv import load_dotenv

        load_dotenv(project_root / ".env", override=False)
    except Exception:
        pass

    try:
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.graph.trading_graph import TradingAgentsGraph
    except Exception as exc:
        _emit("error", message=f"无法导入美股 TradingAgents: {exc}")
        return 3

    config = DEFAULT_CONFIG.copy()
    # Prefer streamed progress; checkpoint resume is optional via US env.
    config.setdefault("checkpoint_enabled", False)

    already: set[str] = set()
    _emit("stage_active", stage="market")

    try:
        graph = TradingAgentsGraph(
            selected_analysts=["market", "social", "news", "fundamentals"],
            debug=False,
            config=config,
        )
        # Mirror US _run_graph state construction without pretty-printing.
        past_context = graph.memory_log.get_past_context(ticker)
        instrument_context = graph.resolve_instrument_context(ticker, "stock")
        init_state = graph.propagator.create_initial_state(
            ticker,
            trade_date,
            asset_type="stock",
            past_context=past_context,
            instrument_context=instrument_context,
        )
        args = graph.propagator.get_graph_args()

        merged: dict[str, Any] = {}
        for chunk in graph.graph.stream(init_state, **args):
            if not isinstance(chunk, dict):
                continue
            merged.update(chunk)
            _detect(merged, already)

        if not merged.get("final_trade_decision"):
            # Fallback invoke if stream yielded nothing useful
            merged = graph.graph.invoke(init_state, **args)

        final_state = _serialize(merged)
        # Same post-analysis action_plan extract + disk log as the CN path.
        signal = graph.finalize_graph_run(ticker, trade_date, merged)
        _emit("complete", signal=signal, state=_serialize(merged))
        return 0
    except Exception as exc:
        _emit("error", message=f"{exc}\n{traceback.format_exc()[-1500:]}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
