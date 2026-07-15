"""Tests for value swing strategy (三阶漏斗).

Core stateless logic — no network calls.
"""

from __future__ import annotations

import pytest

from tradingagents.strategies.value_swing import (
    SCAN_RECOMMENDATION_BUY,
    SCAN_RECOMMENDATION_STRONG,
    SCAN_RECOMMENDATION_WATCH,
    ScanResult,
    StockInfo,
    _is_stock_excluded_by_prefix,
    _MAX_CANDIDATES,
    _MIN_AMPLITUDE_20D,
    _MIN_VOLUME_WAN,
    _PB_MAX,
    _PE_MIN,
    _PE_TTM_MAX,
    _tencent_volume_wan,
    build_scan_summary_row,
    map_scan_recommendation,
    run_l1a_filter,
    run_l1b_filter,
    run_l2_filter_impl,
)


# ── 数据结构 ────────────────────────────────────────────────────────────────


class TestDataStructures:
    def test_stock_info_defaults(self):
        i = StockInfo(code="000001", name="平安银行")
        assert i.code == "000001"
        assert i.signal_score == 0
        assert i.exclude_reason == ""

    def test_scan_result_defaults(self):
        r = ScanResult(scan_date="2026-07-15")
        assert r.l0_passed == 0 and r.l1a_passed == 0
        assert r.l1b_passed == 0 and r.l2_passed == 0
        assert r.candidates == []

    def test_constants_sane(self):
        assert 3 <= _PE_MIN <= 10
        assert _PE_TTM_MAX >= 20
        assert _PB_MAX > 0
        assert _MIN_VOLUME_WAN >= 1000


# ── 工具函数 ────────────────────────────────────────────────────────────────


class TestHelpers:
    def test_volume_wan(self):
        assert _tencent_volume_wan(10, 2, 100) == pytest.approx(20000)
        assert _tencent_volume_wan(0, 2, 100) == 0
        assert _tencent_volume_wan(-1, 2, 100) == 0

    def test_is_stock_excluded_by_prefix(self):
        assert not _is_stock_excluded_by_prefix("688999")
        assert _is_stock_excluded_by_prefix("899999")
        assert not _is_stock_excluded_by_prefix("600519")

    def test_build_summary_row(self):
        info = StockInfo(code="000001", name="平安", price=12, pe_ttm=6, pb=0.6,
                         signal_score=5, above_ma20=True)
        row = build_scan_summary_row(info, "强烈推荐")
        assert row["code"] == "000001"
        assert row["recommendation"] == "强烈推荐"
        assert row["pe_ttm"] == 6.0
        assert row["above_ma20"] is True

    def test_build_summary_row_nulls(self):
        info = StockInfo(code="000001", name="测试", signal_score=0)
        row = build_scan_summary_row(info, "")
        assert row["debt_ratio"] is None
        assert row["revenue_growth"] is None


# ── 推荐映射 ────────────────────────────────────────────────────────────────


class TestRecommendationMapping:
    def test_strong_buy(self):
        assert map_scan_recommendation("Buy", 5) == SCAN_RECOMMENDATION_STRONG
        assert map_scan_recommendation("Buy", 4) == SCAN_RECOMMENDATION_STRONG

    def test_buy(self):
        assert map_scan_recommendation("Buy", 2) == SCAN_RECOMMENDATION_BUY
        assert map_scan_recommendation("Overweight", 3) == SCAN_RECOMMENDATION_BUY

    def test_watch(self):
        assert map_scan_recommendation("Hold", 4) == SCAN_RECOMMENDATION_WATCH
        assert map_scan_recommendation("Hold", 3) == SCAN_RECOMMENDATION_WATCH

    def test_none(self):
        assert map_scan_recommendation("Hold", 0) == ""
        assert map_scan_recommendation("Sell", 5) == ""
        assert map_scan_recommendation("Underweight", 2) == ""

    def test_case(self):
        assert map_scan_recommendation("buy", 5) == SCAN_RECOMMENDATION_STRONG
        assert map_scan_recommendation("OVERWEIGHT", 2) == SCAN_RECOMMENDATION_BUY


# ── L1a: 快速 PE/PB 判定 ────────────────────────────────────────────────────


