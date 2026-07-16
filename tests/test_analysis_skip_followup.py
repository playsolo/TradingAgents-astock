"""扫描跳过 → 观察池跟进与升级。"""

from __future__ import annotations

from tradingagents.analysis.skip_followup import follow_up_scan_skip
from tradingagents.watchlist.models import Alert, Baseline, WatchItem
from tradingagents.watchlist.store import WatchlistStore


def _baseline(**kwargs) -> Baseline:
    data = dict(
        ticker="002648",
        trade_date="2026-07-13",
        market="CN",
        stance="Hold",
        position_pct=10.0,
        baseline_price=100.0,
        entry_price=None,
        stop_loss=90.0,
        thesis_summary="x",
        major_risks=[],
        log_path="/tmp/x.json",
        valid_trading_days=5,
    )
    data.update(kwargs)
    return Baseline(**data)


def test_follow_up_noop_without_watch_item(tmp_path):
    store = WatchlistStore(tmp_path / "w.json")
    out = follow_up_scan_skip(
        "002648",
        trade_date="2026-07-14",
        watch_store=store,
        escalate=True,
    )
    assert out["observed"] is False
    assert out["escalated"] is False


def test_follow_up_observes_and_escalates_on_high_alert(tmp_path, monkeypatch):
    store = WatchlistStore(tmp_path / "w.json")
    store.add(WatchItem(baseline=_baseline(), enabled=True))

    alert = Alert(
        kind="stop_loss",
        title="破止损",
        detail="89<90",
        observed_at="2026-07-14T10:00:00",
    )
    monkeypatch.setattr(
        "tradingagents.watchlist.observe.observe_item",
        lambda *a, **k: [alert],
    )

    enqueued: list = []

    class _Store:
        def append_atomic(self, jobs):
            enqueued.extend(jobs)
            return len(jobs)

    monkeypatch.setattr(
        "web.analysis_queue.AnalysisQueueStore",
        lambda: _Store(),
    )
    monkeypatch.setattr(
        "tradingagents.inbox.emit",
        lambda *a, **k: {},
    )

    out = follow_up_scan_skip(
        "002648",
        trade_date="2026-07-14",
        watch_store=store,
        escalate=True,
    )
    assert out["observed"] is True
    assert out["escalated"] is True
    assert len(enqueued) == 1
    assert enqueued[0].force_full_reeval is True


def test_follow_up_no_escalate_on_soft_alert(tmp_path, monkeypatch):
    store = WatchlistStore(tmp_path / "w.json")
    store.add(WatchItem(baseline=_baseline(), enabled=True))
    alert = Alert(
        kind="price",
        title="偏离",
        detail="5%",
        observed_at="2026-07-14T10:00:00",
    )
    monkeypatch.setattr(
        "tradingagents.watchlist.observe.observe_item",
        lambda *a, **k: [alert],
    )
    calls = {"n": 0}

    class _Store:
        def append_atomic(self, jobs):
            calls["n"] += 1
            return 0

    monkeypatch.setattr("web.analysis_queue.AnalysisQueueStore", lambda: _Store())
    out = follow_up_scan_skip(
        "002648",
        trade_date="2026-07-14",
        watch_store=store,
        escalate=True,
    )
    assert out["observed"] is True
    assert out["escalated"] is False
    assert calls["n"] == 0
