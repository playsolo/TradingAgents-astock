"""Tests for the value swing strategy (三阶漏斗筛选).

Core stateless logic tests — no network calls.
"""

from __future__ import annotations

from datetime import datetime

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
    _PE_HIST_PCT_THRESHOLD,
    _PEG_MAX,
    _tencent_volume_wan,
    build_scan_summary_row,
    map_scan_recommendation,
    run_l1_filter_impl,
    run_l2_filter_impl,
)


# ── 数据结构和配置测试 ─────────────────────────────────────────────────────


class TestDataStructures:
    def test_stock_info_defaults(self):
        info = StockInfo(code="000001", name="平安银行")
        assert info.code == "000001"
        assert info.name == "平安银行"
        assert info.price == 0.0
        assert info.pe_ttm == 0.0
        assert info.signal_score == 0
        assert info.exclude_reason == ""

    def test_stock_info_full(self):
        info = StockInfo(
            code="600519",
            name="贵州茅台",
            price=1500.0,
            volume_wan=500000.0,
            pe_ttm=25.0,
            pb=8.0,
            pe_hist_pct=0.50,
            peg=1.2,
            signal_score=5,
        )
        assert info.pe_ttm == 25.0
        assert info.pb == 8.0
        assert info.peg == 1.2
        assert info.signal_score == 5

    def test_scan_result_defaults(self):
        result = ScanResult(scan_date="2026-07-15")
        assert result.scan_date == "2026-07-15"
        assert result.total_stocks == 0
        assert result.l0_passed == 0
        assert result.l1_passed == 0
        assert result.l2_passed == 0
        assert result.candidates == []
        assert result.duration_seconds == 0.0

    def test_config_constants(self):
        assert _MIN_VOLUME_WAN > 0
        assert _PB_MAX > 0
        assert _PE_HIST_PCT_THRESHOLD > 0
        assert _PEG_MAX > 0
        assert _MIN_AMPLITUDE_20D > 0
        assert _MAX_CANDIDATES > 0


# ── 工具函数测试 ────────────────────────────────────────────────────────────


class TestHelpers:
    def test_tencent_volume_wan_normal(self):
        vol = _tencent_volume_wan(price=10.0, turnover_pct=2.0, mcap_yi=100.0)
        # 100亿 * 2% = 20000万
        assert vol == pytest.approx(20000.0)

    def test_tencent_volume_wan_zero_price(self):
        vol = _tencent_volume_wan(price=0, turnover_pct=2.0, mcap_yi=100.0)
        assert vol == 0.0

    def test_tencent_volume_wan_low_turnover(self):
        vol = _tencent_volume_wan(price=50.0, turnover_pct=0.1, mcap_yi=500.0)
        # 500亿 * 0.1% = 5000万
        assert vol == pytest.approx(5000.0)

    def test_build_scan_summary_row(self):
        info = StockInfo(
            code="000001",
            name="平安银行",
            price=12.0,
            pe_ttm=6.0,
            pb=0.6,
            peg=0.8,
            debt_ratio=0.45,
            revenue_growth=0.05,
            northbound_net_3d=2.5,
            fund_flow_main_3d=3000.0,
            dragon_tiger_inst_net=500.0,
            above_ma20=True,
            near_ma250=True,
            signal_score=6,
        )
        row = build_scan_summary_row(info, "强烈推荐")
        assert row["code"] == "000001"
        assert row["name"] == "平安银行"
        assert row["recommendation"] == "强烈推荐"
        assert row["pe_ttm"] == 6.0
        assert row["pb"] == 0.6
        assert row["peg"] == 0.8
        assert row["debt_ratio"] == 45.0
        assert row["revenue_growth"] == 5.0
        assert row["above_ma20"] is True
        assert row["near_ma250"] is True

    def test_build_scan_summary_row_none_values(self):
        info = StockInfo(code="000001", name="平安银行", signal_score=0)
        row = build_scan_summary_row(info, "")
        assert row["peg"] is None
        assert row["debt_ratio"] is None
        assert row["revenue_growth"] is None

    def test_build_scan_summary_row_zero_division(self):
        info = StockInfo(code="000001", name="平安银行", signal_score=2)
        row = build_scan_summary_row(info, "推荐")
        assert row["code"] == "000001"


