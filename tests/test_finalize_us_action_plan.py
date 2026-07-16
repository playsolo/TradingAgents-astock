"""US finalize must extract action_plan so report cards / history show levels."""

from __future__ import annotations

from unittest.mock import MagicMock

from web.progress import ProgressTracker
from web.runner import _finalize_us_run


def test_finalize_us_run_extracts_and_persists_action_plan(tmp_path, monkeypatch):
    plan = {
        "rating": "Underweight",
        "summary": "减持",
        "levels": {"reduce_low": 36.0, "stop_loss": 34.0},
        "horizon": "2-4 weeks",
    }

    monkeypatch.setattr(
        "tradingagents.agents.utils.action_plan.extract_action_plan",
        lambda *_a, **_k: plan,
    )
    monkeypatch.setattr(
        "web.runner._build_quick_llm_from_config",
        lambda _cfg: MagicMock(name="llm"),
    )

    tracker = ProgressTracker(ticker="MSFT", trade_date="2026-07-16", market="US")
    tracker.mark_complete(
        {
            "company_of_interest": "MSFT",
            "trade_date": "2026-07-16",
            "final_trade_decision": "Rating: Underweight\nStop loss 34.",
            "market_report": "m",
            "sentiment_report": "s",
            "news_report": "n",
            "fundamentals_report": "f",
            "investment_plan": "p",
            "trader_investment_plan": "t",
            "investment_debate_state": {},
            "risk_debate_state": {},
        },
        "Sell",
    )

    config = {"results_dir": str(tmp_path)}
    _finalize_us_run("MSFT", "2026-07-16", config, tracker)

    log_path = (
        tmp_path / "MSFT" / "TradingAgentsStrategy_logs" / "full_states_log_2026-07-16.json"
    )
    assert log_path.is_file()
    import json

    saved = json.loads(log_path.read_text(encoding="utf-8"))
    assert saved["action_plan"]["rating"] == "Underweight"
    assert tracker.final_state["action_plan"]["rating"] == "Underweight"
    assert tracker.signal == "Sell"  # Underweight → sidebar Sell


def test_finalize_us_run_skips_extract_when_plan_already_present(tmp_path, monkeypatch):
    called = {"n": 0}

    def boom(*_a, **_k):
        called["n"] += 1
        raise AssertionError("should not extract again")

    monkeypatch.setattr(
        "tradingagents.agents.utils.action_plan.extract_action_plan",
        boom,
    )

    tracker = ProgressTracker(ticker="AAPL", trade_date="2026-07-16", market="US")
    tracker.mark_complete(
        {
            "company_of_interest": "AAPL",
            "trade_date": "2026-07-16",
            "final_trade_decision": "Hold",
            "action_plan": {"rating": "Hold", "summary": "观望"},
            "market_report": "",
            "sentiment_report": "",
            "news_report": "",
            "fundamentals_report": "",
            "investment_plan": "",
            "trader_investment_plan": "",
            "investment_debate_state": {},
            "risk_debate_state": {},
        },
        "Hold",
    )
    _finalize_us_run("AAPL", "2026-07-16", {"results_dir": str(tmp_path)}, tracker)
    assert called["n"] == 0
    assert tracker.final_state["action_plan"]["rating"] == "Hold"
