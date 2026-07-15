"""Web 侧终态 → inbox 事件适配（notify_tracker_terminal）。"""

from __future__ import annotations

import threading

from tradingagents import inbox
from web.events import notify_tracker_terminal
from web.progress import ProgressTracker


def _isolate_inbox(tmp_path, monkeypatch):
    monkeypatch.setattr(inbox, "_INBOX_FILE", tmp_path / "inbox.json")
    monkeypatch.setattr(inbox, "_LOCK", threading.Lock())


def test_complete_tracker_emits_completion_event(tmp_path, monkeypatch):
    _isolate_inbox(tmp_path, monkeypatch)
    tracker = ProgressTracker(ticker="300750", trade_date="2026-07-14")
    tracker.is_complete = True
    tracker.signal = "Buy"
    tracker.final_state = {}

    notify_tracker_terminal(tracker)

    events = inbox.list_events()
    assert len(events) == 1
    assert events[0]["kind"] == inbox.KIND_ANALYSIS_COMPLETE
    assert events[0]["ticker"] == "300750"
    assert "买入" in events[0]["detail"]


def test_error_tracker_emits_failure_event(tmp_path, monkeypatch):
    _isolate_inbox(tmp_path, monkeypatch)
    tracker = ProgressTracker(ticker="600519", trade_date="2026-07-14")
    tracker.error = "quota exceeded"

    notify_tracker_terminal(tracker)

    events = inbox.list_events()
    assert len(events) == 1
    assert events[0]["kind"] == inbox.KIND_ANALYSIS_FAILED
    assert events[0]["severity"] == "error"


def test_rerun_same_ticker_emits_fresh_notification(tmp_path, monkeypatch):
    # Emission happens once per run from the single worker thread, so a later
    # re-run of the same ticker/date must produce a NEW event (not be deduped).
    _isolate_inbox(tmp_path, monkeypatch)
    tracker = ProgressTracker(ticker="300750", trade_date="2026-07-14")
    tracker.is_complete = True
    tracker.signal = "Buy"
    tracker.final_state = {}

    notify_tracker_terminal(tracker)
    notify_tracker_terminal(tracker)

    completions = [
        e for e in inbox.list_events() if e["kind"] == inbox.KIND_ANALYSIS_COMPLETE
    ]
    assert len(completions) == 2


def test_notify_skips_when_ticker_or_date_missing(tmp_path, monkeypatch):
    _isolate_inbox(tmp_path, monkeypatch)
    tracker = ProgressTracker()  # no ticker / trade_date
    tracker.is_complete = True

    notify_tracker_terminal(tracker)

    assert inbox.list_events() == []


def test_completion_wins_over_late_cleanup_error(tmp_path, monkeypatch):
    # mark_complete succeeded; a later cleanup error set tracker.error while
    # is_complete stays true. Must still record success, not failure.
    _isolate_inbox(tmp_path, monkeypatch)
    tracker = ProgressTracker(ticker="300750", trade_date="2026-07-14")
    tracker.is_complete = True
    tracker.signal = "Buy"
    tracker.error = "clear_incomplete_task boom"

    notify_tracker_terminal(tracker)

    kinds = {e["kind"] for e in inbox.list_events()}
    assert inbox.KIND_ANALYSIS_COMPLETE in kinds
    assert inbox.KIND_ANALYSIS_FAILED not in kinds


def test_missing_data_warning_covers_flattened_stage_reports(tmp_path, monkeypatch):
    _isolate_inbox(tmp_path, monkeypatch)
    tracker = ProgressTracker(ticker="300750", trade_date="2026-07-14")
    tracker.is_complete = True
    tracker.signal = "Hold"
    # risk stage rendered text lives in stage_reports (raw state is a dict).
    tracker.stage_reports = {"risk": "结论：[数据缺失：融资融券接口SSL握手失败]"}

    notify_tracker_terminal(tracker)

    warnings = [
        e for e in inbox.list_events() if e["kind"] == inbox.KIND_ANALYSIS_WARNING
    ]
    assert warnings
    assert "风控评估" in warnings[0]["detail"]
