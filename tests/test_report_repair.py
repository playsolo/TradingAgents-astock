"""Report-side probe + regenerate helpers for sections marked [数据缺失]."""

from __future__ import annotations

import json
from pathlib import Path


def test_has_missing_data_detects_marker():
    from web.report_repair import has_missing_data

    assert has_missing_data("正常段落") is False
    assert has_missing_data("[数据缺失: 资产负债率]") is True


def test_probe_section_data_fundamentals_ok(monkeypatch):
    from web import report_repair

    monkeypatch.setattr(
        report_repair,
        "_call_probe_tool",
        lambda name, fn: (True, f"{name}: ok rows"),
    )

    result = report_repair.probe_section_data(
        "fundamentals_report", "002648", "2026-07-14"
    )
    assert result.ok is True
    assert "get_balance_sheet" in result.detail


def test_probe_section_data_hot_money_fails_on_error(monkeypatch):
    from web import report_repair

    def fake_call(name, fn):
        if name == "get_fund_flow":
            return False, f"{name}: fail — Error fetching fund flow: SSL"
        return True, f"{name}: ok"

    monkeypatch.setattr(report_repair, "_call_probe_tool", fake_call)

    result = report_repair.probe_section_data(
        "hot_money_report", "002648", "2026-07-14"
    )
    assert result.ok is False
    assert "get_fund_flow" in result.detail


def test_save_analysis_state_atomic(tmp_path: Path):
    from web.report_repair import save_analysis_state

    path = tmp_path / "full_states_log_2026-07-14.json"
    path.write_text(json.dumps({"fundamentals_report": "old"}), encoding="utf-8")

    save_analysis_state(str(path), {"fundamentals_report": "new", "hot_money_report": "x"})

    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["fundamentals_report"] == "new"
    assert loaded["hot_money_report"] == "x"


def test_save_analysis_state_strips_runtime_messages(tmp_path: Path):
    from web.report_repair import save_analysis_state

    path = tmp_path / "full_states_log_2026-07-14.json"
    path.write_text(
        json.dumps(
            {
                "company_of_interest": "002648",
                "trade_date": "2026-07-14",
                "fundamentals_report": "old",
                "investment_plan": "keep me",
            }
        ),
        encoding="utf-8",
    )

    save_analysis_state(
        str(path),
        {
            "company_of_interest": "002648",
            "trade_date": "2026-07-14",
            "fundamentals_report": "new",
            "messages": [("human", "002648"), object()],
            "trader_investment_plan": "buy plan",
        },
    )

    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["fundamentals_report"] == "new"
    assert loaded["investment_plan"] == "keep me"
    assert loaded["trader_investment_decision"] == "buy plan"
    assert "messages" not in loaded


def test_section_supports_repair_known_sections():
    from web.report_repair import section_supports_repair

    assert section_supports_repair("fundamentals_report") is True
    assert section_supports_repair("market_report") is False


def test_regenerate_section_updates_report_and_quality(monkeypatch, tmp_path: Path):
    from web import report_repair

    state = {
        "company_of_interest": "002648",
        "trade_date": "2026-07-14",
        "fundamentals_report": "[数据缺失: 资产负债率]\nold",
        "market_report": "stale-in-memory market",
        "sentiment_report": "ok",
        "news_report": "ok",
        "policy_report": "ok",
        "hot_money_report": "ok",
        "lockup_report": "ok",
        "data_quality_summary": "old dqs",
    }
    path = tmp_path / "full_states_log_2026-07-14.json"
    # Disk already has a newer market section than the in-memory snapshot.
    disk_state = dict(state)
    disk_state["market_report"] = "fresh-on-disk market"
    path.write_text(json.dumps(disk_state), encoding="utf-8")

    monkeypatch.setattr(
        report_repair,
        "_run_analyst_tool_loop",
        lambda **kwargs: "regenerated fundamentals with 资产负债率 52%",
    )
    monkeypatch.setattr(
        report_repair,
        "_refresh_quality_gate",
        lambda state, config: "## 数据质量门控结果\n基本面 A",
    )

    updated = report_repair.regenerate_section(
        state=state,
        section_key="fundamentals_report",
        config={"llm_provider": "test"},
        log_path=str(path),
    )

    assert "资产负债率 52%" in updated["fundamentals_report"]
    assert "基本面 A" in updated["data_quality_summary"]
    disk = json.loads(path.read_text(encoding="utf-8"))
    assert disk["fundamentals_report"] == updated["fundamentals_report"]
    assert disk["market_report"] == "fresh-on-disk market"


def test_list_repairable_missing_sections():
    from web.report_repair import list_repairable_missing_sections

    state = {
        "fundamentals_report": "[数据缺失: 资产负债率]",
        "hot_money_report": "[数据缺失: 主力资金]",
        "lockup_report": "完整",
        "market_report": "[数据缺失: xxx]",  # unsupported
    }
    assert list_repairable_missing_sections(state) == [
        "fundamentals_report",
        "hot_money_report",
    ]


