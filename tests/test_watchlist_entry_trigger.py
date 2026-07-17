"""观察池：触及入场价告警 + 扫描分析后自动入池。"""

from tradingagents.watchlist.models import Baseline, MarketSnapshot
from tradingagents.watchlist.rules import detect_changes
from tradingagents.watchlist.service import maybe_auto_watch_from_scan_analysis
from tradingagents.watchlist.store import WatchlistStore


def _baseline(**kwargs):
    data = dict(
        ticker="002648",
        trade_date="2026-07-13",
        market="CN",
        stance="Hold",
        position_pct=10.0,
        baseline_price=100.0,
        entry_price=95.0,
        stop_loss=90.0,
        thesis_summary="等回撤",
        major_risks=[],
        log_path="/tmp/x.json",
    )
    data.update(kwargs)
    return Baseline(**data)


def test_entry_trigger_when_price_at_or_below_entry():
    snap = MarketSnapshot(price=94.5, change_pct=-5.5, name="x")
    alerts = detect_changes(
        _baseline(entry_price=95.0),
        snap,
        suggested_stance="Hold",
        suggested_position_pct=10.0,
        new_major_risks=[],
    )
    assert any(a.kind == "entry" for a in alerts)


def test_no_entry_trigger_when_above_entry():
    snap = MarketSnapshot(price=96.0, change_pct=-4.0, name="x")
    alerts = detect_changes(
        _baseline(entry_price=95.0),
        snap,
        suggested_stance="Hold",
        suggested_position_pct=10.0,
        new_major_risks=[],
    )
    assert not any(a.kind == "entry" for a in alerts)


def test_no_entry_trigger_without_entry_price():
    snap = MarketSnapshot(price=90.0, change_pct=-10.0, name="x")
    alerts = detect_changes(
        _baseline(entry_price=None),
        snap,
        suggested_stance="Hold",
        suggested_position_pct=10.0,
        new_major_risks=[],
    )
    assert not any(a.kind == "entry" for a in alerts)


def test_auto_watch_hold_with_entry(tmp_path, monkeypatch):
    store = WatchlistStore(path=tmp_path / "watch.json")
    monkeypatch.setattr(
        "tradingagents.watchlist.service._current_price",
        lambda *a, **k: 100.0,
    )
    item = maybe_auto_watch_from_scan_analysis(
        {
            "final_trade_decision": "**Rating**: Hold\n**Entry Price**: 92.0\n等回撤再买。",
            "trader_investment_decision": "入场价：92",
        },
        ticker="002648",
        trade_date="2026-07-16",
        market="CN",
        source="scan",
        store=store,
    )
    assert item is not None
    assert store.get("002648") is not None
    assert item.baseline.entry_price == 92.0


def test_auto_watch_skips_sell_without_usable_setup(tmp_path, monkeypatch):
    store = WatchlistStore(path=tmp_path / "watch.json")
    monkeypatch.setattr(
        "tradingagents.watchlist.service._current_price",
        lambda *a, **k: 100.0,
    )
    item = maybe_auto_watch_from_scan_analysis(
        {"final_trade_decision": "**Rating**: Sell\n不宜介入。"},
        ticker="002648",
        trade_date="2026-07-16",
        market="CN",
        source="scan",
        store=store,
    )
    assert item is None
    assert store.get("002648") is None


def test_auto_watch_skips_non_scan_source(tmp_path, monkeypatch):
    store = WatchlistStore(path=tmp_path / "watch.json")
    monkeypatch.setattr(
        "tradingagents.watchlist.service._current_price",
        lambda *a, **k: 100.0,
    )
    item = maybe_auto_watch_from_scan_analysis(
        {
            "final_trade_decision": "**Rating**: Hold\n**Entry Price**: 92.0",
            "trader_investment_decision": "入场价：92",
        },
        ticker="002648",
        trade_date="2026-07-16",
        market="CN",
        source="manual",
        store=store,
    )
    assert item is None
