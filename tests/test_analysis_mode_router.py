"""分析模式路由：硬闸优先全量，窄口径才伪增量；不确定默认全量。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from tradingagents.analysis.calibration import CalibrationStore
from tradingagents.analysis.mode_router import (
    MODE_FULL,
    MODE_INCREMENTAL,
    AnalysisRouteDecision,
    resolve_analysis_mode,
)
from tradingagents.archive.models import ActivePlan
from tradingagents.archive.store import StockArchiveStore
from tradingagents.watchlist.models import Alert, Baseline, WatchItem
from tradingagents.watchlist.store import WatchlistStore


def _baseline(**kwargs) -> Baseline:
    data = dict(
        ticker="002648",
        trade_date="2026-07-10",
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


def _archives(tmp_path: Path) -> StockArchiveStore:
    return StockArchiveStore(tmp_path / "archives")


def _resolve(tmp_path: Path, **kwargs):
    kwargs.setdefault("archive_store", _archives(tmp_path))
    return resolve_analysis_mode(**kwargs)


def test_force_full_reeval_wins(tmp_path):
    store = CalibrationStore(tmp_path / "anchors.json")
    store.save(_baseline())
    decision = _resolve(
        tmp_path,
        ticker="002648",
        trade_date="2026-07-14",
        force_full_reeval=True,
        calibration_store=store,
        current_price=100.0,
        as_of=date(2026, 7, 14),
    )
    assert decision.mode == MODE_FULL
    assert decision.extra_past_context == ""
    assert "force" in decision.reason


def test_explicit_full_mode(tmp_path):
    store = CalibrationStore(tmp_path / "anchors.json")
    store.save(_baseline())
    decision = _resolve(
        tmp_path,
        ticker="002648",
        trade_date="2026-07-14",
        analysis_mode=MODE_FULL,
        calibration_store=store,
        current_price=100.0,
        as_of=date(2026, 7, 14),
    )
    assert decision.mode == MODE_FULL
    assert decision.extra_past_context == ""


def test_no_anchor_defaults_to_full(tmp_path):
    store = CalibrationStore(tmp_path / "anchors.json")
    decision = _resolve(
        tmp_path,
        ticker="002648",
        trade_date="2026-07-14",
        calibration_store=store,
        current_price=100.0,
        as_of=date(2026, 7, 14),
        seed_from_history=False,
    )
    assert decision.mode == MODE_FULL
    assert "no_anchor" in decision.reason


def test_stale_calibration_forces_full(tmp_path):
    store = CalibrationStore(tmp_path / "anchors.json")
    # Use older calibration: 2026-07-06 (Mon) → 2026-07-14 (Tue) = 6 trading days.
    store.save(_baseline(trade_date="2026-07-06"))
    decision = _resolve(
        tmp_path,
        ticker="002648",
        trade_date="2026-07-14",
        calibration_store=store,
        current_price=100.0,
        as_of=date(2026, 7, 14),
        max_calibration_age_trading_days=3,
    )
    assert decision.mode == MODE_FULL
    assert "stale_calibration" in decision.reason


def test_stop_loss_breach_forces_full(tmp_path):
    store = CalibrationStore(tmp_path / "anchors.json")
    store.save(_baseline(trade_date="2026-07-13", stop_loss=90.0))
    decision = _resolve(
        tmp_path,
        ticker="002648",
        trade_date="2026-07-14",
        calibration_store=store,
        current_price=89.0,
        as_of=date(2026, 7, 14),
    )
    assert decision.mode == MODE_FULL
    assert "stop_loss" in decision.reason


def test_large_price_move_forces_full(tmp_path):
    store = CalibrationStore(tmp_path / "anchors.json")
    store.save(_baseline(trade_date="2026-07-13", baseline_price=100.0))
    decision = _resolve(
        tmp_path,
        ticker="002648",
        trade_date="2026-07-14",
        calibration_store=store,
        current_price=106.0,
        as_of=date(2026, 7, 14),
        price_threshold_pct=5.0,
    )
    assert decision.mode == MODE_FULL
    assert "price_move" in decision.reason


def test_high_priority_watch_alert_forces_full(tmp_path):
    cal = CalibrationStore(tmp_path / "anchors.json")
    cal.save(_baseline(trade_date="2026-07-13"))
    watch = WatchlistStore(tmp_path / "watch.json")
    watch.add(
        WatchItem(
            baseline=_baseline(trade_date="2026-07-13"),
            enabled=True,
            alerts=[
                Alert(
                    kind="stance",
                    title="立场变化",
                    detail="Hold→Sell",
                    observed_at="2026-07-14T10:00:00",
                )
            ],
        )
    )
    decision = _resolve(
        tmp_path,
        ticker="002648",
        trade_date="2026-07-14",
        calibration_store=cal,
        watch_store=watch,
        current_price=100.0,
        as_of=date(2026, 7, 14),
    )
    assert decision.mode == MODE_FULL
    assert "watch_alert" in decision.reason


def test_narrow_path_allows_pseudo_incremental(tmp_path):
    store = CalibrationStore(tmp_path / "anchors.json")
    store.save(_baseline(trade_date="2026-07-13"))
    decision = _resolve(
        tmp_path,
        ticker="002648",
        trade_date="2026-07-14",
        calibration_store=store,
        current_price=101.0,
        as_of=date(2026, 7, 14),
        price_threshold_pct=5.0,
        max_calibration_age_trading_days=3,
        seed_from_history=False,
    )
    assert decision.mode == MODE_INCREMENTAL
    assert "原逻辑仍在" in decision.extra_past_context
    assert "[档案计划" in decision.extra_past_context
    assert "[自上次以来的变更" in decision.extra_past_context
    assert decision.updates_calibration is False
    # Lazy migrate calibration → archive
    assert _archives(tmp_path).get_active_plan("002648", "CN") is not None
    assert _archives(tmp_path).get_delta("002648", "CN") is not None


def test_archive_plan_preferred_over_calibration(tmp_path):
    cal = CalibrationStore(tmp_path / "anchors.json")
    cal.save(_baseline(trade_date="2026-07-13", stance="Hold", thesis_summary="校准旧逻辑"))
    arch = _archives(tmp_path)
    arch.save_active_plan(
        ActivePlan.from_baseline(
            _baseline(
                trade_date="2026-07-13",
                stance="Buy",
                thesis_summary="档案新逻辑",
            ),
            invalidation="跌破止损",
            plan_version=3,
        )
    )
    decision = _resolve(
        tmp_path,
        ticker="002648",
        trade_date="2026-07-14",
        calibration_store=cal,
        archive_store=arch,
        current_price=101.0,
        as_of=date(2026, 7, 14),
        seed_from_history=False,
    )
    assert decision.mode == MODE_INCREMENTAL
    assert decision.anchor is not None
    assert decision.anchor.stance == "Buy"
    assert "档案新逻辑" in decision.extra_past_context
    assert "失效条件：跌破止损" in decision.extra_past_context
    assert "校准旧逻辑" not in decision.extra_past_context


def test_scan_source_skips_deep_analysis_on_narrow_path(tmp_path):
    from tradingagents.analysis.mode_router import MODE_SKIP, SOURCE_SCAN

    store = CalibrationStore(tmp_path / "anchors.json")
    store.save(_baseline(trade_date="2026-07-13"))
    decision = _resolve(
        tmp_path,
        ticker="002648",
        trade_date="2026-07-14",
        source=SOURCE_SCAN,
        calibration_store=store,
        current_price=101.0,
        as_of=date(2026, 7, 14),
        seed_from_history=False,
    )
    assert decision.mode == MODE_SKIP
    assert decision.skips_deep_analysis is True
    assert "scan_skip" in decision.reason


def test_seed_from_history_when_no_anchor(tmp_path, monkeypatch):
    store = CalibrationStore(tmp_path / "anchors.json")

    def fake_seed(ticker, *, market="CN", store=None):
        b = _baseline(trade_date="2026-07-13")
        (store or CalibrationStore(tmp_path / "anchors.json")).save(b)
        return b

    monkeypatch.setattr(
        "tradingagents.analysis.persist.seed_calibration_from_history",
        fake_seed,
    )
    decision = _resolve(
        tmp_path,
        ticker="002648",
        trade_date="2026-07-14",
        calibration_store=store,
        current_price=100.0,
        as_of=date(2026, 7, 14),
        seed_from_history=True,
    )
    assert decision.mode == MODE_INCREMENTAL
    assert store.get("002648", "CN") is not None


def test_full_decision_marks_calibration_update():
    d = AnalysisRouteDecision(mode=MODE_FULL, reason="force")
    assert d.updates_calibration is True
    d2 = AnalysisRouteDecision(mode=MODE_INCREMENTAL, reason="ok", extra_past_context="x")
    assert d2.updates_calibration is False


def test_missing_price_defaults_to_full(tmp_path):
    """取价失败时保守全量，避免漏掉该翻盘。"""
    store = CalibrationStore(tmp_path / "anchors.json")
    store.save(_baseline(trade_date="2026-07-13"))
    decision = _resolve(
        tmp_path,
        ticker="002648",
        trade_date="2026-07-14",
        calibration_store=store,
        current_price=None,
        as_of=date(2026, 7, 14),
        fetch_price=False,
    )
    assert decision.mode == MODE_FULL
    assert "no_price" in decision.reason


def test_resume_skips_routing_context(tmp_path):
    store = CalibrationStore(tmp_path / "anchors.json")
    store.save(_baseline())
    decision = _resolve(
        tmp_path,
        ticker="002648",
        trade_date="2026-07-14",
        fresh=False,
        calibration_store=store,
        current_price=100.0,
        as_of=date(2026, 7, 14),
    )
    assert decision.mode == MODE_FULL
    assert decision.extra_past_context == ""
    assert "resume" in decision.reason
    assert decision.updates_calibration is False


def test_watchlist_baseline_used_when_calibration_missing(tmp_path):
    cal = CalibrationStore(tmp_path / "anchors.json")
    watch = WatchlistStore(tmp_path / "watch.json")
    watch.add(WatchItem(baseline=_baseline(trade_date="2026-07-13"), enabled=True))
    decision = _resolve(
        tmp_path,
        ticker="002648",
        trade_date="2026-07-14",
        calibration_store=cal,
        watch_store=watch,
        current_price=100.0,
        as_of=date(2026, 7, 14),
        seed_from_history=False,
    )
    assert decision.mode == MODE_INCREMENTAL
    assert "[档案计划" in decision.extra_past_context


def test_calibration_store_roundtrip(tmp_path):
    store = CalibrationStore(tmp_path / "anchors.json")
    assert store.get("002648", "CN") is None
    store.save(_baseline())
    got = store.get("002648", "CN")
    assert got is not None
    assert got.ticker == "002648"
    assert got.stop_loss == 90.0
    store.delete("002648", "CN")
    assert store.get("002648", "CN") is None


def test_persist_full_reeval_writes_archive(tmp_path):
    from tradingagents.analysis.persist import save_calibration_from_state

    cal = CalibrationStore(tmp_path / "anchors.json")
    arch = _archives(tmp_path)
    state = {
        "final_trade_decision": "Rating: Buy\n入场价：10.5\n止损：9.0\n逻辑仍成立",
        "trader_investment_decision": "建议仓位: 15%\nEntry Price: 10.5\nStop Loss: 9.0",
        "investment_plan": "继续看好",
    }
    baseline = save_calibration_from_state(
        state,
        ticker="002648",
        trade_date="2026-07-14",
        market="CN",
        price=10.8,
        log_path="/tmp/y.json",
        store=cal,
        archive_store=arch,
    )
    assert baseline is not None
    plan = arch.get_active_plan("002648", "CN")
    assert plan is not None
    assert plan.plan_version == 1
    again = save_calibration_from_state(
        state,
        ticker="002648",
        trade_date="2026-07-15",
        market="CN",
        price=11.0,
        log_path="/tmp/z.json",
        store=cal,
        archive_store=arch,
    )
    assert again is not None
    plan2 = arch.get_active_plan("002648", "CN")
    assert plan2 is not None
    assert plan2.plan_version == 2


def test_job_roundtrip_preserves_mode_fields():
    from web.analysis_queue import AnalysisJob

    job = AnalysisJob(
        ticker="300253",
        trade_date="2026-07-15",
        market="CN",
        force_full_reeval=True,
        analysis_mode="full_reeval",
        source="scan",
    )
    back = AnalysisJob.from_mapping(job.to_dict())
    assert back.force_full_reeval is True
    assert back.analysis_mode == "full_reeval"
    assert back.source == "scan"
    req = job.to_start_request()
    assert req["force_full_reeval"] is True
    assert req["analysis_mode"] == "full_reeval"
    assert req["source"] == "scan"


def test_partition_scan_jobs_skips_narrow(monkeypatch):
    from tradingagents.analysis.mode_router import (
        MODE_FULL,
        MODE_SKIP,
        AnalysisRouteDecision,
    )
    from web.analysis_queue import AnalysisJob, partition_scan_jobs_for_enqueue

    def fake_resolve(**kwargs):
        ticker = kwargs["ticker"]
        if ticker == "002648":
            return AnalysisRouteDecision(mode=MODE_SKIP, reason="narrow_ok_scan_skip")
        return AnalysisRouteDecision(mode=MODE_FULL, reason="no_anchor")

    monkeypatch.setattr(
        "tradingagents.analysis.mode_router.resolve_analysis_mode",
        fake_resolve,
    )
    jobs = [
        AnalysisJob(ticker="002648", trade_date="2026-07-14", market="CN", source="scan"),
        AnalysisJob(ticker="300253", trade_date="2026-07-14", market="CN", source="scan"),
    ]
    keep, skipped = partition_scan_jobs_for_enqueue(jobs, as_of="2026-07-14")
    assert [j.ticker for j in keep] == ["300253"]
    assert skipped == [("002648", "narrow_ok_scan_skip")]