def test_repair_all_missing_probes_then_auto_regenerates(monkeypatch, tmp_path: Path):
    from web import report_repair

    state = {
        "company_of_interest": "002648",
        "trade_date": "2026-07-14",
        "fundamentals_report": "[数据缺失: 资产负债率]",
        "hot_money_report": "[数据缺失: 主力资金]",
        "lockup_report": "ok",
        "market_report": "ok",
        "sentiment_report": "ok",
        "news_report": "ok",
        "policy_report": "ok",
        "data_quality_summary": "old",
    }
    path = tmp_path / "full_states_log_2026-07-14.json"
    path.write_text(json.dumps(state), encoding="utf-8")

    def fake_probe(section_key, ticker, trade_date):
        if section_key == "hot_money_report":
            return report_repair.ProbeResult(False, "hot_money fail")
        return report_repair.ProbeResult(True, f"{section_key} ok")

    calls: list[str] = []

    def fake_regen(*, state, section_key, config, log_path=None, refresh_quality=True):
        calls.append(section_key)
        out = dict(state)
        out[section_key] = f"regen:{section_key}"
        if refresh_quality:
            out["data_quality_summary"] = "qg"
        return out

    monkeypatch.setattr(report_repair, "probe_section_data", fake_probe)
    monkeypatch.setattr(report_repair, "regenerate_section", fake_regen)
    monkeypatch.setattr(
        report_repair,
        "refresh_downstream_decisions",
        lambda state, config: {
            **state,
            "investment_plan": "refreshed plan Sell",
            "trader_investment_plan": "Sell",
            "trader_investment_decision": "Sell",
            "final_trade_decision": "**Rating**: Sell\n最终评级：卖出",
        },
    )
    monkeypatch.setattr(
        report_repair,
        "_refresh_quality_gate",
        lambda state, config: "batch quality",
    )

    result = report_repair.repair_all_missing_sections(
        state=state,
        config={"llm_provider": "test"},
        log_path=str(path),
    )

    assert result.regenerated == ["fundamentals_report"]
    assert result.probe_failed == ["hot_money_report"]
    assert result.state["fundamentals_report"] == "regen:fundamentals_report"
    assert result.state["hot_money_report"] == "[数据缺失: 主力资金]"
    assert result.state["data_quality_summary"] == "batch quality"
    assert result.state["final_trade_decision"].startswith("**Rating**: Sell")
    assert calls == ["fundamentals_report"]
    disk = json.loads(path.read_text(encoding="utf-8"))
    assert disk["fundamentals_report"] == "regen:fundamentals_report"
    assert disk["data_quality_summary"] == "batch quality"
    assert "Sell" in disk["final_trade_decision"]

def test_execute_tool_calls_without_langgraph_runtime(monkeypatch):
    """Standalone regen must not use ToolNode.invoke (needs runtime → 'N/A' error)."""
    from langchain_core.messages import AIMessage, ToolMessage
    from web import report_repair

    tools = report_repair._tools_for("fundamentals")

    monkeypatch.setattr(
        report_repair,
        "_invoke_tool",
        lambda tool_obj, payload: f"ok:{payload.get('ticker')}",
    )

    msg = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "get_fundamentals",
                "args": {"ticker": "688401", "curr_date": "2026-07-14"},
                "id": "call_1",
                "type": "tool_call",
            }
        ],
    )
    out = report_repair._execute_tool_calls(tools, msg)
    assert len(out) == 1
    assert isinstance(out[0], ToolMessage)
    assert out[0].content == "ok:688401"
    assert out[0].tool_call_id == "call_1"


def test_scrub_soft_missing_markers_removes_false_positives():
    from web.report_repair import has_hard_missing_data, has_missing_data, scrub_soft_missing_markers

    raw = (
        "正文\n"
        "[数据缺失：未检索到明确增减持记录]\n"
        "[数据缺失: 近20日主力资金历史日度数据——SSLError]\n"
        "[数据缺失: 行业对比查询失败——HTTP 502]\n"
        "[数据缺失: 无分析师覆盖]\n"
        "[数据缺失：股权质押比例]\n"
        "[数据缺失]\n"
        "[数据缺失: 资产负债表明细]\n"
    )
    cleaned = scrub_soft_missing_markers(raw)
    assert "[数据缺失：未检索到" not in cleaned
    assert "近20日" not in cleaned or "[数据缺失" not in cleaned.split("近20日")[0][-20:]
    assert "无分析师覆盖" not in cleaned or "[数据缺失" not in cleaned
    assert "资产负债表明细" in cleaned  # hard missing kept
    assert has_missing_data(cleaned) is True
    assert has_hard_missing_data(cleaned) is True
    soft_only = scrub_soft_missing_markers(
        "[数据缺失：未检索到明确增减持记录]\n[数据缺失: 近20日主力资金历史]\n"
        "[数据缺失: 无分析师覆盖]"
    )
    assert has_hard_missing_data(soft_only) is False


def test_extract_signal_reads_chinese_sell():
    from web.history import extract_signal

    assert (
        extract_signal(
            {
                "final_trade_decision": "**最终评级：卖出**\n立即清仓离场",
            }
        )
        == "Sell"
    )
    assert (
        extract_signal({"final_trade_decision": "**Rating**: Underweight\n"})
        == "Sell"
    )
