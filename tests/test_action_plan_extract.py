"""Post-analysis LLM extraction of the final action-plan section → JSON."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tradingagents.agents.schemas import (
    ActionPlanLevels,
    FinalActionPlan,
    PortfolioRating,
)
from tradingagents.agents.utils.action_plan import (
    extract_action_plan,
    isolate_action_section,
    rating_to_sidebar_signal,
)


SAMPLE_MEMO = """**投资决策备忘录**

**最终评级**：**减持（Underweight）**

## 一、辩论评审与立场评定

牛方主张买入。熊方主张卖出。

## 二、关键风险-收益评估

略。

---

三、最终投资决策与操作指令

**评级：减持（Underweight）**

### 1. 已持仓者：有序降低敞口

**执行方向**：卖出/减仓
**减仓比例**：当前持仓的30%-50%
**减持价格区间参考**：101.00元 - 102.50元
**减仓后核心仓位的风控防线**：止损位设在95.66元。

### 2. 未持仓者：维持观望，不参与当前博弈

**执行方向**：不买入
触发买入条件：缩量回踩100.50元并企稳后，于100-101元区间试探。

若股价有效跌破布林带中轨约96元与50日均线93元，进一步减仓或清仓。

时限：覆盖未来至少1-2个交易周。
"""


@pytest.mark.unit
def test_isolate_action_section_drops_debate_prose():
    section = isolate_action_section(SAMPLE_MEMO)
    assert "三、最终投资决策与操作指令" in section
    assert "主张买入" not in section
    assert "101.00" in section


@pytest.mark.unit
def test_isolate_falls_back_to_rating_block_when_no_section_header():
    text = "**最终评级**：**持有**\n继续观察，暂不加仓。"
    section = isolate_action_section(text)
    assert "持有" in section


@pytest.mark.unit
def test_isolate_rating_anchor_prefers_last_label_without_section_header():
    """No 三、 header: start at the LAST rating label, not the first debate mention."""
    text = (
        "早期讨论中，有人给出 评级：买入 的乐观观点。\n"
        "但随后逻辑被证伪。\n"
        "最终评级：减持\n"
        "已持仓者有序减仓离场。"
    )
    section = isolate_action_section(text)
    assert "减持" in section
    assert "买入" not in section


@pytest.mark.unit
def test_isolate_keeps_authoritative_rating_header_over_later_prose():
    """Canonical English PM markdown: the first-line **Rating** is authoritative;
    a bare 'rating: hold' substring in later prose must not become the anchor."""
    text = (
        "**Rating**: Underweight\n\n"
        "**Executive Summary**: Trim exposure; a rating: hold stance was rejected.\n\n"
        "**Investment Thesis**: Momentum fading."
    )
    section = isolate_action_section(text)
    assert section.startswith("**Rating**: Underweight")


@pytest.mark.unit
def test_isolate_prefers_final_plan_over_earlier_recap_heading():
    """An earlier 三、 recap heading must not swallow the debate prose that
    precedes the real final-plan section."""
    memo = (
        "## 三、投资决策回顾\n"
        "牛方主张买入，熊方主张卖出，分歧巨大。\n\n"
        "## 三、最终投资计划与操作建议\n"
        "评级：减持（Underweight）\n"
        "已持仓者：减仓，参考价位96元。\n"
    )
    section = isolate_action_section(memo)
    assert "最终投资计划与操作建议" in section
    assert "主张买入" not in section
    assert "96" in section


@pytest.mark.unit
def test_rating_to_sidebar_signal_collapses_five_tier():
    assert rating_to_sidebar_signal("Underweight") == "Sell"
    assert rating_to_sidebar_signal("Overweight") == "Buy"
    assert rating_to_sidebar_signal("Hold") == "Hold"


@pytest.mark.unit
def test_extract_action_plan_uses_structured_llm_on_isolated_section():
    plan = FinalActionPlan(
        rating=PortfolioRating.UNDERWEIGHT,
        holders_action="减仓30%-50%",
        non_holders_action="观望，不买入",
        levels=ActionPlanLevels(
            reduce_low=101.0,
            reduce_high=102.5,
            stop_loss=95.66,
            watch_support=96.0,
            reentry_low=100.0,
            reentry_high=101.0,
        ),
        horizon="1-2w",
        summary="短期高位减持，未持仓观望。",
    )
    structured = MagicMock()
    structured.invoke.return_value = plan
    llm = MagicMock()
    llm.with_structured_output.return_value = structured

    result = extract_action_plan(llm, SAMPLE_MEMO)

    assert result is not None
    assert result["rating"] == "Underweight"
    assert result["levels"]["reduce_low"] == 101.0
    assert result["levels"]["stop_loss"] == 95.66
    prompt = structured.invoke.call_args.args[0]
    assert "主张买入" not in prompt
    assert "101.00" in prompt
    llm.invoke.assert_not_called()


@pytest.mark.unit
def test_extract_action_plan_returns_none_on_failure():
    llm = MagicMock()
    llm.with_structured_output.side_effect = NotImplementedError("no schema")
    llm.invoke.side_effect = RuntimeError("down")

    assert extract_action_plan(llm, SAMPLE_MEMO) is None


@pytest.mark.unit
def test_extract_signal_prefers_action_plan_over_body_buy():
    from web.history import extract_signal

    state = {
        "final_trade_decision": SAMPLE_MEMO,
        "action_plan": {
            "rating": "Underweight",
            "holders_action": "减仓",
            "non_holders_action": "观望",
            "levels": {},
            "summary": "减持",
        },
    }
    assert extract_signal(state) == "Sell"
