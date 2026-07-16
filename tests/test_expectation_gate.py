"""一致预期质量闸门 — 纯逻辑，无网络。"""

from __future__ import annotations

import pytest

from tradingagents.strategies.expectation_gate import (
    MIN_ANALYSTS,
    ConsensusSnapshot,
    build_consensus_snapshot,
    score_growth_expectation,
    score_value_expectation,
)


def _snap(
    *,
    analysts: int = 5,
    fy1: float | None = 1.0,
    fy2: float | None = 1.2,
    price: float = 20.0,
) -> ConsensusSnapshot:
    return build_consensus_snapshot(
        records=[
            {"year": "2026", "eps": fy1, "analysts": analysts},
            {"year": "2027", "eps": fy2, "analysts": analysts},
        ]
        if fy1 is not None
        else [],
        org_count=analysts,
        price=price,
    )


class TestBuildConsensus:
    def test_usable_with_coverage_and_positive_eps(self):
        snap = _snap(analysts=5, fy1=1.0, fy2=1.25, price=20.0)
        assert snap.usable is True
        assert snap.low_coverage is False
        assert snap.analysts == 5
        assert snap.fwd_pe == pytest.approx(20.0)
        assert snap.implied_cagr == pytest.approx(0.25)

    def test_low_coverage_not_usable(self):
        snap = _snap(analysts=2, fy1=1.0, fy2=1.2)
        assert snap.low_coverage is True
        assert snap.usable is False
        assert snap.analysts == 2

    def test_empty_records(self):
        snap = build_consensus_snapshot(records=[], org_count=0, price=10.0)
        assert snap.usable is False
        assert snap.fy1_eps is None

    def test_negative_eps_not_usable(self):
        snap = _snap(fy1=-0.5, fy2=0.2)
        assert snap.usable is False

    def test_min_analysts_constant(self):
        assert MIN_ANALYSTS == 3


class TestValueGate:
    def test_fwd_cheaper_than_ttm_bonus(self):
        # pe_ttm=30, fwd_pe=20 → 明显更便宜
        snap = _snap(fy1=1.0, price=20.0)
        r = score_value_expectation(pe_ttm=30.0, snap=snap)
        assert r.score_delta == 1
        assert r.hit is True
        assert "便宜" in r.label or "远期" in r.label

    def test_fwd_implies_earnings_decline_penalty(self):
        # pe_ttm=10, fwd_pe=20 → 一致预期暗示盈利下滑
        snap = _snap(fy1=1.0, price=20.0)
        r = score_value_expectation(pe_ttm=10.0, snap=snap)
        assert r.score_delta == -1
        assert r.hit is False
        assert "下滑" in r.label or "偏弱" in r.label or "恶化" in r.label

    def test_neutral_band(self):
        snap = _snap(fy1=1.0, price=20.0)  # fwd_pe=20
        r = score_value_expectation(pe_ttm=20.0, snap=snap)
        assert r.score_delta == 0
        assert r.hit is False

    def test_low_coverage_noop(self):
        snap = _snap(analysts=1, fy1=1.0, price=10.0)
        r = score_value_expectation(pe_ttm=30.0, snap=snap)
        assert r.score_delta == 0
        assert r.low_coverage is True

    def test_missing_pe_ttm_noop(self):
        snap = _snap(fy1=1.0, price=10.0)
        r = score_value_expectation(pe_ttm=0.0, snap=snap)
        assert r.score_delta == 0


class TestGrowthGate:
    def test_actual_beats_consensus_bonus(self):
        # implied CAGR 20%, actual 50% → gap 30pct
        snap = _snap(fy1=1.0, fy2=1.2, price=20.0)
        r = score_growth_expectation(actual_yoy=0.50, snap=snap, track="profit")
        assert r.score_delta == 1
        assert r.hit is True
        assert "预期差" in r.label or "高于" in r.label

    def test_overhang_penalty(self):
        # implied 40%, actual 15% → 预期透支
        snap = _snap(fy1=1.0, fy2=1.4, price=20.0)
        r = score_growth_expectation(actual_yoy=0.15, snap=snap, track="profit")
        assert r.score_delta == -1
        assert r.hit is False
        assert "透支" in r.label or "偏高" in r.label or "放缓" in r.label

    def test_loss_track_skipped(self):
        snap = _snap(fy1=1.0, fy2=1.5, price=20.0)
        r = score_growth_expectation(actual_yoy=0.80, snap=snap, track="loss")
        assert r.score_delta == 0

    def test_low_coverage_noop(self):
        snap = _snap(analysts=2, fy1=1.0, fy2=2.0)
        r = score_growth_expectation(actual_yoy=1.0, snap=snap, track="profit")
        assert r.score_delta == 0
        assert r.low_coverage is True

    def test_missing_implied_cagr_noop(self):
        snap = build_consensus_snapshot(
            records=[{"year": "2026", "eps": 1.0, "analysts": 5}],
            org_count=5,
            price=20.0,
        )
        assert snap.usable is True
        assert snap.implied_cagr is None
        r = score_growth_expectation(actual_yoy=0.8, snap=snap, track="profit")
        assert r.score_delta == 0
