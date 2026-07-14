"""观察 briefing 持久化与 observe 写入。"""

from datetime import datetime

from tradingagents.watchlist.models import (
    Baseline,
    MarketSnapshot,
    ObservationBriefing,
    ScenarioOutlook,
    WatchItem,
)
from tradingagents.watchlist.observe import observe_item
from tradingagents.watchlist.store import WatchlistStore


def _item() -> WatchItem:
    return WatchItem(
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


def test_briefing_roundtrip_in_store(tmp_path):
    store = WatchlistStore(tmp_path / "w.json")
    store.add(_item())
    briefing = ObservationBriefing(
        market_brief="微跌，板块偏弱",
        lean="neutral",
        lean_reason="缺少方向性催化剂",
        scenarios={
            "optimistic": ScenarioOutlook(view="反弹", reason="超跌"),
            "neutral": ScenarioOutlook(view="震荡", reason="观望"),
            "pessimistic": ScenarioOutlook(view="下行", reason="资金流出"),
        },
    )
    store.mark_observed(
        "002648",
        "2026-07-14T09:35:00",
        summary="维持",
        briefing=briefing,
    )
    item = store.get("002648")
    assert item is not None
    assert item.last_briefing is not None
    assert item.last_briefing.market_brief == "微跌，板块偏弱"
    assert item.last_briefing.lean == "neutral"
    assert item.last_briefing.scenarios["optimistic"].view == "反弹"


def test_observe_force_runs_when_disabled(monkeypatch, tmp_path):
    from tradingagents.watchlist import observe as observe_mod

    store = WatchlistStore(tmp_path / "w.json")
    item = _item()
    item.enabled = False
    store.add(item)
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
            "summary": "维持",
            "watch_point": "",
            "avoid": "",
            "market_brief": "手动观察仍可更新",
            "lean": "neutral",
            "lean_reason": "开关只影响自动",
            "scenarios": {
                "optimistic": {"view": "a", "reason": "ra"},
                "neutral": {"view": "b", "reason": "rb"},
                "pessimistic": {"view": "c", "reason": "rc"},
            },
        },
    )
    skipped = observe_item(store.get("002648"), store=store, llm=object())
    assert skipped == []
    assert store.get("002648").last_briefing is None

    alerts = observe_item(
        store.get("002648"),
        store=store,
        llm=object(),
        force=True,
        slot_key="manual-1",
        now=datetime(2026, 7, 14, 9, 36),
    )
    assert alerts == []
    saved = store.get("002648")
    assert saved is not None
    assert saved.last_briefing is not None
    assert "手动观察" in saved.last_briefing.market_brief


def test_observe_persists_briefing_even_without_alerts(monkeypatch, tmp_path):
    from tradingagents.watchlist import observe as observe_mod

    store = WatchlistStore(tmp_path / "w.json")
    store.add(_item())
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
            "summary": "维持",
            "watch_point": "量能",
            "avoid": "追高",
            "market_brief": "小幅上涨，交投一般",
            "lean": "optimistic",
            "lean_reason": "价格站上基准附近",
            "scenarios": {
                "optimistic": {"view": "续涨", "reason": "量价配合"},
                "neutral": {"view": "横盘", "reason": "等待确认"},
                "pessimistic": {"view": "回落", "reason": "冲高回落"},
            },
        },
    )
    alerts = observe_item(
        store.get("002648"),
        store=store,
        llm=object(),
        slot_key="2026-07-14T09:35",
        now=datetime(2026, 7, 14, 9, 36),
    )
    assert alerts == []
    item = store.get("002648")
    assert item is not None
    assert item.last_briefing is not None
    assert item.last_briefing.market_brief == "小幅上涨，交投一般"
    assert item.last_briefing.lean == "optimistic"
    assert "续涨" in item.last_briefing.scenarios["optimistic"].view
    assert item.last_summary
    assert "小幅上涨" in item.last_summary or "维持" in item.last_summary
