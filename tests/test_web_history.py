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
    assert history.extract_signal(state) == "Sell"


def test_extract_signal_reads_plain_jianchi_label():
    assert (
        history.extract_signal({"final_trade_decision": "最终评级：减持\n降低仓位"})
        == "Sell"
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
        == "Sell"
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
