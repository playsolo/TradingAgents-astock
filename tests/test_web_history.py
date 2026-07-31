"""Tests for Web history helpers."""

from __future__ import annotations

import json
import os
import threading

from web import history


def test_extract_signal_prefers_labeled_underweight_over_body_buy():
    """Sidebar must follow 最终评级, not debate prose mentioning 买入.

    Regression: markdown ``**最终评级**：**减持**`` failed the colon regex,
    then the first whole-text ``买入`` (bull case) was shown as Buy while
    the report body recommended Underweight / 减持.
    """
    state = {
        "final_trade_decision": (
            "**投资决策备忘录**\n\n"
            "**最终评级**：**减持（Underweight）**\n\n"
            "牛方主张买入。熊方主张卖出。\n"
            "**执行方向**：不买入\n"
        ),
    }
    assert history.extract_signal(state) == "Underweight"


def test_extract_signal_reads_plain_jianchi_label():
    assert (
        history.extract_signal({"final_trade_decision": "最终评级：减持\n降低仓位"})
        == "Underweight"
    )


def test_extract_signal_hold_label_not_fooled_by_jianchi_prose():
    assert (
        history.extract_signal(
            {"final_trade_decision": "最终评级：持有（无减持计划）\n继续观察"}
        )
        == "Hold"
    )


def test_extract_signal_reads_prefixed_jianchi_label():
    assert (
        history.extract_signal({"final_trade_decision": "最终评级：建议减持\n降低敞口"})
        == "Underweight"
    )


def test_get_history_sorted_by_completion_mtime_desc(tmp_path, monkeypatch):
    """Sidebar history should list analyses by finish time, not trade date."""
    logs = tmp_path / "logs"
    monkeypatch.setattr(history, "_results_dir", lambda: logs)

    older = logs / "600000" / "TradingAgentsStrategy_logs"
    newer = logs / "000001" / "TradingAgentsStrategy_logs"
    older.mkdir(parents=True)
    newer.mkdir(parents=True)

    # Trade dates alone would put 600000 first; completion mtime should win.
    older_path = older / "full_states_log_2026-07-14.json"
    newer_path = newer / "full_states_log_2026-06-01.json"
    older_path.write_text(json.dumps({"final_trade_decision": "HOLD"}), encoding="utf-8")
    newer_path.write_text(json.dumps({"final_trade_decision": "BUY"}), encoding="utf-8")
    os.utime(older_path, (1_700_000_000, 1_700_000_000))
    os.utime(newer_path, (1_800_000_000, 1_800_000_000))

    entries = history.get_history()

    assert [e["ticker"] for e in entries] == ["000001", "600000"]
    assert [e["date"] for e in entries] == ["2026-06-01", "2026-07-14"]


def test_history_stamp_tracks_count_and_mtime(tmp_path, monkeypatch):
    """Worker-mode sidebar polls this stamp to refresh history without F5."""
    logs = tmp_path / "logs"
    monkeypatch.setattr(history, "_results_dir", lambda: logs)

    assert history.history_stamp() == (0, 0)

    first_dir = logs / "300750" / "TradingAgentsStrategy_logs"
    first_dir.mkdir(parents=True)
    first = first_dir / "full_states_log_2026-07-17.json"
    first.write_text(json.dumps({"final_trade_decision": "BUY"}), encoding="utf-8")
    os.utime(first, (1_700_000_000, 1_700_000_000))

    stamp1 = history.history_stamp()
    assert stamp1[0] == 1
    assert stamp1[1] > 0

    second_dir = logs / "600519" / "TradingAgentsStrategy_logs"
    second_dir.mkdir(parents=True)
    second = second_dir / "full_states_log_2026-07-17.json"
    second.write_text(json.dumps({"final_trade_decision": "HOLD"}), encoding="utf-8")
    os.utime(second, (1_800_000_000, 1_800_000_000))

    stamp2 = history.history_stamp()
    assert stamp2[0] == 2
    assert stamp2[1] > stamp1[1]

    # Rewrite same path (re-analysis) must bump stamp via mtime.
    second.write_text(json.dumps({"final_trade_decision": "SELL"}), encoding="utf-8")
    os.utime(second, (1_900_000_000, 1_900_000_000))
    stamp3 = history.history_stamp()
    assert stamp3[0] == 2
    assert stamp3[1] > stamp2[1]


def test_filter_history_by_ticker_case_insensitive_lists_all():
    entries = [
        {"ticker": "300750", "date": "2026-07-14", "path": "a", "signal": "Buy"},
        {"ticker": "AAPL", "date": "2026-07-13", "path": "b", "signal": "Hold"},
        {"ticker": "300750", "date": "2026-06-01", "path": "c", "signal": "Sell"},
        {"ticker": "aapl", "date": "2026-05-01", "path": "d", "signal": "Buy"},
    ]
    assert [e["date"] for e in history.filter_history_by_ticker(entries, "300750")] == [
        "2026-07-14",
        "2026-06-01",
    ]
    assert [e["path"] for e in history.filter_history_by_ticker(entries, "aapl")] == [
        "b",
        "d",
    ]
    assert history.filter_history_by_ticker(entries, "") == entries
    assert history.filter_history_by_ticker(entries, "NVDA") == []


