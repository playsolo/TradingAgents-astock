"""Agents must anchor calendar dates to trade_date and not invent D+1 / fake closes."""

from __future__ import annotations

from tradingagents.agents.researchers.bear_researcher import create_bear_researcher
from tradingagents.agents.researchers.bull_researcher import create_bull_researcher
from tradingagents.agents.risk_mgmt.aggressive_debator import create_aggressive_debator
from tradingagents.agents.utils.agent_utils import analysis_date_instruction


def test_analysis_date_instruction_forbids_next_day_and_fake_close():
    text = analysis_date_instruction("2026-07-14")
    assert "2026-07-14" in text
    assert "next calendar day" in text.lower() or "Do not invent the next calendar day" in text
    assert "intraday" in text.lower() or "盘中" in text
    assert "收盘" in text or "close" in text.lower()


def _debate_state():
    return {
        "company_of_interest": "002648",
        "trade_date": "2026-07-14",
        "market_report": "m",
        "sentiment_report": "s",
        "news_report": "n",
        "fundamentals_report": "f",
        "policy_report": "p",
        "hot_money_report": "h",
        "lockup_report": "l",
        "data_quality_summary": "",
        "trader_investment_plan": "plan",
        "investment_debate_state": {
            "history": "",
            "bull_history": "",
            "bear_history": "",
            "current_response": "",
            "count": 0,
        },
        "risk_debate_state": {
            "history": "",
            "aggressive_history": "",
            "conservative_history": "",
            "neutral_history": "",
            "current_aggressive_response": "",
            "current_conservative_response": "",
            "current_neutral_response": "",
            "count": 0,
        },
    }


class _CaptureLLM:
    def __init__(self):
        self.prompt = ""

    def invoke(self, prompt):
        self.prompt = prompt
        return type("R", (), {"content": "ok"})()


def test_bull_researcher_prompt_includes_analysis_date():
    llm = _CaptureLLM()
    create_bull_researcher(llm)(_debate_state())
    assert "2026-07-14" in llm.prompt
    assert "Analysis date" in llm.prompt


def test_bear_researcher_prompt_includes_analysis_date():
    llm = _CaptureLLM()
    create_bear_researcher(llm)(_debate_state())
    assert "2026-07-14" in llm.prompt
    assert "Analysis date" in llm.prompt


def test_aggressive_debator_prompt_includes_analysis_date():
    llm = _CaptureLLM()
    create_aggressive_debator(llm)(_debate_state())
    assert "2026-07-14" in llm.prompt
    assert "Analysis date" in llm.prompt
