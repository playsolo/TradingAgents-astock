"""US watchlist: snapshot routing, observe not skipped, calendar slots."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from tradingagents.watchlist.calendar import (
    US_OBSERVE_SLOTS,
    active_observe_slots,
    slot_key_for_us,
)
from tradingagents.watchlist.models import Baseline, MarketSnapshot, WatchItem
from tradingagents.watchlist.observe import observe_item
from tradingagents.watchlist.service import add_from_analysis
from tradingagents.watchlist.snapshot import fetch_snapshot
from tradingagents.watchlist.store import WatchlistStore


def test_us_observe_slots_defaults():
    assert US_OBSERVE_SLOTS == [(9, 35), (12, 5), (16, 5)]


def test_slot_key_for_us_within_window():
    et = ZoneInfo("America/New_York")
    assert slot_key_for_us(datetime(2026, 7, 13, 9, 36, tzinfo=et)) == "US:2026-07-13T09:35"
    assert slot_key_for_us(datetime(2026, 7, 13, 9, 42, tzinfo=et)) is None
    assert slot_key_for_us(datetime(2026, 7, 13, 16, 5, tzinfo=et)) == "US:2026-07-13T16:05"


def test_active_observe_slots_can_include_us():
    et = ZoneInfo("America/New_York")
    # 09:36 ET Monday → US slot; Beijing wall clock at that moment is ~21:36
    # so CN slot should be empty.
    us_now = datetime(2026, 7, 13, 9, 36, tzinfo=et)
    active = active_observe_slots(us_now)
    assert ("US", "US:2026-07-13T09:35") in active


def test_fetch_snapshot_routes_us(monkeypatch):
    monkeypatch.setattr(
        "tradingagents.watchlist.snapshot._fetch_us_snapshot",
        lambda ticker, max_headlines=8: MarketSnapshot(
            price=190.0, change_pct=1.2, name="Apple", headlines=["Apple news headline"]
        ),
    )
    snap = fetch_snapshot("AAPL", market="US")
    assert snap.price == 190.0
    assert snap.main_net_inflow is None
    assert snap.name == "Apple"


def test_observe_item_runs_for_us(monkeypatch, tmp_path):
    from tradingagents.watchlist import observe as observe_mod

    store = WatchlistStore(tmp_path / "w.json")
    item = WatchItem(
        baseline=Baseline(
            ticker="MSFT",
            trade_date="2026-07-13",
            market="US",
            stance="Hold",
            position_pct=10.0,
            baseline_price=400.0,
            entry_price=None,
            stop_loss=None,
            thesis_summary="us thesis",
            major_risks=[],
            log_path="",
            valid_trading_days=5,
        ),
        enabled=True,
    )
    store.add(item)

    monkeypatch.setattr(
        observe_mod,
        "fetch_snapshot",
        lambda ticker, market="CN", **_k: MarketSnapshot(
            price=410.0, change_pct=2.5, name="Microsoft"
        ),
    )
    monkeypatch.setattr(
        observe_mod,
        "judge_vs_baseline",
        lambda *_a, **_k: {
            "suggested_stance": "Hold",
            "suggested_position_pct": 10.0,
            "new_major_risks": [],
            "summary": "ok",
            "watch_point": "",
            "avoid": "",
            "market_brief": "平稳",
            "lean": "neutral",
            "lean_reason": "",
            "scenarios": {},
        },
    )

    alerts = observe_item(item, store=store, llm=None, force=True)
    assert alerts == []
    refreshed = store.get("MSFT")
    assert refreshed is not None
    assert refreshed.last_summary is not None


def test_add_from_analysis_sets_us_market(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "tradingagents.watchlist.service._current_price",
        lambda ticker, market="CN": 190.5,
    )
    store = WatchlistStore(tmp_path / "w.json")
    item = add_from_analysis(
        {
            "final_trade_decision": "**Rating**: Hold\n观望。",
            "trader_investment_decision": "position sizing: 10%",
        },
        ticker="AAPL",
        trade_date="2026-07-16",
        store=store,
        market="US",
    )
    assert item.baseline.market == "US"
    assert item.baseline.ticker == "AAPL"
    assert item.baseline.baseline_price == 190.5
