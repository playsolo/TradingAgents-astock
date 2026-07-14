"""轻量 judge：盘面小结 + 三情景 + 今日倾向。"""

import json
from types import SimpleNamespace

from tradingagents.watchlist.judge import judge_vs_baseline
from tradingagents.watchlist.models import Baseline, MarketSnapshot


def _baseline() -> Baseline:
    return Baseline(
        ticker="002648",
        trade_date="2026-07-13",
        market="CN",
        stance="Hold",
        position_pct=10.0,
        baseline_price=23.0,
        entry_price=None,
        stop_loss=None,
        thesis_summary="持有为主",
        major_risks=[],
        log_path="",
    )


def _snapshot() -> MarketSnapshot:
    return MarketSnapshot(
        price=23.27,
        change_pct=-0.17,
        name="卫星化学",
        turnover_pct=1.2,
        headlines=["板块资金净流出"],
        main_net_inflow=121_320_000.0,
    )


def test_judge_fallback_without_llm_includes_briefing_shape():
    result = judge_vs_baseline(_baseline(), _snapshot(), llm=None)
    assert result["lean"] == "neutral"
    assert result["market_brief"]
    for key in ("optimistic", "neutral", "pessimistic"):
        sc = result["scenarios"][key]
        assert sc["view"]
        assert sc["reason"]


def test_judge_parses_briefing_fields_from_llm():
    payload = {
        "suggested_stance": "Hold",
        "suggested_position_pct": 10,
        "new_major_risks": [],
        "summary": "维持持有",
        "watch_point": "量能",
        "avoid": "追高",
        "market_brief": "股价微跌，化工板块资金偏弱。",
        "lean": "pessimistic",
        "lean_reason": "板块资金持续流出。",
        "scenarios": {
            "optimistic": {"view": "回踩后修复", "reason": "估值仍低"},
            "neutral": {"view": "震荡消化", "reason": "等待资金回流"},
            "pessimistic": {"view": "继续阴跌", "reason": "板块继续抽血"},
        },
    }
    captured: dict = {}

    def _invoke(prompt, *_a, **_k):
        captured["prompt"] = prompt
        return SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))

    llm = SimpleNamespace(invoke=_invoke)
    result = judge_vs_baseline(_baseline(), _snapshot(), llm=llm)
    assert "股价微跌" in result["market_brief"]
    assert "主力净流入" in result["market_brief"]
    assert result["lean"] == "pessimistic"
    assert "板块" in result["lean_reason"]
    assert result["scenarios"]["optimistic"]["view"] == "回踩后修复"
    assert result["scenarios"]["neutral"]["reason"] == "等待资金回流"
    assert result["scenarios"]["pessimistic"]["view"] == "继续阴跌"
    prompt = captured["prompt"]
    assert "主力净流入" in prompt or "12132" in prompt
    assert "个股资金流向必须以快照数值为准" in prompt
    # realtime fund flow present → 过时板块资金标题不进 prompt
    assert "板块资金净流出" not in prompt


def test_judge_overrides_false_stock_outflow_when_snapshot_shows_inflow():
    payload = {
        "suggested_stance": "Hold",
        "suggested_position_pct": 10,
        "new_major_risks": [],
        "summary": "卫星化学两日均出现在净流出榜，量价背离。",
        "watch_point": "资金回流",
        "avoid": "追高",
        "market_brief": (
            "卫星化学当日大涨7.34%，但基础化工板块近两日主力净流出超54亿元，"
            "卫星化学两日均出现在净流出榜，量价背离明显。"
        ),
        "lean": "neutral",
        "lean_reason": "股价上涨但个股资金流出。",
        "scenarios": {
            "optimistic": {"view": "a", "reason": "ra"},
            "neutral": {"view": "b", "reason": "rb"},
            "pessimistic": {"view": "c", "reason": "rc"},
        },
    }
    llm = SimpleNamespace(
        invoke=lambda *_a, **_k: SimpleNamespace(
            content=json.dumps(payload, ensure_ascii=False)
        )
    )
    result = judge_vs_baseline(_baseline(), _snapshot(), llm=llm)
    assert "净流入" in result["market_brief"]
    assert "12132" in result["market_brief"] or "主力净流入" in result["market_brief"]
    assert "净流出榜" not in result["market_brief"]
    assert "净流出榜" not in result["summary"]
    assert "净流入" in result["summary"] or "主力净流入" in result["summary"]
    assert "个股资金流出" not in result["lean_reason"] or "净流入" in result["lean_reason"]


