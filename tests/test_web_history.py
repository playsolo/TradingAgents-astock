"""Tests for Web history helpers."""

from __future__ import annotations

import json
import os
import threading

from web import history


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


def test_completed_history_hides_incomplete_task(tmp_path, monkeypatch):
    index = tmp_path / "incomplete_tasks.json"
    logs = tmp_path / "logs"
    log_dir = logs / "600370" / "TradingAgentsStrategy_logs"
    log_dir.mkdir(parents=True)
    (log_dir / "full_states_log_2026-06-02.json").write_text(
        json.dumps({"final_trade_decision": "HOLD"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(history, "_INCOMPLETE_TASKS_FILE", index)
    monkeypatch.setattr(history, "_results_dir", lambda: logs)
    monkeypatch.setattr(history, "_checkpoint_step", lambda ticker, trade_date: 3)

    history.record_incomplete_task("600370", "2026-06-02", status="running")

    assert history.get_incomplete_history() == []


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
