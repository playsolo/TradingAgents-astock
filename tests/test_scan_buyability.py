"""选股可买入对齐：超涨降权、动量题材门控、analyze/watch 分道。"""

from __future__ import annotations

from tradingagents.strategies.value_swing import (
    LANE_ANALYZE,
    LANE_WATCH,
    StockInfo,
    assign_scan_lane,
    momentum_catalyst_hit,
    overextend_score_delta,
    run_l2_filter_impl,
)


def test_overextend_delta_thresholds():
    assert overextend_score_delta(None) == 0
    assert overextend_score_delta(0.07) == 0
    assert overextend_score_delta(0.08) == -1
    assert overextend_score_delta(0.14) == -1
    assert overextend_score_delta(0.15) == -2


def test_momentum_catalyst_blocked_when_overextended():
    assert momentum_catalyst_hit(
        StockInfo(code="1", hot_topic_match=True, ret_5d=0.12)
    ) is False
    assert momentum_catalyst_hit(
        StockInfo(code="1", concept_active=True, ret_5d=0.09)
    ) is False
    assert momentum_catalyst_hit(
        StockInfo(code="1", hot_topic_match=True, ret_5d=0.05)
    ) is True


def test_overextended_hot_theme_does_not_score():
    calm = StockInfo(code="001", hot_topic_match=True, concept_active=True, ret_5d=0.02)
    hot = StockInfo(code="002", hot_topic_match=True, concept_active=True, ret_5d=0.12)
    scores = {s.code: s.signal_score for s in run_l2_filter_impl([calm, hot])}
    assert scores["001"] == 2
    # 题材不计分，且超涨 −1 → 地板 0
    assert scores["002"] == 0


def test_overextend_penalty_reduces_quality_name():
    info = StockInfo(
        code="001",
        above_ma20=True,
        near_ma250=True,
        northbound_net_3d=1.0,
        ret_5d=0.16,
    )
    # 3 质量分 −2 超涨 = 1
    assert run_l2_filter_impl([info])[0].signal_score == 1
    assert run_l2_filter_impl([info])[0].overextend_delta == -2


def test_assign_scan_lane_watch_when_soft_overextended():
    assert assign_scan_lane(StockInfo(code="1", ret_5d=0.08)) == LANE_WATCH
    assert assign_scan_lane(StockInfo(code="1", ret_5d=0.07)) == LANE_ANALYZE
    assert assign_scan_lane(StockInfo(code="1", ret_5d=None)) == LANE_ANALYZE


def test_l2_sets_lane_on_candidates():
    stocks = [
        StockInfo(code="001", above_ma20=True, ret_5d=0.03),
        StockInfo(code="002", above_ma20=True, ret_5d=0.10),
    ]
    out = {s.code: s for s in run_l2_filter_impl(stocks)}
    assert out["001"].lane == LANE_ANALYZE
    assert out["002"].lane == LANE_WATCH
