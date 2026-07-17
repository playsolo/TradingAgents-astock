"""观察池：同代码新报告完成后，已在池内则自动换绑基准。"""

from pathlib import Path

import pytest

from tradingagents.watchlist.models import Alert, Baseline, WatchItem
from tradingagents.watchlist.service import (
    maybe_auto_watch_from_scan_analysis,
    maybe_refresh_watched_from_analysis,
    refresh_watched_across_stores,
)
from tradingagents.watchlist.store import WatchlistStore, default_store


@pytest.fixture
def fake_home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def _baseline(**overrides) -> Baseline:
    data = dict(
        ticker="002648",
        trade_date="2026-07-13",
        market="CN",
        stance="Underweight",
        position_pct=5.0,
        baseline_price=23.44,
        entry_price=22.0,
        stop_loss=20.0,
        thesis_summary="旧基准",
        major_risks=["油价"],
        log_path="/tmp/old.json",
        horizon_raw="3个交易日",
        valid_trading_days=3,
    )
    data.update(overrides)
    return Baseline(**data)


def _new_state(*, stance: str = "Sell") -> dict:
    return {
        "final_trade_decision": f"**Rating**: {stance}\n逻辑更新，建议离场。",
        "trader_investment_decision": "建议仓位: 0%\nEntry Price: N/A",
        "investment_plan": "更新后结论",
        "action_plan": {
            "rating": stance,
            "holders_action": "减仓",
            "non_holders_action": "观望",
            "horizon": "3个交易日",
            "summary": "新报告",
            "levels": {},
        },
    }


def test_maybe_refresh_rebinds_when_already_watched(tmp_path, monkeypatch):
    store = WatchlistStore(tmp_path / "w.json")
    store.add(
        WatchItem(
            baseline=_baseline(),
            enabled=True,
            alerts=[
                Alert(
                    kind="price",
                    title="旧告警",
                    detail="d",
                    observed_at="2026-07-14T10:00:00",
                )
            ],
            last_summary="旧摘要",
        )
    )
    monkeypatch.setattr(
        "tradingagents.watchlist.service._current_price",
        lambda *a, **k: 24.0,
    )

    item = maybe_refresh_watched_from_analysis(
        _new_state(),
        ticker="002648",
        trade_date="2026-07-17",
        log_path="/tmp/new.json",
        store=store,
        market="CN",
    )

    assert item is not None
    assert item.baseline.trade_date == "2026-07-17"
    assert item.baseline.stance == "Sell"
    assert item.baseline.log_path == "/tmp/new.json"
    assert item.enabled is True
    assert len(item.alerts) == 1
    assert item.last_summary is None
    assert store.get("002648").baseline.trade_date == "2026-07-17"


def test_maybe_refresh_noop_when_not_in_pool(tmp_path, monkeypatch):
    store = WatchlistStore(tmp_path / "w.json")
    monkeypatch.setattr(
        "tradingagents.watchlist.service._current_price",
        lambda *a, **k: 24.0,
    )

    item = maybe_refresh_watched_from_analysis(
        _new_state(),
        ticker="002648",
        trade_date="2026-07-17",
        store=store,
    )
    assert item is None
    assert store.get("002648") is None


def test_maybe_refresh_rebinds_even_when_auto_observe_disabled(tmp_path, monkeypatch):
    """关自动观察仍展示基准；新报告应换绑，避免池内仍显示旧报告。"""
    store = WatchlistStore(tmp_path / "w.json")
    store.add(WatchItem(baseline=_baseline(), enabled=False))
    monkeypatch.setattr(
        "tradingagents.watchlist.service._current_price",
        lambda *a, **k: 24.0,
    )

    item = maybe_refresh_watched_from_analysis(
        _new_state(stance="Buy"),
        ticker="002648",
        trade_date="2026-07-17",
        store=store,
    )
    assert item is not None
    assert item.enabled is False
    assert item.baseline.stance == "Buy"
    assert item.baseline.trade_date == "2026-07-17"


