"""观察执行：时段去重 + 规则告警写入；观察全部批处理。"""

from datetime import datetime

from tradingagents.watchlist.models import Baseline, MarketSnapshot, WatchItem
from tradingagents.watchlist.observe import (
    format_observe_batch_summary,
    observe_all,
    observe_item,
)
from tradingagents.watchlist.store import WatchlistStore


def _item(
    ticker: str,
    *,
    enabled: bool = True,
    trade_date: str = "2026-07-13",
    valid_trading_days: int = 3,
) -> WatchItem:
    return WatchItem(
        baseline=Baseline(
            ticker=ticker,
            trade_date=trade_date,
            market="CN",
            stance="Hold",
            position_pct=10.0,
            baseline_price=100.0,
            entry_price=None,
            stop_loss=None,
            thesis_summary="t",
            major_risks=[],
            log_path="",
            valid_trading_days=valid_trading_days,
        ),
        enabled=enabled,
    )


def _patch_light_observe(monkeypatch, *, fail_tickers: set[str] | None = None):
    from tradingagents.watchlist import observe as observe_mod

    fail_tickers = fail_tickers or set()

    def _snap(ticker, *_a, **_k):
        if ticker in fail_tickers:
            raise RuntimeError(f"boom-{ticker}")
        return MarketSnapshot(price=101.0, change_pct=1.0, name=ticker)

    monkeypatch.setattr(observe_mod, "fetch_snapshot", _snap)
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


def test_observe_writes_alerts_and_skips_same_slot(monkeypatch, tmp_path):
    from tradingagents.watchlist import observe as observe_mod

    store = WatchlistStore(tmp_path / "w.json")
    item = WatchItem(
        baseline=Baseline(
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
    )
    store.add(item)

    monkeypatch.setattr(
        observe_mod,
        "fetch_snapshot",
        lambda *_a, **_k: MarketSnapshot(price=108.0, change_pct=8.0, name="卫星化学"),
    )
    monkeypatch.setattr(
        observe_mod,
        "judge_vs_baseline",
        lambda *_a, **_k: {
            "suggested_stance": "Buy",
            "suggested_position_pct": 10.0,
            "new_major_risks": [],
            "summary": "上修",
            "watch_point": "量能",
            "avoid": "追高",
            "market_brief": "放量偏离基准",
            "lean": "optimistic",
            "lean_reason": "价格上破",
            "scenarios": {
                "optimistic": {"view": "续涨", "reason": "量价"},
                "neutral": {"view": "横盘", "reason": "观望"},
                "pessimistic": {"view": "回落", "reason": "获利了结"},
            },
        },
    )

    alerts = observe_item(
        store.get("002648"),
        store=store,
        llm=None,
        slot_key="2026-07-14T09:35",
        now=datetime(2026, 7, 14, 9, 36),
    )
    assert any(a.kind == "stance" for a in alerts)
    assert any(a.kind == "price" for a in alerts)

    again = observe_item(
        store.get("002648"),
        store=store,
        llm=None,
        slot_key="2026-07-14T09:35",
        now=datetime(2026, 7, 14, 9, 37),
    )
    assert again == []


def test_observe_all_runs_enabled_skips_disabled_and_expired(monkeypatch, tmp_path):
    _patch_light_observe(monkeypatch)
    store = WatchlistStore(tmp_path / "w.json")
    store.add(_item("002648"))  # eligible
    store.add(_item("300750", enabled=False))  # disabled
    store.add(_item("600519", trade_date="2026-07-01", valid_trading_days=1))  # expired

    now = datetime(2026, 7, 14, 10, 0)
    result = observe_all(
        store,
        llm=None,
        slot_key="manual-batch-1",
        now=now,
    )

    assert result.observed == ["002648"]
    assert result.skipped_expired == ["600519"]
    assert "300750" not in result.alerts
    assert "300750" not in result.failed
    assert store.get("002648").last_slot_key == "manual-batch-1"
    assert store.get("300750").last_observed_at is None
    assert store.get("600519").last_observed_at is None


def test_observe_all_continues_after_failure(monkeypatch, tmp_path):
    _patch_light_observe(monkeypatch, fail_tickers={"002648"})
    store = WatchlistStore(tmp_path / "w.json")
    store.add(_item("002648"))
    store.add(_item("300750"))

    result = observe_all(
        store,
        llm=None,
        slot_key="manual-batch-2",
        now=datetime(2026, 7, 14, 10, 0),
    )

    assert result.observed == ["300750"]
    assert set(result.failed) == {"002648"}
    assert "boom-002648" in result.failed["002648"]
    assert store.get("300750").last_slot_key == "manual-batch-2"
    assert store.get("002648").last_observed_at is None


def test_observe_all_force_rerun_with_new_slot(monkeypatch, tmp_path):
    _patch_light_observe(monkeypatch)
    store = WatchlistStore(tmp_path / "w.json")
    store.add(_item("002648"))
    now = datetime(2026, 7, 14, 10, 0)

    first = observe_all(store, slot_key="manual-a", now=now)
    assert first.observed == ["002648"]

    second = observe_all(store, slot_key="manual-b", now=now)
    assert second.observed == ["002648"]
    assert store.get("002648").last_slot_key == "manual-b"


def test_format_observe_batch_summary():
    from tradingagents.watchlist.observe import ObserveBatchResult
    from tradingagents.watchlist.models import Alert

    empty = ObserveBatchResult()
    assert "已观察 0 只" in format_observe_batch_summary(empty)

    rich = ObserveBatchResult(
        alerts={
            "002648": [
                Alert(
                    kind="price",
                    title="偏",
                    detail="d",
                    observed_at="2026-07-14T10:00:00",
                )
            ],
            "300750": [],
        },
        observed=["002648", "300750"],
        skipped_expired=["600519"],
        failed={"000001": "timeout"},
    )
    text = format_observe_batch_summary(rich)
    assert "已观察 2 只" in text
    assert "新告警 1 条" in text
    assert "跳过过期 1 只" in text
    assert "失败 1 只" in text
    assert "000001" in text
