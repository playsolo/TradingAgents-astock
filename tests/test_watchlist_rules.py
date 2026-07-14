"""观察池：变更检测规则。"""

from tradingagents.watchlist.models import Baseline, MarketSnapshot
from tradingagents.watchlist.rules import detect_changes


def _baseline(**kwargs):
    data = dict(
        ticker="002648",
        trade_date="2026-07-13",
        market="CN",
        stance="Hold",
        position_pct=10.0,
        baseline_price=100.0,
        entry_price=None,
        stop_loss=90.0,
        thesis_summary="基准逻辑",
        major_risks=["解禁临近"],
        log_path="/tmp/x.json",
    )
    data.update(kwargs)
    return Baseline(**data)


def test_stance_change_always_alerts():
    snap = MarketSnapshot(price=101.0, change_pct=1.0, name="卫星化学")
    alerts = detect_changes(
        _baseline(stance="Hold"),
        snap,
        suggested_stance="Buy",
        suggested_position_pct=10.0,
        new_major_risks=[],
    )
    kinds = {a.kind for a in alerts}
    assert "stance" in kinds


def test_position_change_alerts_when_exceeds_threshold():
    snap = MarketSnapshot(price=100.0, change_pct=0.0, name="x")
    alerts = detect_changes(
        _baseline(position_pct=10.0),
        snap,
        suggested_stance="Hold",
        suggested_position_pct=16.0,  # +6pp > 5
        new_major_risks=[],
        position_threshold_pp=5.0,
    )
    assert any(a.kind == "position" for a in alerts)


def test_position_change_ignored_within_threshold():
    snap = MarketSnapshot(price=100.0, change_pct=0.0, name="x")
    alerts = detect_changes(
        _baseline(position_pct=10.0),
        snap,
        suggested_stance="Hold",
        suggested_position_pct=14.0,  # +4pp
        new_major_risks=[],
        position_threshold_pp=5.0,
    )
    assert not any(a.kind == "position" for a in alerts)


def test_price_deviation_alerts():
    snap = MarketSnapshot(price=106.0, change_pct=6.0, name="x")
    alerts = detect_changes(
        _baseline(baseline_price=100.0, stance="Hold"),
        snap,
        suggested_stance="Hold",
        suggested_position_pct=10.0,
        new_major_risks=[],
        price_threshold_pct=5.0,
    )
    assert any(a.kind == "price" for a in alerts)


def test_stop_loss_breach_alerts():
    snap = MarketSnapshot(price=89.0, change_pct=-11.0, name="x")
    alerts = detect_changes(
        _baseline(stop_loss=90.0, stance="Hold"),
        snap,
        suggested_stance="Hold",
        suggested_position_pct=10.0,
        new_major_risks=[],
    )
    assert any(a.kind == "stop_loss" for a in alerts)


def test_new_major_risks_alert():
    snap = MarketSnapshot(price=100.0, change_pct=0.0, name="x")
    alerts = detect_changes(
        _baseline(major_risks=["解禁临近"]),
        snap,
        suggested_stance="Hold",
        suggested_position_pct=10.0,
        new_major_risks=["立案调查"],
    )
    assert any(a.kind == "risk" for a in alerts)


def test_no_alert_when_stable():
    snap = MarketSnapshot(price=101.0, change_pct=1.0, name="x")
    alerts = detect_changes(
        _baseline(),
        snap,
        suggested_stance="Hold",
        suggested_position_pct=10.0,
        new_major_risks=[],
    )
    assert alerts == []