# ── 推荐映射测试 ──────────────────────────────────────────────────────────


class TestRecommendationMapping:
    def test_strong_buy(self):
        assert map_scan_recommendation("Buy", 5) == SCAN_RECOMMENDATION_STRONG
        assert map_scan_recommendation("Buy", 4) == SCAN_RECOMMENDATION_STRONG

    def test_buy_threshold(self):
        assert map_scan_recommendation("Buy", 3) == SCAN_RECOMMENDATION_BUY
        assert map_scan_recommendation("Buy", 2) == SCAN_RECOMMENDATION_BUY
        assert map_scan_recommendation("Overweight", 3) == SCAN_RECOMMENDATION_BUY
        assert map_scan_recommendation("Overweight", 2) == SCAN_RECOMMENDATION_BUY

    def test_watch(self):
        assert map_scan_recommendation("Hold", 4) == SCAN_RECOMMENDATION_WATCH
        assert map_scan_recommendation("Hold", 3) == SCAN_RECOMMENDATION_WATCH

    def test_no_recommendation(self):
        assert map_scan_recommendation("Hold", 0) == ""
        assert map_scan_recommendation("Hold", 2) == ""
        assert map_scan_recommendation("Sell", 5) == ""
        assert map_scan_recommendation("Underweight", 3) == ""

    def test_case_insensitive(self):
        assert map_scan_recommendation("buy", 5) == SCAN_RECOMMENDATION_STRONG
        assert map_scan_recommendation("OVERWEIGHT", 2) == SCAN_RECOMMENDATION_BUY
        assert map_scan_recommendation("hold", 4) == SCAN_RECOMMENDATION_WATCH

    def test_edge_signal_score_zero(self):
        assert map_scan_recommendation("Buy", 0) == ""
        assert map_scan_recommendation("Overweight", 0) == ""


# ── L0 子函数测试 ──────────────────────────────────────────────────────────


class TestL0Helpers:
    def test_tencent_volume_wan_edge_cases(self):
        vol = _tencent_volume_wan(price=100.0, turnover_pct=50.0, mcap_yi=10.0)
        assert vol == pytest.approx(50000.0)

        vol = _tencent_volume_wan(price=5.0, turnover_pct=1.0, mcap_yi=3.0)
        assert vol == pytest.approx(300.0)

        vol = _tencent_volume_wan(price=-1, turnover_pct=2.0, mcap_yi=100.0)
        assert vol == 0.0

    def test_tencent_volume_wan_very_low_volume(self):
        vol = _tencent_volume_wan(price=10.0, turnover_pct=0.05, mcap_yi=50.0)
        assert 0 < vol < _MIN_VOLUME_WAN

    def test_l0_include_star_market(self):
        """科创板 (688xxx) 应被纳入，不排除。"""
        assert not _is_stock_excluded_by_prefix("688999")

    def test_l0_exclude_bj_market(self):
        """北交所 (8xxx) 应被排除。"""
        assert _is_stock_excluded_by_prefix("899999")


# ── L1 筛选逻辑测试（不依赖网络） ─────────────────────────────────────────


