"""深度分析：利好兑现定价门禁 — Buy 禁止，最高 Overweight。"""

from __future__ import annotations

from unittest.mock import MagicMock

from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager
from tradingagents.agents.managers.research_manager import create_research_manager
from tradingagents.agents.researchers.bear_researcher import create_bear_researcher
from tradingagents.agents.risk_mgmt.aggressive_debator import create_aggressive_debator
from tradingagents.agents.schemas import (
    PortfolioDecision,
    PortfolioRating,
    ResearchPlan,
)
from tradingagents.agents.utils.agent_utils import catalyst_pricing_instruction


def test_catalyst_pricing_instruction_caps_buy_at_overweight():
    text = catalyst_pricing_instruction()
    assert "已兑现" in text
    assert "Buy" in text
    assert "Overweight" in text
    assert "must not" in text.lower() or "forbidden" in text.lower() or "禁止" in text


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
        "investment_plan": "research plan",
        "investment_debate_state": {
            "history": "debate",
            "bull_history": "",
            "bear_history": "",
            "current_response": "",
            "count": 0,
            "judge_decision": "",
        },
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
        "past_context": "",
    }


class _CaptureLLM:
    def __init__(self):
        self.prompt = ""

    def invoke(self, prompt):
        self.prompt = prompt if isinstance(prompt, str) else str(prompt)
        return type("R", (), {"content": "ok"})()


def _assert_pricing_gate(prompt: str) -> None:
    assert "已兑现" in prompt or "priced in" in prompt.lower()
    assert "Overweight" in prompt


def test_bear_prompt_includes_catalyst_pricing_gate():
    llm = _CaptureLLM()
    create_bear_researcher(llm)(_debate_state())
    _assert_pricing_gate(llm.prompt)


def test_aggressive_prompt_respects_pricing_cap():
    llm = _CaptureLLM()
    create_aggressive_debator(llm)(_debate_state())
    _assert_pricing_gate(llm.prompt)


def test_research_manager_prompt_includes_pricing_cap():
    captured: dict = {}
    plan = ResearchPlan(
        recommendation=PortfolioRating.HOLD,
        rationale="balanced",
        strategic_actions="wait",
    )
    structured = MagicMock()
    structured.invoke.side_effect = lambda prompt: (
        captured.__setitem__("prompt", prompt) or plan
    )
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    create_research_manager(llm)(_debate_state())
    _assert_pricing_gate(captured["prompt"])


def test_portfolio_manager_prompt_includes_pricing_cap():
    captured: dict = {}
    decision = PortfolioDecision(
        rating=PortfolioRating.HOLD,
        executive_summary="hold",
        investment_thesis="priced in",
    )
    structured = MagicMock()
    structured.invoke.side_effect = lambda prompt: (
        captured.__setitem__("prompt", prompt) or decision
    )
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    create_portfolio_manager(llm)(_debate_state())
    _assert_pricing_gate(captured["prompt"])