class TestL1aFilter:
    def test_empty(self):
        assert run_l1a_filter([]) == []

    def test_passes(self):
        info = StockInfo(code="000001", pe_ttm=10, pb=1)
        assert len(run_l1a_filter([info])) == 1

    def test_high_pe_rejected(self):
        info = StockInfo(code="000001", pe_ttm=50, pb=1)
        assert len(run_l1a_filter([info])) == 0

    def test_high_pb_rejected(self):
        info = StockInfo(code="000001", pe_ttm=10, pb=5)
        assert len(run_l1a_filter([info])) == 0

    def test_low_pe_rejected(self):
        info = StockInfo(code="000001", pe_ttm=1, pb=1)
        assert len(run_l1a_filter([info])) == 0

    def test_edge(self):
        info = StockInfo(code="000001", pe_ttm=_PE_TTM_MAX, pb=_PB_MAX)
        assert len(run_l1a_filter([info])) == 1

    def test_multiple(self):
        stocks = [
            StockInfo(code="000001", pe_ttm=8, pb=0.6),
            StockInfo(code="000002", pe_ttm=50, pb=1),
            StockInfo(code="000003", pe_ttm=10, pb=5),
            StockInfo(code="000004", pe_ttm=2, pb=1),
        ]
        assert len(run_l1a_filter(stocks)) == 1


# ── L1b: 财务验证 ────────────────────────────────────────────────────────────


class TestL1bFilter:
    def test_empty(self):
        assert run_l1b_filter([]) == []

    def test_field_none_does_not_exclude(self):
        info = StockInfo(code="000001", pe_ttm=10, pb=1,
                         revenue_growth=0.05, debt_ratio=0.4, amplitude_20d=0.04)
        result = run_l1b_filter([info])
        assert len(result) == 1

    def test_high_debt_excluded(self):
        info = StockInfo(code="000001", pe_ttm=10, pb=1,
                         revenue_growth=0.05, debt_ratio=0.85, amplitude_20d=0.04)
        result = run_l1b_filter([info])
        assert len(result) == 0

    def test_negative_growth_excluded(self):
        info = StockInfo(code="000001", pe_ttm=10, pb=1,
                         revenue_growth=-0.05, debt_ratio=0.4, amplitude_20d=0.04)
        result = run_l1b_filter([info])
        assert len(result) == 0

    def test_low_amplitude_excluded(self):
        info = StockInfo(code="000001", pe_ttm=10, pb=1,
                         revenue_growth=0.05, debt_ratio=0.4, amplitude_20d=0.005)
        result = run_l1b_filter([info])
        assert len(result) == 0

    def test_star_debt_stricter(self):
        from tradingagents.strategies.value_swing import _MAX_DEBT_RATIO_STAR
        assert _MAX_DEBT_RATIO_STAR < 0.65


# ── L2 评分 ──────────────────────────────────────────────────────────────────


class TestL2Filter:
    def test_empty(self):
        assert run_l2_filter_impl([]) == []

    def test_zero_score(self):
        info = StockInfo(code="000001", name="测试")
        r = run_l2_filter_impl([info])
        assert r[0].signal_score == 0

    def test_all_signals(self):
        info = StockInfo(
            code="000001", name="测试",
            northbound_net_3d=2, fund_flow_main_3d=5000,
            dragon_tiger_inst_net=1000, above_ma20=True, near_ma250=True,
        )
        r = run_l2_filter_impl([info])
        assert r[0].signal_score == 5

    def test_small_dragon_tiger_ignored(self):
        info = StockInfo(code="000001", dragon_tiger_inst_net=50)
        r = run_l2_filter_impl([info])
        assert r[0].signal_score == 0

    def test_negative_fund_flow(self):
        info = StockInfo(code="000001", northbound_net_3d=-1)
        r = run_l2_filter_impl([info])
        assert r[0].signal_score == 0

    def test_candidate_limit(self):
        stocks = [StockInfo(code=f"{i:06d}", northbound_net_3d=float(i)) for i in range(1, 21)]
        r = run_l2_filter_impl(stocks, max_candidates=5)
        assert len(r) <= 5

    def test_sort_desc(self):
        stocks = [
            StockInfo(code="001", northbound_net_3d=5, above_ma20=True),
            StockInfo(code="002", northbound_net_3d=3),
            StockInfo(code="003"),
        ]
        r = run_l2_filter_impl(stocks)
        scores = [s.signal_score for s in r]
        assert scores == sorted(scores, reverse=True)

    def test_ma_points(self):
        info = StockInfo(code="001", above_ma20=True, near_ma250=True)
        assert run_l2_filter_impl([info])[0].signal_score == 2
        info2 = StockInfo(code="002", near_ma250=True)
        assert run_l2_filter_impl([info2])[0].signal_score == 1


# ── 端到端（纯逻辑）─────────────────────────────────────────────────────────


class TestEndToEnd:
    def test_l1a_then_l1b_then_l2_no_crash(self):
        stocks = [
            StockInfo(code=f"{i:06d}", pe_ttm=10, pb=1,
                      revenue_growth=0.03, debt_ratio=0.4, amplitude_20d=0.04)
            for i in range(1, 6)
        ]
        l1a = run_l1a_filter(stocks)
        assert len(l1a) == 5
        l1b = run_l1b_filter(l1a)
        assert len(l1b) == 5
        l2 = run_l2_filter_impl(l1b)
        assert len(l2) <= 5
