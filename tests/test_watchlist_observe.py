"""观察执行：时段去重 + 规则告警写入。"""

from datetime import datetime

from tradingagents.watchlist.models import Baseline, MarketSnapshot, WatchItem
from tradingagents.watchlist.observe import observe_item
from tradingagents.watchlist.store import WatchlistStore


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