class TestL1FilterImpl:
    def test_empty_input(self):
        assert run_l1_filter_impl([]) == []

    def test_single_valid_candidate(self):
        """一只 PB/PE 都低的股票应通过 L1。"""
        info = StockInfo(
            code="000001", name="测试银行",
            price=10.0, pe_ttm=6.0, pb=0.6,
            revenue_growth=0.03,     # 营收增长 > 0 ✓
            debt_ratio=0.50,         # 负债率 50% < 65% ✓
            amplitude_20d=0.04,      # 振幅 4% > 3% ✓
            pe_hist_pct=0.10,        # PE 分位 10% ✓
            peg=0.8,                 # PEG < 1.5 ✓
        )
        result = run_l1_filter_impl([info])
        assert len(result) == 1
        assert result[0].exclude_reason == ""

    def test_poor_valuation_excluded(self):
        """估值条件不足（0/3）的股票应被排除。"""
        info = StockInfo(
            code="300999", name="高估值测试",
            price=100.0, pe_ttm=150.0, pb=15.0,
            revenue_growth=0.03, debt_ratio=0.30,
            amplitude_20d=0.05,
            pe_hist_pct=0.85,   # > 0.30
            peg=None,           # PEG 数据缺失
        )
        result = run_l1_filter_impl([info])
        assert len(result) == 0

    def test_two_of_three_valuation_ok(self):
        """满足两项估值条件应通过（PEG 缺失但仍满足 PB+PE）。"""
        info = StockInfo(
            code="000002", name="测试地产",
            price=15.0, pe_ttm=8.0, pb=1.0,
            revenue_growth=0.02, debt_ratio=0.50,
            amplitude_20d=0.04,
            pe_hist_pct=0.10,  # ≤ 0.30 ✓
            peg=None,          # PEG 缺失
        )
        result = run_l1_filter_impl([info])
        assert len(result) == 1  # PB + PE 满足两项

    def test_high_debt_ratio_excluded(self):
        """负债率超标排除。"""
        info = StockInfo(
            code="000002", name="高负债测试",
            price=10.0, pe_ttm=8.0, pb=1.0,
            revenue_growth=0.02, debt_ratio=0.85,
            amplitude_20d=0.04,
            pe_hist_pct=0.10, peg=None,
        )
        result = run_l1_filter_impl([info])
        assert len(result) == 0

    def test_negative_revenue_growth_excluded(self):
        """营收负增长排除。"""
        info = StockInfo(
            code="000002", name="营收下降测试",
            price=10.0, pe_ttm=8.0, pb=1.0,
            revenue_growth=-0.05, debt_ratio=0.50,
            amplitude_20d=0.04,
            pe_hist_pct=0.10, peg=None,
        )
        result = run_l1_filter_impl([info])
        assert len(result) == 0

    def test_low_amplitude_excluded(self):
        """振幅不足排除。"""
        info = StockInfo(
            code="000002", name="横盘测试",
            price=10.0, pe_ttm=8.0, pb=1.0,
            revenue_growth=0.02, debt_ratio=0.50,
            amplitude_20d=0.01,  # 1% < 3%
            pe_hist_pct=0.10, peg=None,
        )
        result = run_l1_filter_impl([info])
        assert len(result) == 0

    def test_star_market_debt_ratio_stricter(self):
        """科创板负债率阈值应该更严（≤ 45%）。"""
        from tradingagents.strategies.value_swing import _MAX_DEBT_RATIO_STAR
        assert _MAX_DEBT_RATIO_STAR == 0.45

    def test_skip_when_field_is_none(self):
        """字段为 None 时不触发排除（数据不可获取）。"""
        info = StockInfo(
            code="000001", name="测试银行",
            price=10.0, pe_ttm=6.0, pb=0.6,
            revenue_growth=None,  # 数据不可获取，不排除
            debt_ratio=None,      # 同上
            amplitude_20d=None,   # 同上
            pe_hist_pct=0.10, peg=None,
        )
        result = run_l1_filter_impl([info])
        assert len(result) == 1  # PE + PB 满足两项，不触发排除


# ── L2 评分测试 ────────────────────────────────────────────────────────────


