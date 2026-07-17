"""成长加速：与价值波段对齐的超涨降权 / analyze·watch 分道。"""

from __future__ import annotations

from tradingagents.strategies.growth_accel import (
    GrowthStockInfo,
    compute_growth_signal_score,
    run_l2_rank_impl,
)
from tradingagents.strategies.value_swing import LANE_ANALYZE, LANE_WATCH


def _base(**kwargs) -> GrowthStockInfo:
    data = dict(
        code="001",
        track="profit",
        np_ttm=2e8,
        np_ttm_yoy=0.6,
        growth_theme=False,
        profit_accel=False,
        ret_5d=None,
    )
    data.update(kwargs)
    return GrowthStockInfo(**data)


def test_growth_theme_blocked_when_overextended():
    calm = _base(growth_theme=True, ret_5d=0.05)
    hot = _base(code="002", growth_theme=True, ret_5d=0.12)
    assert compute_growth_signal_score(calm) == compute_growth_signal_score(
        _base(growth_theme=False, ret_5d=0.05)
    ) + 1
    assert compute_growth_signal_score(hot) == compute_growth_signal_score(
        _base(code="002", growth_theme=False, ret_5d=0.12)
    )


def test_growth_overextend_penalty_and_lane():
    info = _base(
        np_ttm_yoy=1.2,
        growth_theme=True,
        profit_accel=True,
        ret_5d=0.16,
    )
    # 强增速3 + 加速2 + 题材0(超涨) + 超涨−2 = 3
    score = compute_growth_signal_score(info)
    assert score == 3
    assert info.overextend_delta == -2
    assert info.lane == LANE_WATCH


def test_growth_analyze_lane_when_calm():
    info = _base(np_ttm_yoy=1.2, growth_theme=True, ret_5d=0.03)
    score = compute_growth_signal_score(info)
    assert score == 3 + 1  # 强增速 + 题材
    assert info.overextend_delta == 0
    assert info.lane == LANE_ANALYZE


def test_growth_soft_overextend_minus_one():
    info = _base(np_ttm_yoy=0.6, ret_5d=0.09)
    score = compute_growth_signal_score(info)
    # 基准高增 2 − 1 超涨
    assert score == 1
    assert info.overextend_delta == -1
    assert info.lane == LANE_WATCH


def test_growth_l2_rank_sets_lane_on_candidates():
    stocks = [
        _base(code="A", np_ttm_yoy=1.2, ret_5d=0.02),
        _base(code="B", np_ttm_yoy=1.2, ret_5d=0.11),
    ]
    out = {s.code: s for s in run_l2_rank_impl(stocks, max_candidates=5)}
    assert out["A"].lane == LANE_ANALYZE
    assert out["B"].lane == LANE_WATCH
    assert out["A"].signal_score > out["B"].signal_score


def test_growth_score_floors_at_zero():
    info = _base(
        track="loss",
        np_ttm_yoy=None,
        loss_narrowed=False,
        turnaround=False,
        no_nonrecurring=True,
        ocf_score_delta=-1,
        exp_score_delta=-1,
        ret_5d=0.20,
    )
    # loss 基础1 −1无扣非 −1 OCF −1 预期 −2 超涨 → 地板 0
    assert compute_growth_signal_score(info) == 0