def test_refresh_watched_across_stores_updates_all_users(fake_home, monkeypatch):
    monkeypatch.setattr(
        "tradingagents.watchlist.service._current_price",
        lambda *a, **k: 24.0,
    )
    alice = default_store("alice")
    bob = default_store("bob")
    alice.add(WatchItem(baseline=_baseline()))
    bob.add(WatchItem(baseline=_baseline(ticker="300750", trade_date="2026-07-10")))

    refreshed = refresh_watched_across_stores(
        _new_state(),
        ticker="002648",
        trade_date="2026-07-17",
        log_path="/tmp/new.json",
        market="CN",
    )

    assert len(refreshed) == 1
    assert alice.get("002648").baseline.trade_date == "2026-07-17"
    assert alice.get("002648").baseline.stance == "Sell"
    # Bob watches a different ticker — untouched.
    assert bob.get("300750").baseline.trade_date == "2026-07-10"


def test_manual_analysis_does_not_auto_add_but_does_rebind(tmp_path, monkeypatch):
    store = WatchlistStore(tmp_path / "w.json")
    monkeypatch.setattr(
        "tradingagents.watchlist.service._current_price",
        lambda *a, **k: 100.0,
    )
    # Not in pool → manual still does not auto-add.
    assert (
        maybe_refresh_watched_from_analysis(
            _new_state(stance="Hold"),
            ticker="002648",
            trade_date="2026-07-17",
            store=store,
        )
        is None
    )
    assert (
        maybe_auto_watch_from_scan_analysis(
            {
                "final_trade_decision": "**Rating**: Hold\n**Entry Price**: 92.0",
                "trader_investment_decision": "入场价：92",
            },
            ticker="002648",
            trade_date="2026-07-17",
            source="manual",
            store=store,
        )
        is None
    )

    store.add(WatchItem(baseline=_baseline()))
    item = maybe_refresh_watched_from_analysis(
        _new_state(stance="Sell"),
        ticker="002648",
        trade_date="2026-07-17",
        store=store,
    )
    assert item is not None
    assert item.baseline.stance == "Sell"


def test_run_one_job_rebinds_watched_ticker(monkeypatch, tmp_path):
    from web.analysis_queue import AnalysisJob
    from tradingagents.analyze_worker import executor

    store = WatchlistStore(tmp_path / "w.json")
    store.add(WatchItem(baseline=_baseline()))

    def fake_execute(ticker, trade_date, config, tracker, market="CN", **kw):
        tracker.signal = "SELL"
        tracker.final_state = _new_state()
        tracker.is_complete = True
        return tracker

    monkeypatch.setattr("web.runner.execute_analysis_run", fake_execute)
    monkeypatch.setattr(
        "tradingagents.analysis.mode_router.resolve_analysis_mode",
        lambda **kw: __import__(
            "tradingagents.analysis.mode_router", fromlist=["AnalysisRouteDecision"]
        ).AnalysisRouteDecision(mode="full_reeval", reason="force"),
    )
    monkeypatch.setattr(
        "tradingagents.analysis.persist.save_calibration_from_state",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        executor,
        "_default_watch_store",
        lambda: store,
    )
    monkeypatch.setattr(
        "tradingagents.watchlist.service._current_price",
        lambda *a, **k: 24.0,
    )
    # Force single-store path used when store is injected via across_stores helper.
    monkeypatch.setattr(
        "tradingagents.watchlist.service.iter_user_stores",
        lambda: iter([store]),
    )

    job = AnalysisJob(
        ticker="002648",
        trade_date="2026-07-17",
        market="CN",
        source="manual",
    )
    executor.run_one_job(job, {"llm_provider": "deepseek", "data_cache_dir": "/tmp"})

    assert store.get("002648").baseline.trade_date == "2026-07-17"
    assert store.get("002648").baseline.stance == "Sell"
