"""观察复核产生的 Alert 应镜像到站内事件中心（inbox）。"""

from __future__ import annotations

import threading
from datetime import datetime

from tradingagents import inbox
from tradingagents.watchlist import observe as observe_mod
from tradingagents.watchlist.models import Alert, Baseline, MarketSnapshot, WatchItem
from tradingagents.watchlist.store import WatchlistStore


def _baseline() -> Baseline:
    return Baseline(
        ticker="002648",
        trade_date="2026-07-13",
        market="CN",
        stance="Hold",
        position_pct=10.0,
        baseline_price=100.0,
        entry_price=None,
        stop_loss=None,
        thesis_summary="t",
        major_risks=[],
        log_path="",
    )


def _judgment() -> dict:
    return {
        "suggested_stance": "Hold",
        "suggested_position_pct": 10.0,
        "new_major_risks": [],
        "summary": "ok",
        "watch_point": "",
        "avoid": "",
        "market_brief": "窄幅震荡",
        "lean": "neutral",
        "lean_reason": "缺乏催化剂",
        "scenarios": {},
    }


def test_light_observe_mirrors_alerts_into_inbox(tmp_path, monkeypatch):
    monkeypatch.setattr(inbox, "_INBOX_FILE", tmp_path / "inbox.json")
    monkeypatch.setattr(inbox, "_LOCK", threading.Lock())

    store = WatchlistStore(tmp_path / "w.json")
    item = WatchItem(baseline=_baseline())
    store.add(item)

    monkeypatch.setattr(
        observe_mod,
        "fetch_snapshot",
        lambda *_a, **_k: MarketSnapshot(price=101.0, change_pct=1.0, name="x"),
    )
    monkeypatch.setattr(observe_mod, "judge_vs_baseline", lambda *_a, **_k: _judgment())
    fake_alert = Alert(
        kind="stance",
        title="立场变化",
        detail="Hold → Buy",
        observed_at="2026-07-14T09:30:00",
    )
    monkeypatch.setattr(observe_mod, "detect_changes", lambda *_a, **_k: [fake_alert])

    alerts = observe_mod.observe_item(
        item,
        store=store,
        llm=None,
        force=True,
        now=datetime(2026, 7, 14, 9, 36),
    )

    assert alerts
    events = inbox.list_events()
    assert len(events) == 1
    assert events[0]["kind"] == inbox.KIND_WATCH_ALERT
    assert events[0]["ticker"] == "002648"
    assert events[0]["link_view"] == "watch"


def test_light_observe_without_alerts_emits_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(inbox, "_INBOX_FILE", tmp_path / "inbox.json")
    monkeypatch.setattr(inbox, "_LOCK", threading.Lock())

    store = WatchlistStore(tmp_path / "w.json")
    item = WatchItem(baseline=_baseline())
    store.add(item)

    monkeypatch.setattr(
        observe_mod,
        "fetch_snapshot",
        lambda *_a, **_k: MarketSnapshot(price=101.0, change_pct=1.0, name="x"),
    )
    monkeypatch.setattr(observe_mod, "judge_vs_baseline", lambda *_a, **_k: _judgment())
    monkeypatch.setattr(observe_mod, "detect_changes", lambda *_a, **_k: [])

    observe_mod.observe_item(
        item,
        store=store,
        llm=None,
        force=True,
        now=datetime(2026, 7, 14, 9, 36),
    )

    assert inbox.list_events() == []
