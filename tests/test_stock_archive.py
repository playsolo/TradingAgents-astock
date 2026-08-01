"""P0 stock archive: per-ticker plan, delta, prior (same-ticker only)."""

from __future__ import annotations

from tradingagents.archive.delta import compute_plan_delta
from tradingagents.archive.models import ActivePlan
from tradingagents.archive.prior import build_archive_prior
from tradingagents.archive.store import StockArchiveStore
from tradingagents.watchlist.models import Baseline


def _baseline(**kwargs) -> Baseline:
    data = dict(
        ticker="002648",
        trade_date="2026-07-13",
        market="CN",
        stance="Hold",
        position_pct=10.0,
        baseline_price=100.0,
        entry_price=98.0,
        stop_loss=90.0,
        thesis_summary="原逻辑仍在",
        major_risks=["解禁"],
        log_path="/tmp/x.json",
        valid_trading_days=5,
    )
    data.update(kwargs)
    return Baseline(**data)


def test_save_and_load_active_plan(tmp_path):
    store = StockArchiveStore(tmp_path / "archives")
    plan = ActivePlan.from_baseline(
        _baseline(), invalidation="跌破90或解禁超预期", plan_version=1
    )
    store.save_active_plan(plan)
    loaded = store.get_active_plan("002648", "CN")
    assert loaded is not None
    assert loaded.stance == "Hold"
    assert loaded.invalidation.startswith("跌破")
    assert (tmp_path / "archives" / "CN" / "002648" / "active_plan.json").exists()
    assert (tmp_path / "archives" / "CN" / "002648" / "meta.json").exists()


def test_void_plan_not_returned_as_anchor(tmp_path):
    store = StockArchiveStore(tmp_path / "archives")
    store.save_active_plan(ActivePlan.from_baseline(_baseline()))
    assert store.void_plan("002648", "CN", reason="manual") is True
    assert store.get_active_plan("002648", "CN") is None


def test_full_reeval_bumps_plan_version(tmp_path):
    store = StockArchiveStore(tmp_path / "archives")
    store.save_from_baseline(_baseline(), bump_version=False)
    p2 = store.save_from_baseline(
        _baseline(trade_date="2026-07-14", stance="Buy"), bump_version=True
    )
    assert p2.plan_version == 2
    assert p2.stance == "Buy"


def test_ensure_from_baseline_does_not_overwrite(tmp_path):
    store = StockArchiveStore(tmp_path / "archives")
    store.save_from_baseline(_baseline(stance="Hold"), bump_version=False)
    again = store.ensure_from_baseline(_baseline(stance="Sell"))
    assert again.stance == "Hold"


def test_compute_delta_stop_and_move(tmp_path):
    plan = ActivePlan.from_baseline(_baseline())
    delta = compute_plan_delta(plan, current_price=89.0, price_hard_threshold_pct=5.0)
    assert delta.vs_stop == "breached"
    assert "stop_loss_breached" in delta.risk_flags
    assert delta.price_move_pct is not None and delta.price_move_pct > 10


def test_archive_prior_includes_plan_and_delta_no_cross_ticker():
    plan = ActivePlan.from_baseline(
        _baseline(), invalidation="跌破止损", watch_items=["解禁日"]
    )
    delta = compute_plan_delta(plan, current_price=101.0)
    text = build_archive_prior(plan, delta)
    assert "[档案计划" in text
    assert "原逻辑仍在" in text
    assert "失效条件" in text
    assert "[自上次以来的变更" in text
    assert "跨票" in text  # explicit note that we exclude them
    assert "Recent cross-ticker" not in text
    assert "NVDA" not in text


def test_save_delta_roundtrip(tmp_path):
    store = StockArchiveStore(tmp_path / "archives")
    plan = ActivePlan.from_baseline(_baseline())
    store.save_active_plan(plan)
    delta = compute_plan_delta(plan, current_price=101.0)
    store.save_delta(delta)
    loaded = store.get_delta("002648", "CN")
    assert loaded is not None
    assert loaded.vs_plan_version == 1
    assert loaded.current_price == 101.0
