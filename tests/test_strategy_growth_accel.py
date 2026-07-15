"""Tests for growth-accel strategy (成长加速漏斗) — pure logic, no network."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from tradingagents.strategies.growth_accel import (
    GrowthStockInfo,
    _ACCEL_EXTRA_SCORE,
    _MAX_CANDIDATES,
    _MIN_NP_ABS,
    _MIN_NP_YOY,
    _MIN_REV_ABS_LOSS,
    _MIN_REV_YOY_LOSS,
    _MIN_VOLUME_WAN,
    _STRONG_NP_YOY,
    compute_growth_signal_score,
    compute_ttm_pair,
    evaluate_growth_track,
    is_nonrecurring_gap,
    ocf_quality_score_delta,
    resolve_profit_metrics,
    run_l1_growth_filter_impl,
    run_l2_rank_impl,
    selection_rules_snapshot,
    why_selected_line,
)


def _ytd_series(pairs: list[tuple[str, float]]) -> pd.Series:
    """Build a date→YTD cumulative series for TTM helpers."""
    idx = [pd.Timestamp(d) for d, _ in pairs]
    return pd.Series([v for _, v in pairs], index=idx)


class TestTtmPair:
    def test_annual_latest(self):
        s = _ytd_series(
            [
                ("2025-12-31", 100.0),
                ("2024-12-31", 50.0),
                ("2023-12-31", 40.0),
            ]
        )
        ttm, prior, yoy = compute_ttm_pair(s)
        assert ttm == pytest.approx(100.0)
        assert prior == pytest.approx(50.0)
        assert yoy == pytest.approx(1.0)

    def test_q1_latest_uses_bridge_formula(self):
        # TTM = FY2025 + Q1'26 - Q1'25
        s = _ytd_series(
            [
                ("2026-03-31", 30.0),
                ("2025-12-31", 100.0),
                ("2025-03-31", 20.0),
                ("2024-12-31", 60.0),
                ("2024-03-31", 15.0),
            ]
        )
        ttm, prior, yoy = compute_ttm_pair(s)
        assert ttm == pytest.approx(110.0)  # 100+30-20
        assert prior == pytest.approx(65.0)  # 60+20-15
        assert yoy == pytest.approx(110 / 65 - 1)

    def test_missing_anchor_returns_none(self):
        s = _ytd_series([("2026-03-31", 30.0), ("2025-12-31", 100.0)])
        ttm, prior, yoy = compute_ttm_pair(s)
        assert ttm is None
        assert prior is None
        assert yoy is None


class TestEvaluateGrowthTrack:
    def test_profit_track_passes(self):
        ok, track, reason = evaluate_growth_track(
            np_ttm=2e8,
            np_yoy=0.6,
            rev_ttm=5e9,
            rev_yoy=0.2,
            np_prior=1.25e8,
        )
        assert ok and track == "profit"
        assert reason == ""

    def test_profit_track_rejects_low_yoy(self):
        ok, track, reason = evaluate_growth_track(
            np_ttm=2e8,
            np_yoy=0.2,
            rev_ttm=5e9,
            rev_yoy=0.2,
            np_prior=1.6e8,
        )
        assert not ok and track == "profit"
        assert "净利增速" in reason

    def test_profit_track_rejects_small_abs(self):
        ok, _, reason = evaluate_growth_track(
            np_ttm=5e6,
            np_yoy=2.0,
            rev_ttm=5e9,
            rev_yoy=0.5,
            np_prior=1e6,
        )
        assert not ok
        assert "利润规模" in reason

    def test_profit_track_turnaround_from_loss(self):
        ok, track, reason = evaluate_growth_track(
            np_ttm=1e8,
            np_yoy=None,
            rev_ttm=2e9,
            rev_yoy=0.4,
            np_prior=-5e7,
        )
        assert ok and track == "profit"
        assert reason == ""

    def test_loss_track_narrowing(self):
        ok, track, reason = evaluate_growth_track(
            np_ttm=-7e7,
            np_yoy=None,
            rev_ttm=2e9,
            rev_yoy=0.4,
            np_prior=-1.2e8,
        )
        assert ok and track == "loss"
        assert reason == ""

    def test_loss_track_rejects_small_revenue(self):
        ok, track, reason = evaluate_growth_track(
            np_ttm=-7e7,
            np_yoy=None,
            rev_ttm=5e8,
            rev_yoy=0.5,
            np_prior=-1.2e8,
        )
        assert not ok and track == "loss"
        assert "营收规模" in reason

    def test_missing_metrics_fail_closed(self):
        ok, _, reason = evaluate_growth_track(
            np_ttm=None,
            np_yoy=None,
            rev_ttm=2e9,
            rev_yoy=0.4,
            np_prior=None,
        )
        assert not ok
        assert "缺失" in reason


class TestQualityHelpers:
    def test_resolve_prefers_deduct(self):
        r = resolve_profit_metrics(
            parent_ttm=2e8,
            parent_prior=1e8,
            parent_yoy=1.0,
            deduct_ttm=1.8e8,
            deduct_prior=1e8,
            deduct_yoy=0.8,
        )
        assert r["used_deduct"] is True
        assert r["np_ttm"] == 1.8e8
        assert r["no_nonrecurring"] is False

    def test_resolve_fallback_parent(self):
        r = resolve_profit_metrics(
            parent_ttm=2e8,
            parent_prior=1e8,
            parent_yoy=0.9,
            deduct_ttm=None,
            deduct_prior=None,
            deduct_yoy=None,
        )
        assert r["used_deduct"] is False
        assert r["no_nonrecurring"] is True
        assert r["np_yoy"] == 0.9

    def test_nonrecurring_gap_hard_exclude(self):
        assert is_nonrecurring_gap(1.0, 0.5) is True
        assert is_nonrecurring_gap(0.8, 0.7) is False
        assert is_nonrecurring_gap(0.8, None) is False

    def test_ocf_penalty_and_boost(self):
        assert ocf_quality_score_delta(track="profit", np_ttm=1e8, ocf_ttm=5e7, ocf_prior=4e7) == 0
        assert ocf_quality_score_delta(track="profit", np_ttm=1e8, ocf_ttm=1e7, ocf_prior=2e7) == -1
        assert ocf_quality_score_delta(track="profit", np_ttm=1e8, ocf_ttm=-3e7, ocf_prior=-1e7) == -2
        assert ocf_quality_score_delta(track="loss", np_ttm=-1e7, ocf_ttm=-1e7, ocf_prior=-3e7) == 1
        assert ocf_quality_score_delta(track="profit", np_ttm=1e8, ocf_ttm=None, ocf_prior=None) == 0


class TestL1FilterImpl:
    def test_passes_high_growth(self):
        info = GrowthStockInfo(
            code="300502",
            np_ttm=2e8,
            np_ttm_yoy=0.8,
            np_ttm_prior=1.1e8,
            rev_ttm=5e9,
            rev_ttm_yoy=0.3,
            used_deduct=True,
        )
        out = run_l1_growth_filter_impl([info])
        assert len(out) == 1
        assert out[0].track == "profit"

    def test_excludes_missing_ttm(self):
        info = GrowthStockInfo(code="300502")
        assert run_l1_growth_filter_impl([info]) == []

    def test_excludes_nonrecurring_gap(self):
        info = GrowthStockInfo(
            code="X",
            np_ttm=2e8,
            np_ttm_yoy=0.6,
            np_ttm_prior=1.2e8,
            rev_ttm=5e9,
            rev_ttm_yoy=0.3,
            parent_np_ttm_yoy=1.2,
            deduct_np_ttm_yoy=0.6,
            used_deduct=True,
        )
        assert run_l1_growth_filter_impl([info]) == []
        assert "扣非" in info.exclude_reason


class TestL2Rank:
    def test_top_n_by_score_then_yoy(self):
        stocks = [
            GrowthStockInfo(
                code="A",
                track="profit",
                np_ttm=2e8,
                np_ttm_yoy=0.6,
                growth_theme=False,
                profit_accel=False,
            ),
            GrowthStockInfo(
                code="B",
                track="profit",
                np_ttm=3e8,
                np_ttm_yoy=1.5,
                growth_theme=True,
                profit_accel=True,
            ),
            GrowthStockInfo(
                code="C",
                track="profit",
                np_ttm=2e8,
                np_ttm_yoy=0.55,
                growth_theme=True,
                profit_accel=False,
            ),
        ]
        for s in stocks:
            s.signal_score = compute_growth_signal_score(s)
        assert stocks[1].signal_score == 3 + _ACCEL_EXTRA_SCORE + 1
        ranked = run_l2_rank_impl(stocks, max_candidates=2)
        assert [x.code for x in ranked] == ["B", "C"]

    def test_no_deduct_and_weak_ocf_demote(self):
        strong = GrowthStockInfo(
            code="S",
            track="profit",
            np_ttm_yoy=0.6,
            growth_theme=False,
            no_nonrecurring=False,
            ocf_score_delta=0,
        )
        weak = GrowthStockInfo(
            code="W",
            track="profit",
            np_ttm_yoy=0.6,
            growth_theme=False,
            no_nonrecurring=True,
            ocf_score_delta=-1,
        )
        assert compute_growth_signal_score(strong) > compute_growth_signal_score(weak)

    def test_max_candidates_constant(self):
        assert _MAX_CANDIDATES == 15


class TestRulesAndWhy:
    def test_snapshot_mentions_no_pe_cap(self):
        snap = selection_rules_snapshot()
        assert any("不设 PE/PB" in x for x in snap["l0"])
        assert any("扣非" in x for x in snap["l1"])
        assert any("OCF" in x for x in snap["l1"])
        assert snap["l2"]["top_n"] == 15

    def test_why_line_profit(self):
        info = GrowthStockInfo(
            code="X",
            track="profit",
            np_ttm_yoy=1.2,
            used_deduct=True,
            growth_theme=True,
            profit_accel=True,
        )
        line = why_selected_line(info)
        assert "扣非" in line or "净利" in line
        assert "加速" in line
        assert "题材" in line or "主题" in line

    def test_constants_match_prd(self):
        assert _MIN_VOLUME_WAN == 3000
        assert _MIN_NP_YOY == 0.5
        assert _STRONG_NP_YOY == 1.0
        assert _MIN_NP_ABS == 1e8
        assert _MIN_REV_YOY_LOSS == 0.3
        assert _MIN_REV_ABS_LOSS == 1e9
        assert _ACCEL_EXTRA_SCORE == 2