def test_resolve_history_search_query_blank_means_no_filter():
    assert history.resolve_history_search_query("") == (None, None)
    assert history.resolve_history_search_query("   ") == (None, None)


def test_resolve_history_search_query_us_ticker():
    assert history.resolve_history_search_query("aapl") == ("AAPL", None)
    assert history.resolve_history_search_query("BRK.B") == ("BRK.B", None)


def test_resolve_history_search_query_cn_code():
    assert history.resolve_history_search_query("300750") == ("300750", None)


def test_resolve_history_search_query_cn_name_uses_cache(monkeypatch):
    monkeypatch.setattr(
        "web.stock_display.lookup_code_by_cached_name",
        lambda name: "300750" if name == "宁德时代" else None,
    )
    assert history.resolve_history_search_query("宁德时代") == ("300750", None)


def test_resolve_history_search_query_cn_name_falls_back_to_resolve(monkeypatch):
    monkeypatch.setattr(
        "web.stock_display.lookup_code_by_cached_name",
        lambda name: None,
    )

    def _fake_resolve(raw: str) -> str:
        if raw == "贵州茅台":
            return "600519"
        raise ValueError("x")

    monkeypatch.setattr(
        "tradingagents.dataflows.a_stock.resolve_ticker",
        _fake_resolve,
    )
    monkeypatch.setattr("web.stock_display.remember_resolved_name", lambda code, raw: None)
    assert history.resolve_history_search_query("贵州茅台") == ("600519", None)


def test_resolve_history_search_query_invalid_cn_name(monkeypatch):
    monkeypatch.setattr(
        "web.stock_display.lookup_code_by_cached_name",
        lambda name: None,
    )

    def _fake_resolve(raw: str) -> str:
        raise ValueError("找不到股票")

    monkeypatch.setattr(
        "tradingagents.dataflows.a_stock.resolve_ticker",
        _fake_resolve,
    )
    code, err = history.resolve_history_search_query("不存在的票")
    assert code is None
    assert err is not None
    assert "找不到" in err


def test_incomplete_task_round_trip(tmp_path, monkeypatch):
    index = tmp_path / "incomplete_tasks.json"
    logs = tmp_path / "logs"
    monkeypatch.setattr(history, "_INCOMPLETE_TASKS_FILE", index)
    monkeypatch.setattr(history, "_results_dir", lambda: logs)
    monkeypatch.setattr(history, "_checkpoint_step", lambda ticker, trade_date: 3)

    history.record_incomplete_task(
        "600370",
        "2026-06-02",
        status="error",
        error="quota exceeded",
        completed_stages=["market", "news"],
    )

    entries = history.get_incomplete_history()

    assert entries == [
        {
            "ticker": "600370",
            "trade_date": "2026-06-02",
            "status": "error",
            "error": "quota exceeded",
            "completed_stages": ["market", "news"],
            "resume_count": 0,
            "updated_at": entries[0]["updated_at"],
            "checkpoint_step": 3,
        }
    ]


