"""站内事件中心（inbox）持久化与未读状态。"""

from __future__ import annotations

from tradingagents import inbox


def test_emit_and_list_newest_first(tmp_path, monkeypatch):
    path = tmp_path / "inbox.json"
    monkeypatch.setattr(inbox, "_INBOX_FILE", path)
    monkeypatch.setattr(inbox, "_LOCK", __import__("threading").Lock())

    inbox.emit(
        inbox.KIND_ANALYSIS_COMPLETE,
        "300750 分析完成",
        severity="info",
        detail="信号：买入",
        ticker="300750",
        trade_date="2026-07-14",
        link_view="history",
    )
    inbox.emit(
        inbox.KIND_ANALYSIS_FAILED,
        "600519 分析失败",
        severity="error",
        detail="quota exceeded",
        ticker="600519",
        trade_date="2026-07-14",
        link_view="home",
    )

    events = inbox.list_events()
    assert [e["kind"] for e in events] == [
        inbox.KIND_ANALYSIS_FAILED,
        inbox.KIND_ANALYSIS_COMPLETE,
    ]
    assert events[0]["read"] is False
    assert events[0]["ticker"] == "600519"


def test_unread_count_and_mark_read(tmp_path, monkeypatch):
    path = tmp_path / "inbox.json"
    monkeypatch.setattr(inbox, "_INBOX_FILE", path)
    monkeypatch.setattr(inbox, "_LOCK", __import__("threading").Lock())

    a = inbox.emit(inbox.KIND_ANALYSIS_WARNING, "数据缺失", severity="warning")
    b = inbox.emit(inbox.KIND_WATCH_ALERT, "立场变化", severity="warning", ticker="002648")

    assert inbox.unread_count() == 2
    assert inbox.mark_read(a["id"]) is True
    assert inbox.unread_count() == 1
    assert inbox.list_events(unread_only=True)[0]["id"] == b["id"]

    n = inbox.mark_all_read()
    assert n == 1
    assert inbox.unread_count() == 0


def test_dedupe_key_skips_duplicate_unread(tmp_path, monkeypatch):
    path = tmp_path / "inbox.json"
    monkeypatch.setattr(inbox, "_INBOX_FILE", path)
    monkeypatch.setattr(inbox, "_LOCK", __import__("threading").Lock())

    first = inbox.emit(
        inbox.KIND_WATCH_ALERT,
        "止损触及",
        ticker="002648",
        dedupe_key="watch:002648:stop_loss:2026-07-14T10:00:00",
    )
    second = inbox.emit(
        inbox.KIND_WATCH_ALERT,
        "止损触及（重复）",
        ticker="002648",
        dedupe_key="watch:002648:stop_loss:2026-07-14T10:00:00",
    )
    assert second["id"] == first["id"]
    assert len(inbox.list_events()) == 1


def test_emit_analysis_complete_adds_warning_event(tmp_path, monkeypatch):
    path = tmp_path / "inbox.json"
    monkeypatch.setattr(inbox, "_INBOX_FILE", path)
    monkeypatch.setattr(inbox, "_LOCK", __import__("threading").Lock())

    inbox.emit_analysis_complete(
        "300750",
        "2026-07-14",
        "Buy",
        warnings=["基本面：资产负债率"],
    )
    kinds = [e["kind"] for e in inbox.list_events()]
    assert kinds == [inbox.KIND_ANALYSIS_WARNING, inbox.KIND_ANALYSIS_COMPLETE]
    assert "买入" in inbox.list_events()[-1]["detail"]


def test_emit_watch_alerts_creates_one_per_alert(tmp_path, monkeypatch):
    path = tmp_path / "inbox.json"
    monkeypatch.setattr(inbox, "_INBOX_FILE", path)
    monkeypatch.setattr(inbox, "_LOCK", __import__("threading").Lock())

    class _Alert:
        kind = "stance"
        title = "立场变化"
        detail = "Hold → Buy"
        observed_at = "2026-07-14T09:30:00"

    inbox.emit_watch_alerts("002648", [_Alert()])
    events = inbox.list_events()
    assert len(events) == 1
    assert events[0]["kind"] == inbox.KIND_WATCH_ALERT
    assert events[0]["link_view"] == "watch"
    assert events[0]["title"] == "002648 立场变化"


def test_max_keep_trims_oldest(tmp_path, monkeypatch):
    path = tmp_path / "inbox.json"
    monkeypatch.setattr(inbox, "_INBOX_FILE", path)
    monkeypatch.setattr(inbox, "_LOCK", __import__("threading").Lock())
    monkeypatch.setattr(inbox, "MAX_EVENTS", 3)

    for i in range(5):
        inbox.emit(inbox.KIND_ANALYSIS_COMPLETE, f"e{i}", ticker=str(i))

    events = inbox.list_events()
    assert len(events) == 3
    assert [e["title"] for e in events] == ["e4", "e3", "e2"]
