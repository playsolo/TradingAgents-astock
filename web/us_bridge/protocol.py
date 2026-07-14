"""NDJSON event protocol between the US worker subprocess and the Web UI."""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from web.progress import ProgressTracker

US_PIPELINE_STAGES: list[dict[str, str]] = [
    {"id": "market", "name": "技术分析", "icon": "📊", "report_key": "market_report"},
    {"id": "social", "name": "情绪分析", "icon": "💬", "report_key": "sentiment_report"},
    {"id": "news", "name": "新闻舆情", "icon": "📰", "report_key": "news_report"},
    {"id": "fundamentals", "name": "基本面", "icon": "📋", "report_key": "fundamentals_report"},
    {"id": "debate", "name": "多空辩论", "icon": "⚔️", "report_key": "investment_plan"},
    {"id": "trader", "name": "交易决策", "icon": "💹", "report_key": "trader_investment_plan"},
    {"id": "risk", "name": "风控评估", "icon": "🛡️", "report_key": "risk_debate_state"},
    {"id": "pm", "name": "最终决策", "icon": "👔", "report_key": "final_trade_decision"},
]

US_STAGE_IDS = [s["id"] for s in US_PIPELINE_STAGES]

_FINAL_STATE_KEYS = (
    "company_of_interest",
    "trade_date",
    "market_report",
    "sentiment_report",
    "news_report",
    "fundamentals_report",
    "investment_plan",
    "trader_investment_plan",
    "trader_investment_decision",
    "final_trade_decision",
    "investment_debate_state",
    "risk_debate_state",
)

_DEBATE_KEYS = ("bull_history", "bear_history", "history", "current_response", "judge_decision")
_RISK_KEYS = (
    "aggressive_history",
    "conservative_history",
    "neutral_history",
    "history",
    "judge_decision",
)

_THINK_RE = re.compile(r"<think>.*?</think>\s*", flags=re.DOTALL)


def strip_think_tags(text: str) -> str:
    return _THINK_RE.sub("", text).strip()


def encode_event(event: str, **payload: Any) -> str:
    body = {"event": event, **payload}
    return json.dumps(body, ensure_ascii=False, default=str) + "\n"


def parse_event_line(line: str) -> dict[str, Any] | None:
    text = (line or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "event" not in data:
        return None
    return data


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def detect_new_stages(state: dict[str, Any], already: set[str]) -> list[dict[str, Any]]:
    """Return stage_done events newly satisfied by *state* (order = pipeline order)."""
    events: list[dict[str, Any]] = []

    def _emit(stage_id: str, report: str) -> None:
        if stage_id in already:
            return
        events.append(
            {
                "event": "stage_done",
                "stage": stage_id,
                "report": strip_think_tags(report),
            }
        )

    for stage in US_PIPELINE_STAGES:
        stage_id = stage["id"]
        key = stage["report_key"]
        if stage_id in {"debate"}:
            debate = state.get("investment_debate_state")
            if isinstance(debate, dict):
                judge = _text(debate.get("judge_decision"))
                if judge:
                    _emit(stage_id, judge)
            continue
        if stage_id == "risk":
            risk = state.get("risk_debate_state")
            if isinstance(risk, dict):
                judge = _text(risk.get("judge_decision"))
                if judge:
                    _emit(stage_id, judge)
            continue
        content = _text(state.get(key))
        if content:
            _emit(stage_id, content)
    return events


def next_active_stage(already: set[str]) -> str | None:
    for stage_id in US_STAGE_IDS:
        if stage_id not in already:
            return stage_id
    return None


def serialize_final_state(state: dict[str, Any]) -> dict[str, Any]:
    """Keep only JSON-safe report fields for UI / history."""
    out: dict[str, Any] = {}
    for key in _FINAL_STATE_KEYS:
        if key not in state:
            continue
        value = state[key]
        if key == "investment_debate_state" and isinstance(value, dict):
            out[key] = {k: _text(value.get(k)) for k in _DEBATE_KEYS if k in value}
        elif key == "risk_debate_state" and isinstance(value, dict):
            out[key] = {k: _text(value.get(k)) for k in _RISK_KEYS if k in value}
        else:
            out[key] = _text(value) if not isinstance(value, (dict, list)) else value
    # Align report_viewer which looks for trader_investment_decision
    if "trader_investment_decision" not in out and out.get("trader_investment_plan"):
        out["trader_investment_decision"] = out["trader_investment_plan"]
    return out


def apply_bridge_event(tracker: ProgressTracker, event: dict[str, Any]) -> None:
    name = event.get("event")
    if name == "stage_active":
        stage = event.get("stage") or ""
        if stage:
            tracker.mark_stage_active(stage)
        return
    if name == "stage_done":
        stage = event.get("stage") or ""
        if stage:
            tracker.mark_stage_done(stage, event.get("report") or "")
        return
    if name == "stats":
        tracker.update_stats(
            int(event.get("llm_calls") or 0),
            int(event.get("tool_calls") or 0),
            int(event.get("tokens_in") or 0),
            int(event.get("tokens_out") or 0),
        )
        return
    if name == "complete":
        state = event.get("state") or {}
        if not isinstance(state, dict):
            state = {}
        tracker.mark_complete(state, str(event.get("signal") or ""))
        return
    if name == "error":
        tracker.mark_error(str(event.get("message") or "美股桥接失败"))
        return


def emit_stage_events(
    state: dict[str, Any],
    already: set[str],
    emit: Callable[..., None],
) -> None:
    """Detect newly completed stages and emit stage_done + stage_active events."""
    for event in detect_new_stages(state, already):
        already.add(event["stage"])
        emit(event["event"], stage=event["stage"], report=event.get("report", ""))
    active = next_active_stage(already)
    if active:
        emit("stage_active", stage=active)
