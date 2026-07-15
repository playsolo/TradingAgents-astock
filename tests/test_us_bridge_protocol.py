"""US bridge NDJSON protocol + stage detection from streamed state."""

from __future__ import annotations

import json

from web.us_bridge.protocol import (
    US_PIPELINE_STAGES,
    US_STAGE_IDS,
    apply_bridge_event,
    detect_new_stages,
    encode_event,
    parse_event_line,
    serialize_final_state,
)
from web.progress import ProgressTracker


def test_us_pipeline_excludes_a_share_only_stages():
    ids = {s["id"] for s in US_PIPELINE_STAGES}
    assert ids == {
        "market",
        "social",
        "news",
        "fundamentals",
        "debate",
        "trader",
        "risk",
        "pm",
    }
    assert US_STAGE_IDS == [s["id"] for s in US_PIPELINE_STAGES]
    assert "policy" not in ids
    assert "hot_money" not in ids
    assert "lockup" not in ids
    assert "quality_gate" not in ids


def test_encode_and_parse_roundtrip():
    line = encode_event("stage_done", stage="market", report="ok")
    assert line.endswith("\n")
    event = parse_event_line(line)
    assert event == {"event": "stage_done", "stage": "market", "report": "ok"}


def test_parse_event_line_ignores_non_json_and_blank():
    assert parse_event_line("") is None
    assert parse_event_line("not json\n") is None
    assert parse_event_line('{"event":"stage_active","stage":"news"}')["stage"] == "news"


def test_detect_new_stages_emits_once_per_report():
    state = {
        "market_report": "m",
        "sentiment_report": "s",
        "investment_debate_state": {"judge_decision": "j"},
        "trader_investment_plan": "t",
        "risk_debate_state": {"judge_decision": "r"},
        "final_trade_decision": "f",
    }
    first = detect_new_stages(state, already=set())
    assert [e["stage"] for e in first if e["event"] == "stage_done"] == [
        "market",
        "social",
        "debate",
        "trader",
        "risk",
        "pm",
    ]
    already = {e["stage"] for e in first if e["event"] == "stage_done"}
    assert detect_new_stages(state, already=already) == []


def test_serialize_final_state_keeps_report_fields_only():
    raw = {
        "company_of_interest": "NVDA",
        "trade_date": "2024-05-10",
        "market_report": "m",
        "sentiment_report": "s",
        "news_report": "n",
        "fundamentals_report": "f",
        "investment_plan": "plan",
        "trader_investment_plan": "tp",
        "final_trade_decision": "Buy",
        "investment_debate_state": {"bull_history": "b", "bear_history": "e", "judge_decision": "j"},
        "risk_debate_state": {
            "aggressive_history": "a",
            "conservative_history": "c",
            "neutral_history": "n",
            "judge_decision": "rj",
        },
        "messages": ["drop-me"],
        "something_else": object(),
    }
    out = serialize_final_state(raw)
    assert "messages" not in out
    assert "something_else" not in out
    assert out["market_report"] == "m"
    assert out["investment_debate_state"]["judge_decision"] == "j"
    # Must be JSON-serializable
    json.dumps(out)


def test_serialize_final_state_keeps_action_plan_dict():
    """The post-analysis structured plan must survive serialization so the
    UI / history can prefer action_plan.rating over prose."""
    raw = {
        "final_trade_decision": "**最终评级**：减持",
        "action_plan": {
            "rating": "Underweight",
            "holders_action": "减仓",
            "levels": {"watch_support": 96.0},
        },
    }
    out = serialize_final_state(raw)
    assert out["action_plan"]["rating"] == "Underweight"
    assert out["action_plan"]["levels"]["watch_support"] == 96.0
    json.dumps(out)


def test_apply_bridge_event_updates_tracker():
    tracker = ProgressTracker(ticker="NVDA", trade_date="2024-05-10")
    tracker.is_running = True
    tracker.stages = list(US_PIPELINE_STAGES)

    apply_bridge_event(tracker, {"event": "stage_active", "stage": "market"})
    assert tracker.current_stage == "market"

    apply_bridge_event(
        tracker,
        {"event": "stage_done", "stage": "market", "report": "done"},
    )
    assert "market" in tracker.completed_stages
    assert tracker.stage_reports["market"] == "done"

    apply_bridge_event(
        tracker,
        {
            "event": "complete",
            "signal": "Buy",
            "state": {"final_trade_decision": "Buy NVDA"},
        },
    )
    assert tracker.is_complete
    assert tracker.signal == "Buy"
    assert tracker.final_state["final_trade_decision"] == "Buy NVDA"
