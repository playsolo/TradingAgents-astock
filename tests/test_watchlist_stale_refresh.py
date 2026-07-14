"""完整分析过期升级：交易日差判定、先验上下文、观察路由与基准回写。"""

from datetime import date, datetime

from tradingagents.watchlist.calendar import (
    FULL_ANALYSIS_STALE_TRADING_DAYS,
    cn_trading_days_since,
    is_full_analysis_stale,
)
from tradingagents.watchlist.models import Alert, Baseline, WatchItem
from tradingagents.watchlist.service import prior_context_from_baseline, refresh_from_analysis
from tradingagents.watchlist.store import WatchlistStore


def _baseline(**overrides) -> Baseline:
    data = dict(
        ticker="002648",
        trade_date="2026-07-13",  # Monday
        market="CN",
        stance="Hold",
        position_pct=10.0,
        baseline_price=100.0,
        entry_price=98.0,
        stop_loss=90.0,
        thesis_summary="卫星化学持有，关注量能",
        major_risks=["油价波动"],
        log_path="/tmp/log.json",
    )
    data.update(overrides)
    return Baseline(**data)


def test_full_analysis_stale_threshold_constant():
    assert FULL_ANALYSIS_STALE_TRADING_DAYS == 3


def test_cn_trading_days_since_skips_weekend():
    # Mon 7/13 → Thu 7/16 = Tue,Wed,Thu = 3
    assert cn_trading_days_since("2026-07-13", date(2026, 7, 16)) == 3
    # Mon 7/13 → Fri 7/17 = 4
    assert cn_trading_days_since("2026-07-13", date(2026, 7, 17)) == 4
    # Same day = 0
    assert cn_trading_days_since("2026-07-13", date(2026, 7, 13)) == 0
    # Weekend as_of counts through Friday only relative to Mon→Sun:
    # Mon→Sun: Tue-Fri = 4 trading days (Sat/Sun not counted as additional)
    assert cn_trading_days_since("2026-07-13", date(2026, 7, 19)) == 4


def test_is_full_analysis_stale_strictly_after_threshold():
    # 3 trading days later → not stale
    assert (
        is_full_analysis_stale("2026-07-13", datetime(2026, 7, 16, 15, 0)) is False
    )
    # 4 trading days later → stale
    assert is_full_analysis_stale("2026-07-13", datetime(2026, 7, 17, 9, 35)) is True


def test_prior_context_from_baseline_includes_key_fields():
    text = prior_context_from_baseline(_baseline())
    assert "002648" in text
    assert "2026-07-13" in text
    assert "Hold" in text
    assert "卫星化学持有" in text
    assert "油价波动" in text
    assert "100" in text


def test_refresh_from_analysis_updates_baseline_keeps_alerts(tmp_path):
    store = WatchlistStore(tmp_path / "w.json")
    old = WatchItem(
        baseline=_baseline(),
        enabled=True,
        alerts=[
            Alert(
                kind="price",
                title="偏离",
                detail="d",
                observed_at="2026-07-14T10:00:00",
            )
        ],
        last_summary="旧摘要",
    )
    store.add(old)

    state = {
        "final_trade_decision": "Rating: Buy\n建议仓位: 15%\n逻辑更新",
        "trader_investment_plan": "Entry Price: 105\nStop Loss: 95\n建议仓位: 15%",
        "investment_plan": "继续跟踪化工景气",
    }
    item = refresh_from_analysis(
        state,
        ticker="002648",
        trade_date="2026-07-17",
        log_path="/tmp/new.json",
        store=store,
        price=110.0,
    )
    assert item.baseline.trade_date == "2026-07-17"
    assert item.baseline.stance == "Buy"
    assert item.baseline.log_path == "/tmp/new.json"
    assert item.enabled is True
    assert len(item.alerts) == 1
    assert item.alerts[0].kind == "price"
    assert item.last_briefing is None
    assert item.last_summary is None
    stored = store.get("002648")
    assert stored is not None
    assert stored.baseline.trade_date == "2026-07-17"
    assert len(stored.alerts) == 1


def test_observe_item_runs_full_refresh_when_stale(monkeypatch, tmp_path):
    from tradingagents.watchlist import observe as observe_mod
    from tradingagents.watchlist.observe import observe_item

    store = WatchlistStore(tmp_path / "w.json")
    item = WatchItem(baseline=_baseline(trade_date="2026-07-13"))
    store.add(item)

    calls: list[str] = []

    def fake_refresh(watch_item, **_kwargs):
        calls.append(watch_item.baseline.ticker)
        return {
            "final_trade_decision": "Rating: Buy\n续写结论",
            "trader_investment_plan": "建议仓位: 12%",
            "investment_plan": "基于旧基准的更新",
        }

    monkeypatch.setattr(observe_mod, "run_full_refresh_analysis", fake_refresh)
    monkeypatch.setattr(
        observe_mod,
        "fetch_snapshot",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("light path")),
    )

    alerts = observe_item(
        store.get("002648"),
        store=store,
        llm=None,
        slot_key="2026-07-17T09:35",
        now=datetime(2026, 7, 17, 9, 36),
        force=True,
        analysis_config={"llm_provider": "deepseek"},
    )
    assert calls == ["002648"]
    refreshed = store.get("002648")
    assert refreshed is not None
    assert refreshed.baseline.trade_date == "2026-07-17"
    assert refreshed.last_observed_at is not None
    assert alerts == [] or isinstance(alerts, list)


def test_observe_item_light_path_when_not_stale(monkeypatch, tmp_path):
    from tradingagents.watchlist import observe as observe_mod
    from tradingagents.watchlist.models import MarketSnapshot
    from tradingagents.watchlist.observe import observe_item

    store = WatchlistStore(tmp_path / "w.json")
    store.add(WatchItem(baseline=_baseline(trade_date="2026-07-13")))

    monkeypatch.setattr(
        observe_mod,
        "run_full_refresh_analysis",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("full path")),
    )
    monkeypatch.setattr(
        observe_mod,
        "fetch_snapshot",
        lambda *_a, **_k: MarketSnapshot(price=101.0, change_pct=1.0, name="卫星化学"),
    )
    monkeypatch.setattr(
        observe_mod,
        "judge_vs_baseline",
        lambda *_a, **_k: {
            "suggested_stance": "Hold",
            "suggested_position_pct": 10.0,
            "new_major_risks": [],
            "summary": "ok",
            "market_brief": "平稳",
            "lean": "neutral",
            "lean_reason": "",
            "scenarios": {},
        },
    )

    observe_item(
        store.get("002648"),
        store=store,
        llm=None,
        slot_key="2026-07-14T09:35",
        now=datetime(2026, 7, 14, 9, 36),
        force=True,
        analysis_config={"llm_provider": "deepseek"},
    )
    assert store.get("002648").baseline.trade_date == "2026-07-13"
