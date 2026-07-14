"""观察池持久化。"""

from tradingagents.watchlist.models import Baseline, WatchItem
from tradingagents.watchlist.store import WatchlistStore


def _item(ticker="002648"):
    base = Baseline(
        ticker=ticker,
        trade_date="2026-07-13",
        market="CN",
        stance="Hold",
        position_pct=10.0,
        baseline_price=20.0,
        entry_price=None,
        stop_loss=None,
        thesis_summary="test",
        major_risks=[],
        log_path="/tmp/a.json",
    )
    return WatchItem(baseline=base, enabled=True, alerts=[], last_observed_at=None)


def test_add_list_remove(tmp_path):
    store = WatchlistStore(tmp_path / "watchlist.json")
    store.add(_item("002648"))
    store.add(_item("300750"))
    assert {i.baseline.ticker for i in store.list_items()} == {"002648", "300750"}
    store.remove("002648")
    assert [i.baseline.ticker for i in store.list_items()] == ["300750"]


def test_add_replaces_same_ticker(tmp_path):
    store = WatchlistStore(tmp_path / "watchlist.json")
    store.add(_item("002648"))
    other = _item("002648")
    other.baseline.stance = "Buy"
    store.add(other)
    items = store.list_items()
    assert len(items) == 1
    assert items[0].baseline.stance == "Buy"


def test_append_alert_and_mark_observed(tmp_path):
    from tradingagents.watchlist.models import Alert

    store = WatchlistStore(tmp_path / "watchlist.json")
    store.add(_item("002648"))
    store.append_alerts(
        "002648",
        [Alert(kind="stance", title="立场变化", detail="Hold → Buy", observed_at="2026-07-14T09:35:00")],
    )
    store.mark_observed("002648", "2026-07-14T09:35:00")
    item = store.get("002648")
    assert item is not None
    assert item.last_observed_at == "2026-07-14T09:35:00"
    assert len(item.alerts) == 1