def test_report_mtime_does_not_auto_drop_incomplete(tmp_path, monkeypatch):
    """Rewriting the report file must not hide resumable incompletes (repair UX)."""
    index = tmp_path / "incomplete_tasks.json"
    logs = tmp_path / "logs"
    log_dir = logs / "600370" / "TradingAgentsStrategy_logs"
    log_dir.mkdir(parents=True)
    log_path = log_dir / "full_states_log_2026-06-02.json"
    log_path.write_text(
        json.dumps({"final_trade_decision": "HOLD"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(history, "_INCOMPLETE_TASKS_FILE", index)
    monkeypatch.setattr(history, "_results_dir", lambda: logs)
    monkeypatch.setattr(history, "_checkpoint_step", lambda ticker, trade_date: 3)

    history.record_incomplete_task("600370", "2026-06-02", status="error", error="boom")
    newer = history._load_incomplete_index()[0]["updated_at"] + 10
    os.utime(log_path, (newer, newer))

    entries = history.get_incomplete_history()
    assert len(entries) == 1
    assert entries[0]["status"] == "error"

    history.clear_incomplete_task("600370", "2026-06-02")
    assert history.get_incomplete_history() == []


def test_running_reanalysis_keeps_incomplete_despite_prior_same_day_report(
    tmp_path, monkeypatch
):
    """Same ticker+date re-run must stay visible while in progress (refresh UX)."""
    index = tmp_path / "incomplete_tasks.json"
    logs = tmp_path / "logs"
    log_dir = logs / "002648" / "TradingAgentsStrategy_logs"
    log_dir.mkdir(parents=True)
    log_path = log_dir / "full_states_log_2026-07-14.json"
    log_path.write_text(
        json.dumps({"final_trade_decision": "HOLD"}),
        encoding="utf-8",
    )
    os.utime(log_path, (1_700_000_000, 1_700_000_000))

    monkeypatch.setattr(history, "_INCOMPLETE_TASKS_FILE", index)
    monkeypatch.setattr(history, "_results_dir", lambda: logs)
    monkeypatch.setattr(history, "_checkpoint_step", lambda ticker, trade_date: 5)

    history.record_incomplete_task(
        "002648",
        "2026-07-14",
        status="running",
        completed_stages=["market", "quality_gate"],
    )

    entries = history.get_incomplete_history()
    assert len(entries) == 1
    assert entries[0]["ticker"] == "002648"
    assert entries[0]["status"] == "running"
    assert entries[0]["completed_stages"] == ["market", "quality_gate"]
    # Must not wipe the on-disk incomplete index.
    assert history._load_incomplete_index()


def test_list_active_incomplete_for_refresh_notice(tmp_path, monkeypatch):
    index = tmp_path / "incomplete_tasks.json"
    logs = tmp_path / "logs"
    monkeypatch.setattr(history, "_INCOMPLETE_TASKS_FILE", index)
    monkeypatch.setattr(history, "_results_dir", lambda: logs)
    monkeypatch.setattr(history, "_checkpoint_step", lambda ticker, trade_date: 1)

    history.record_incomplete_task("300750", "2026-07-14", status="running")
    history.record_incomplete_task("600519", "2026-07-14", status="paused")
    history.record_incomplete_task(
        "000001", "2026-07-14", status="error", error="boom"
    )

    active = history.list_active_incomplete_tasks()
    assert {e["ticker"] for e in active} == {"000001", "600519", "300750"}
    assert {e["status"] for e in active} == {"running", "paused", "error"}

    notice = history.format_refresh_incomplete_notice(active)
    assert notice is not None
    assert "300750" in notice
    assert "未完成任务" in notice
    assert history.format_refresh_incomplete_notice([]) is None


def test_incomplete_task_writes_are_thread_safe(tmp_path, monkeypatch):
    index = tmp_path / "incomplete_tasks.json"
    logs = tmp_path / "logs"
    monkeypatch.setattr(history, "_INCOMPLETE_TASKS_FILE", index)
    monkeypatch.setattr(history, "_results_dir", lambda: logs)
    monkeypatch.setattr(history, "_checkpoint_step", lambda ticker, trade_date: 1)

    def write_task(i: int) -> None:
        history.record_incomplete_task(
            f"60037{i % 10}",
            "2026-06-02",
            status="running",
            completed_stages=["market"],
        )

    threads = [threading.Thread(target=write_task, args=(i,)) for i in range(30)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    entries = history.get_incomplete_history()

    assert len(entries) == 10
    assert {entry["status"] for entry in entries} == {"running"}
    assert not list(tmp_path.glob("*.tmp"))


def test_extract_signal_chinese_final_decision():
    """Chinese free-text decision must yield the real rating, not Hold/N/A.

    Regression for issues #78 / #80: history reload used an English-only
    BUY/SELL/HOLD scan that missed Chinese output entirely.
    """
    state = {
        "final_trade_decision": "最终评级：卖出\n核心结论：风险尚未出清。",
        "investment_plan": "研究经理倾向持有观望。",
    }
    assert history.extract_signal(state) == "Sell"


def test_extract_signal_prefers_final_trade_decision():
    """The reload signal must match the authoritative live signal source."""
    state = {
        "investment_plan": "最终评级：买入",
        "final_trade_decision": "最终评级：减持",
    }
    assert history.extract_signal(state) == "Underweight"


def test_extract_signal_english_still_works():
    state = {"final_trade_decision": "**Rating**: Buy\n\nThesis."}
    assert history.extract_signal(state) == "Buy"


def test_extract_signal_unknown_returns_na():
    assert history.extract_signal({"final_trade_decision": "无明确方向。"}) == "N/A"
    assert history.extract_signal({}) == "N/A"


def test_extract_signal_debate_prose_does_not_override_underweight():
    """Bare keyword scan must not match 买入 in debate prose when the
    structured label says 减持 (Underweight)."""
    state = {
        "final_trade_decision": (
            "## 风险管理辩论\n\n"
            "多头分析师: 主张买入，看涨到120元\n"
            "空头分析师: 建议减持\n\n"
            "**最终评级**：**减持（Underweight）**\n"
            "理由：估值偏高，下行风险加大"
        ),
    }
    assert history.extract_signal(state) == "Underweight"


def test_extract_signal_buy_in_debate_does_not_override_labeled_hold():
    """Structured final label must beat 买入/卖出 in bull/bear debate.
    This tests the 300014 scenario (Hold → incorrectly Buy)."""
    state = {
        "final_trade_decision": (
            "多头观点：建议买入，目标价40元\n"
            "空头观点：建议卖出\n\n"
            "**最终裁决：维持 Hold 评级**\n"
            "等待催化剂确认"
        ),
    }
    assert history.extract_signal(state) == "Hold"
