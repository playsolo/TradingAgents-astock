"""A 股交易时段：可执行窗口与决策 Agent 注入。"""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager
from tradingagents.agents.schemas import PortfolioDecision, PortfolioRating, TraderAction, TraderProposal
from tradingagents.agents.trader.trader import create_trader
from tradingagents.agents.utils.agent_utils import actionability_instruction
from tradingagents.watchlist.calendar import (
    CnSessionPhase,
    actionable_cn_trading_day,
    cn_session_phase,
    next_cn_trading_day,
)

CN_TZ = ZoneInfo("Asia/Shanghai")


def _cn(y, m, d, hh=0, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=CN_TZ)


def test_session_phase_trading_day_windows():
    assert cn_session_phase(_cn(2026, 7, 13, 9, 0)) is CnSessionPhase.PRE_MARKET
    assert cn_session_phase(_cn(2026, 7, 13, 10, 0)) is CnSessionPhase.IN_SESSION
    assert cn_session_phase(_cn(2026, 7, 13, 11, 30)) is CnSessionPhase.LUNCH_BREAK
    assert cn_session_phase(_cn(2026, 7, 13, 12, 0)) is CnSessionPhase.LUNCH_BREAK
    assert cn_session_phase(_cn(2026, 7, 13, 14, 0)) is CnSessionPhase.IN_SESSION
    assert cn_session_phase(_cn(2026, 7, 13, 15, 0)) is CnSessionPhase.AFTER_HOURS
    assert cn_session_phase(_cn(2026, 7, 13, 16, 48)) is CnSessionPhase.AFTER_HOURS


def test_weekend_is_non_trading_day():
    assert cn_session_phase(_cn(2026, 7, 11, 10, 0)) is CnSessionPhase.NON_TRADING_DAY


def test_next_cn_trading_day_skips_weekend():
    assert next_cn_trading_day(date(2026, 7, 10)) == date(2026, 7, 13)
    assert next_cn_trading_day("2026-07-13") == date(2026, 7, 14)


def test_actionable_day_after_hours_is_next_session():
    assert actionable_cn_trading_day(_cn(2026, 7, 14, 16, 48)) == date(2026, 7, 15)
    assert actionable_cn_trading_day(_cn(2026, 7, 10, 15, 5)) == date(2026, 7, 13)


def test_actionable_day_during_session_is_today():
    assert actionable_cn_trading_day(_cn(2026, 7, 14, 10, 30)) == date(2026, 7, 14)
    assert actionable_cn_trading_day(_cn(2026, 7, 14, 12, 0)) == date(2026, 7, 14)
    assert actionable_cn_trading_day(_cn(2026, 7, 14, 9, 0)) == date(2026, 7, 14)


def test_actionability_instruction_after_hours_splits_fact_vs_order():
    text = actionability_instruction(
        "2026-07-14",
        now=_cn(2026, 7, 14, 16, 48),
    )
    assert "Execution timing" in text
    assert "2026-07-14" in text
    assert "2026-07-15" in text
    assert "after_hours" in text
    assert "下一交易日" in text
    assert "可执行" in text
    assert "今日追高" in text or "立即" in text


def test_actionability_instruction_in_session_allows_same_day_orders():
    text = actionability_instruction(
        "2026-07-14",
        now=_cn(2026, 7, 14, 10, 30),
    )
    assert "Execution timing" in text
    assert "in_session" in text
    assert "2026-07-14" in text
    assert "盘中" in text or "当前交易时段" in text


def test_actionability_instruction_lunch_prefers_afternoon():
    text = actionability_instruction(
        "2026-07-14",
        now=_cn(2026, 7, 14, 12, 0),
    )
    assert "lunch_break" in text
    assert "午后" in text or "下午" in text


def test_actionability_instruction_retrospective_uses_analysis_next_session():
    # Generate on Wed looking back at Monday → actionable Tue, not live clock
    text = actionability_instruction(
        "2026-07-13",
        now=_cn(2026, 7, 15, 10, 30),
    )
    assert "retrospective" in text
    assert "2026-07-14" in text
    assert "今日追高" in text or "立即" in text
    assert "in_session" not in text


def test_propagator_freezes_analysis_clock():
    from tradingagents.graph.propagation import Propagator

    state = Propagator().create_initial_state("002648", "2026-07-14")
    assert "analysis_clock" in state
    assert "T" in state["analysis_clock"] or "+" in state["analysis_clock"]


def _structured_llm(captured: dict, result):
    structured = MagicMock()
    structured.invoke.side_effect = lambda prompt: (
        captured.__setitem__("prompt", prompt) or result
    )
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    return llm


def test_trader_prompt_includes_actionability():
    captured = {}
    llm = _structured_llm(
        captured,
        TraderProposal(action=TraderAction.HOLD, reasoning="wait"),
    )
    create_trader(llm)(
        {
            "company_of_interest": "002648",
            "trade_date": "2026-07-14",
            "investment_plan": "plan",
            "policy_report": "",
            "hot_money_report": "",
            "lockup_report": "",
        }
    )
    joined = " ".join(m["content"] for m in captured["prompt"])
    assert "Execution timing" in joined
    assert "Analysis date" in joined


def test_portfolio_manager_prompt_includes_actionability():
    captured = {}
    llm = _structured_llm(
        captured,
        PortfolioDecision(
            rating=PortfolioRating.UNDERWEIGHT,
            executive_summary="trim",
            investment_thesis="risk",
        ),
    )
    create_portfolio_manager(llm)(
        {
            "company_of_interest": "002648",
            "trade_date": "2026-07-14",
            "investment_plan": "research",
            "trader_investment_plan": "trader",
            "past_context": "",
            "risk_debate_state": {
                "history": "h",
                "aggressive_history": "",
                "conservative_history": "",
                "neutral_history": "",
                "current_aggressive_response": "",
                "current_conservative_response": "",
                "current_neutral_response": "",
                "count": 0,
                "judge_decision": "",
                "latest_speaker": "",
            },
        }
    )
    assert "Execution timing" in captured["prompt"]
    assert "Analysis date" in captured["prompt"]