class TestL2FilterImpl:
    def test_empty_input(self):
        assert run_l2_filter_impl([]) == []

    def test_score_calculation_no_signals(self):
        """无信号的股票评分应为 0。"""
        info = StockInfo(code="000001", name="测试银行")
        result = run_l2_filter_impl([info])
        assert len(result) == 1
        assert result[0].signal_score == 0

    def test_score_with_signals(self):
        """手动设置信号验证评分。"""
        info = StockInfo(
            code="000001", name="测试银行",
            northbound_net_3d=2.0,    # 北向 +1
            fund_flow_main_3d=5000.0, # 主力 +1
            dragon_tiger_inst_net=1000.0,  # 机构 +1
            above_ma20=True,          # MA20 +1
        )
        result = run_l2_filter_impl([info])
        assert len(result) == 1
        assert result[0].signal_score == 4

    def test_score_with_some_signals(self):
        info = StockInfo(
            code="000001", name="测试银行",
            northbound_net_3d=-1.0,   # 北向流出，不加分
            above_ma20=True,          # MA20 +1
            near_ma250=True,          # 接近年线 +1
        )
        result = run_l2_filter_impl([info])
        assert result[0].signal_score == 2

    def test_score_dragon_tiger_small(self):
        """龙虎榜净买入太小（<100万）应忽略。"""
        info = StockInfo(
            code="000001", name="测试银行",
            dragon_tiger_inst_net=50.0,  # < 100万
        )
        result = run_l2_filter_impl([info])
        assert result[0].signal_score == 0

    def test_candidate_limit(self):
        stocks = [
            StockInfo(code=f"{i:06d}", name=f"测试{i}",
                      northbound_net_3d=float(i))
            for i in range(1, 21)
        ]
        result = run_l2_filter_impl(stocks, max_candidates=5)
        assert len(result) <= 5

    def test_sort_by_score_desc(self):
        stocks = [
            StockInfo(code="000001", name="高分",
                      northbound_net_3d=5.0, above_ma20=True),
            StockInfo(code="000002", name="中分",
                      northbound_net_3d=3.0, above_ma20=True),
            StockInfo(code="000003", name="低分"),
        ]
        result = run_l2_filter_impl(stocks, max_candidates=5)
        scores = [s.signal_score for s in result]
        assert scores == sorted(scores, reverse=True)
        assert len(result) == 3

    def test_ma_near_250_extra_score(self):
        """near_ma250 当前设计加分（需要时可调整）。"""
        info = StockInfo(
            code="000001", name="测试",
            near_ma250=True,
        )
        result = run_l2_filter_impl([info])
        assert result[0].signal_score == 1

    def test_ma_both_signals(self):
        """above_ma20 + near_ma250 各加 1 分。"""
        info = StockInfo(
            code="000001", name="测试",
            above_ma20=True,
            near_ma250=True,
        )
        result = run_l2_filter_impl([info])
        assert result[0].signal_score == 2


# ── 集成边界测试 ──────────────────────────────────────────────────────────


class TestFilterEdgeCases:
    def test_low_signal_but_good_valuation(self):
        """估值好但信号少的股票仍应该出现在候选池中（score=1 也有机会）。"""
        info = StockInfo(
            code="000001", name="估值好无信号",
            price=10.0, pe_ttm=5.0, pb=0.5,
            revenue_growth=0.05, debt_ratio=0.40,
            amplitude_20d=0.05,
            pe_hist_pct=0.05, peg=0.5,
            above_ma20=True,
        )
        l1_result = run_l1_filter_impl([info])
        assert len(l1_result) == 1
        l2_result = run_l2_filter_impl(l1_result)
        assert len(l2_result) == 1
        assert l2_result[0].signal_score >= 1  # above_ma20

    def test_full_pipeline_no_network(self):
        """完整 L1→L2 流水线（不联网），验证不崩溃。"""
        stocks = [
            StockInfo(
                code=f"{i:06d}", name=f"股{i}",
                price=10.0, pe_ttm=10.0, pb=1.0,
                revenue_growth=0.03, debt_ratio=0.40,
                amplitude_20d=0.04,
                pe_hist_pct=0.15, peg=1.0,
            )
            for i in range(1, 6)
        ]
        l1 = run_l1_filter_impl(stocks)
        assert len(l1) <= 5
        l2 = run_l2_filter_impl(l1)
        assert len(l2) <= 5

    def test_scan_result_serialization(self):
        """ScanResult 结构的属性访问正常。"""
        r = ScanResult(scan_date="2026-07-15", total_stocks=100,
                      l0_passed=80, l1_passed=12, l2_passed=5)
        assert r.scan_date == "2026-07-15"
        assert r.total_stocks == 100
        assert r.l0_passed == 80
        assert r.l1_passed == 12
        assert r.l2_passed == 5
        assert len(r.candidates) == 0
