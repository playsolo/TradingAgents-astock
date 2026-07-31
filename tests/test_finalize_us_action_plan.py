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
    assert tracker.signal == "Underweight"  # 5-tier preserved for sidebar


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


# ---------------------------------------------------------------------------
# Bug C regression: persisted log entries must carry LLM provenance so
# report cards can answer "which model produced this decision?" and audit
# logs can prove a given analysis used a specific provider. Previously the
# log_entry dict omitted llm_provider/deep_think_llm/quick_think_llm/
# llm_backend_url/llm_fallback_chain — the data was available in ``config``
# but thrown away before write.
# ---------------------------------------------------------------------------


def test_finalize_us_run_persists_llm_provenance(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "tradingagents.agents.utils.action_plan.extract_action_plan",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "web.runner._build_quick_llm_from_config",
        lambda _cfg: MagicMock(name="llm"),
    )

    tracker = ProgressTracker(ticker="NVDA", trade_date="2026-07-16", market="US")
    tracker.mark_complete(
        {
            "company_of_interest": "NVDA",
            "trade_date": "2026-07-16",
            "final_trade_decision": "Hold",
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

    config = {
        "results_dir": str(tmp_path),
        "llm_provider": "minimax",
        "deep_think_llm": "MiniMax-M3",
        "quick_think_llm": "MiniMax-M3",
        "backend_url": "https://api.minimaxi.com/v1",
        "fallback_chain": [{"provider": "deepseek", "model": "deepseek-v4-flash"}],
    }
    _finalize_us_run("NVDA", "2026-07-16", config, tracker)

    import json
    log_path = (
        tmp_path / "NVDA" / "TradingAgentsStrategy_logs" / "full_states_log_2026-07-16.json"
    )
    saved = json.loads(log_path.read_text(encoding="utf-8"))

    assert saved["llm_provider"] == "minimax", saved
    assert saved["deep_think_llm"] == "MiniMax-M3", saved
    assert saved["quick_think_llm"] == "MiniMax-M3", saved
    assert saved["llm_backend_url"] == "https://api.minimaxi.com/v1", saved
    assert saved["llm_fallback_chain"] == [
        {"provider": "deepseek", "model": "deepseek-v4-flash"}
    ], saved
    assert saved["llm_providers_used"] == ["minimax"], saved
    assert saved["llm_models_used"] == ["MiniMax-M3"], saved


def test_finalize_us_run_persists_effective_providers_after_fallback(
    tmp_path, monkeypatch
):
    """When US preflight swapped minimax → deepseek, log the actual provider."""
    monkeypatch.setattr(
        "tradingagents.agents.utils.action_plan.extract_action_plan",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "web.runner._build_quick_llm_from_config",
        lambda _cfg: MagicMock(name="llm"),
    )

    tracker = ProgressTracker(ticker="NFLX", trade_date="2026-07-18", market="US")
    tracker.mark_complete(
        {
            "company_of_interest": "NFLX",
            "trade_date": "2026-07-18",
            "final_trade_decision": "Buy",
            "market_report": "",
            "sentiment_report": "",
            "news_report": "",
            "fundamentals_report": "",
            "investment_plan": "",
            "trader_investment_plan": "",
            "investment_debate_state": {},
            "risk_debate_state": {},
        },
        "Buy",
    )

    config = {
        "results_dir": str(tmp_path),
        "llm_provider": "deepseek",
        "deep_think_llm": "deepseek-v4-pro",
        "quick_think_llm": "deepseek-v4-flash",
        "fallback_chain": [{"provider": "deepseek", "model": "deepseek-v4-flash"}],
        "llm_providers_used": ["deepseek"],
        "llm_models_used": ["deepseek-v4-pro", "deepseek-v4-flash"],
        "llm_provider_configured": "minimax",
    }
    _finalize_us_run("NFLX", "2026-07-18", config, tracker)

    import json

    log_path = (
        tmp_path / "NFLX" / "TradingAgentsStrategy_logs" / "full_states_log_2026-07-18.json"
    )
    saved = json.loads(log_path.read_text(encoding="utf-8"))
    assert saved["llm_provider"] == "deepseek"
    assert saved["llm_providers_used"] == ["deepseek"]
    assert saved["llm_models_used"] == ["deepseek-v4-pro", "deepseek-v4-flash"]
    assert tracker.final_state["llm_providers_used"] == ["deepseek"]


def test_finalize_us_run_provenance_handles_missing_keys(tmp_path, monkeypatch):
    """A legacy config (no llm_* fields at all) must still write the entry
    — the provenance fields default to None / empty rather than crashing
    on KeyError or skipping the write."""
    monkeypatch.setattr(
        "tradingagents.agents.utils.action_plan.extract_action_plan",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "web.runner._build_quick_llm_from_config",
        lambda _cfg: MagicMock(name="llm"),
    )

    tracker = ProgressTracker(ticker="ORCL", trade_date="2026-07-16", market="US")
    tracker.mark_complete(
        {
            "company_of_interest": "ORCL",
            "trade_date": "2026-07-16",
            "final_trade_decision": "Hold",
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

    config = {"results_dir": str(tmp_path)}  # no llm_* keys at all
    _finalize_us_run("ORCL", "2026-07-16", config, tracker)

    import json
    log_path = (
        tmp_path / "ORCL" / "TradingAgentsStrategy_logs" / "full_states_log_2026-07-16.json"
    )
    saved = json.loads(log_path.read_text(encoding="utf-8"))
    assert saved["llm_provider"] is None
    assert saved["deep_think_llm"] is None
    assert saved["quick_think_llm"] is None
    assert saved["llm_backend_url"] is None
    assert saved["llm_fallback_chain"] == []