def test_judge_strips_unanchored_main_outflow_when_snapshot_inflow():
    payload = {
        "suggested_stance": "Hold",
        "suggested_position_pct": 10,
        "new_major_risks": [],
        "summary": "主力净流出明显，谨慎。",
        "watch_point": "",
        "avoid": "",
        "market_brief": "涨7%，主力净流出，量价背离。",
        "lean": "neutral",
        "lean_reason": "观望",
        "scenarios": {
            "optimistic": {"view": "a", "reason": "ra"},
            "neutral": {"view": "b", "reason": "rb"},
            "pessimistic": {"view": "c", "reason": "rc"},
        },
    }
    llm = SimpleNamespace(
        invoke=lambda *_a, **_k: SimpleNamespace(
            content=json.dumps(payload, ensure_ascii=False)
        )
    )
    result = judge_vs_baseline(_baseline(), _snapshot(), llm=llm)
    assert "个股主力净流入" in result["market_brief"]
    assert "主力净流出" not in result["market_brief"]
    assert "主力净流出" not in result["summary"]


def test_judge_prompt_allows_unknown_fund_flow_without_inventing():
    captured: dict = {}
    snap = MarketSnapshot(
        price=23.0,
        change_pct=1.0,
        name="卫星化学",
        headlines=["旧闻：板块资金净流出"],
        main_net_inflow=None,
    )
    payload = {
        "suggested_stance": "Hold",
        "suggested_position_pct": 10,
        "new_major_risks": [],
        "summary": "维持",
        "watch_point": "",
        "avoid": "",
        "market_brief": "涨1%",
        "lean": "neutral",
        "lean_reason": "观望",
        "scenarios": {
            "optimistic": {"view": "a", "reason": "ra"},
            "neutral": {"view": "b", "reason": "rb"},
            "pessimistic": {"view": "c", "reason": "rc"},
        },
    }

    def _invoke(prompt, *_a, **_k):
        captured["prompt"] = prompt
        return SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))

    judge_vs_baseline(
        _baseline(),
        snap,
        llm=SimpleNamespace(invoke=_invoke),
        as_of="2026-07-14",
    )
    assert "未知" in captured["prompt"]
    assert "不得编造个股净流入或净流出" in captured["prompt"]
    assert "观察日 2026-07-14" in captured["prompt"]


def test_judge_parses_fenced_nested_scenarios_json():
    payload = {
        "suggested_stance": "Hold",
        "suggested_position_pct": 10,
        "new_major_risks": [],
        "summary": "维持",
        "watch_point": "",
        "avoid": "",
        "market_brief": "盘整",
        "lean": "neutral",
        "lean_reason": "缺乏方向",
        "scenarios": {
            "optimistic": {"view": "反弹", "reason": "超跌"},
            "neutral": {"view": "震荡", "reason": "观望"},
            "pessimistic": {"view": "破位", "reason": "量缩"},
        },
    }
    fenced = "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"
    llm = SimpleNamespace(invoke=lambda *_a, **_k: SimpleNamespace(content=fenced))
    result = judge_vs_baseline(_baseline(), _snapshot(), llm=llm)
    assert result["scenarios"]["pessimistic"]["view"] == "破位"
    assert "盘整" in result["market_brief"]
    assert "个股主力净流入" in result["market_brief"]


def test_judge_normalizes_invalid_lean_to_neutral():
    payload = {
        "suggested_stance": "Hold",
        "suggested_position_pct": 10,
        "new_major_risks": [],
        "summary": "x",
        "watch_point": "",
        "avoid": "",
        "market_brief": "盘面平稳",
        "lean": "super-bull",
        "lean_reason": "无效倾向",
        "scenarios": {
            "optimistic": {"view": "a", "reason": "ra"},
            "neutral": {"view": "b", "reason": "rb"},
            "pessimistic": {"view": "c", "reason": "rc"},
        },
    }
    llm = SimpleNamespace(
        invoke=lambda *_a, **_k: SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))
    )
    result = judge_vs_baseline(_baseline(), _snapshot(), llm=llm)
    assert result["lean"] == "neutral"
