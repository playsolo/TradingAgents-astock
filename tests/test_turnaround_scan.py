"""错杀反转策略：模块、W底检测、评分、序列化、CLI 集成测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tradingagents.strategies.scan_runner import result_dict_from_scan, run_scan_job
from tradingagents.strategies.scan_store import (
    SCAN_STATUS_COMPLETED,
    STRATEGY_TURNAROUND,
    ValueSwingScanStore,
    expand_strategies,
    resolve_strategy,
)
from tradingagents.strategies.turnaround import (
    TurnaroundStockInfo,
    ScanResult,
    _detect_w_bottom,
    compute_turnaround_score,
    selection_rules_snapshot,
    l2_score_max,
    l2_factor_hits,
    why_selected_line,
)


# ── 模块基础 ──────────────────────────────────────────────────────────────

def test_module_imports():
    """确保 turnaround 模块正确导入且导出所有必要符号。"""
    from tradingagents.strategies.turnaround import (
        run_turnaround_scan,
        run_l0_filter,
        run_l0_5_filter,
        run_l1_news_filter,
        run_l1_5_money_filter,
        run_l2_technical_filter,
        run_l3_catalyst_and_rank,
    )
    assert callable(run_turnaround_scan)
    assert callable(run_l0_filter)
    assert callable(run_l0_5_filter)
    assert callable(run_l1_news_filter)
    assert callable(run_l1_5_money_filter)
    assert callable(run_l2_technical_filter)
    assert callable(run_l3_catalyst_and_rank)


def test_selection_rules_snapshot():
    """规则快照格式完整。"""
    snap = selection_rules_snapshot()
    assert snap["strategy"] == "turnaround"
    assert snap["name"] == "错杀反转"
    assert len(snap["l0"]) == 6
    assert len(snap["l0_5"]) == 5
    assert len(snap["l1"]) == 2
    assert len(snap["l1_5"]) == 3
    assert len(snap["l2"]) == 5
    assert len(snap["l3"]) == 3


def test_l2_score_max():
    """L3 展示满分应为生效因子数。"""
    assert l2_score_max() == 6


# ── W底检测 ──────────────────────────────────────────────────────────────

class TestDetectWBottom:
    """W 双底形态检测算法测试。"""

    def test_no_pattern_on_insufficient_data(self):
        """数据不足时不报 W 底。"""
        found, l1, l2, p = _detect_w_bottom([10] * 10, window=60)
        assert found is False

    def test_classic_w_bottom(self):
        """经典 W 底：两次探底后回升。"""
        # Build a clear W shape:
        # fill with 20 flat, then two distinct V dips, then breakout
        prices = [20.0] * 30           # Flat lead-in
        prices += [
            20, 19, 18, 17, 16, 15, 14.0, 14.0, 15, 16, 17, 18, 19, 20,  # First V (bottom at 14.0)
            20, 19, 18, 17, 16, 15, 14.5, 14.0, 15, 16, 17, 18, 19, 20,  # Second V (bottom at 14.0)
            21, 22, 23, 24, 25, 26, 27, 28, 29, 30,  # Breakout
        ]
        # Total: 30 + 14 + 14 + 10 = 68 values

        found, l1, l2, peak = _detect_w_bottom(prices, window=60)
        assert found is True
        assert l1 is not None and l2 is not None
        # Two lows should be very close
        assert abs(l2 - l1) / max(l1, 0.01) < 0.05

    def test_w_bottom_with_volatile_data(self):
        """带波动的 W 底仍能检测。"""
        prices = [20.0] * 20
        prices += [
            20, 19, 18, 17, 16, 15.0, 14.0, 14.5, 15, 16, 17, 18, 19,  # First V
            19, 18, 17, 16, 15, 14.5, 14.0, 14.5, 15, 16, 17, 18, 19,  # Second V
            20, 21, 22, 23, 24, 25, 26,  # Breakout
        ]
        found, l1, l2, peak = _detect_w_bottom(prices, window=50)
        # Should find two minima near ~14.0
        assert found is True

    def test_single_v_bottom_no_w(self):
        """单次 V 型反转不算 W 底。"""
        prices = [20.0] * 10
        prices += [20, 18, 16, 14, 16, 18, 20, 22, 24, 26]
        found, l1, l2, peak = _detect_w_bottom(prices, window=30)
        assert found is False

    def test_flat_trend_no_pattern(self):
        """横盘无 W 底。"""
        prices = [10.1, 10.0, 10.2, 10.0, 10.1, 10.2] * 10
        found, _, _, _ = _detect_w_bottom(prices, window=60)
        assert found is False


# ── 数据结构 ──────────────────────────────────────────────────────────────

class TestDataStructures:
    def test_turnaround_stock_info_defaults(self):
        info = TurnaroundStockInfo(code="600828")
        assert info.code == "600828"
        assert info.name == ""
        assert info.pb == 0.0
        assert info.signal_score == 0
        assert info.lane == "analyze"
        assert not info.w_bottom_found
        assert not info.has_negative_news

    def test_scan_result_defaults(self):
        result = ScanResult(scan_date="2026-07-29")
        assert result.scan_date == "2026-07-29"
        assert result.total_stocks == 0
        assert result.strategy == "turnaround"
        assert result.candidates == []


# ── 评分逻辑 ──────────────────────────────────────────────────────────────

class TestScoring:
    def test_baseline_score_zero(self):
        """无任何信号的候选分数为 0。"""
        info = TurnaroundStockInfo(code="000001", pb=2.5)
        score = compute_turnaround_score(info)
        assert score == 0

    def test_asset_revalue_gets_bonus(self):
        """PB < 1 的破净股获得资产重估加分。"""
        info = TurnaroundStockInfo(code="000001", pb=0.8, pb_safe=True)
        # pb_safe + PB < 1 → +2, asset_revalue → +1
        score = compute_turnaround_score(info)
        assert score >= 2  # pb_safe + pb<1 bonus

    def test_positive_revenue_adds_score(self):
        """营收正增长 +1。"""
        info = TurnaroundStockInfo(code="000001", pb=1.5, pb_safe=True, revenue_yoy=0.15)
        score = compute_turnaround_score(info)
        assert score >= 2  # pb_safe + revenue positive

    def test_news_resilient_adds_score(self):
        """利空出尽 +1。"""
        info = TurnaroundStockInfo(code="000001", pb=1.5, pb_safe=True, news_resilient=True)
        score = compute_turnaround_score(info)
        assert score >= 2  # pb_safe + news_resilient

    def test_w_bottom_technical_bonus(self):
        """W 底形态 +2。"""
        info = TurnaroundStockInfo(code="000001", pb=1.5, pb_safe=True, w_bottom_found=True)
        score = compute_turnaround_score(info)
        assert score >= 3  # pb_safe(1) + w_bottom(2)

    def test_management_action_strong_signal(self):
        """增持信号 +2。"""
        info = TurnaroundStockInfo(code="000001", pb=1.5, pb_safe=True, management_action=True)
        score = compute_turnaround_score(info)
        assert score >= 3  # pb_safe(1) + mgmt(2)

    def test_full_signal_stock(self):
        """全信号命中的理想候选。"""
        info = TurnaroundStockInfo(
            code="600828",
            name="茂业商业",
            pb=0.97,
            pb_safe=True,
            revenue_yoy=0.05,
            ocf_ttm=1e8,
            news_resilient=True,
            volume_bottomed=True,
            smart_money_active=True,
            w_bottom_found=True,
            volume_breakout=True,
            neckline_break=True,
            main_force_turned=True,
            above_ma20=True,
            industry_resonance=True,
            asset_revalue=True,
            management_action=True,
        )
        score = compute_turnaround_score(info)
        assert score >= 10  # All major signals hit


# ── 因子展示 ──────────────────────────────────────────────────────────────

class TestFactorHits:
    def test_empty_candidate_no_hits(self):
        info = TurnaroundStockInfo(code="000001")
        hits = l2_factor_hits(info)
        assert len(hits) == 6
        assert all(h["hit"] is False for h in hits)

    def test_active_candidate_shows_hits(self):
        info = TurnaroundStockInfo(
            code="000001",
            w_bottom_found=True,
            volume_breakout=True,
            smart_money_active=True,
        )
        hits = l2_factor_hits(info)
        w_hit = next(h for h in hits if h["key"] == "w_bottom_found")
        assert w_hit["hit"] is True
        vol_hit = next(h for h in hits if h["key"] == "volume_breakout")
        assert vol_hit["hit"] is True
        sm_hit = next(h for h in hits if h["key"] == "smart_money_active")
        assert sm_hit["hit"] is True

    def test_why_selected_line(self):
        info = TurnaroundStockInfo(
            code="600828",
            pb=0.97,
            w_bottom_found=True,
            news_resilient=True,
            asset_revalue=True,
            signal_score=8,
        )
        line = why_selected_line(info)
        assert "破净" in line
        assert "W底形态" in line
        assert "利空出尽" in line


# ── 序列化 ───────────────────────────────────────────────────────────────

def _fake_turnaround_scan(*, max_candidates: int = 15) -> ScanResult:
    return ScanResult(
        scan_date="2026-07-29",
        total_stocks=100,
        l0_passed=80,
        l0_5_passed=30,
        l1_passed=20,
        l1_5_passed=10,
        l2_passed=5,
        l3_passed=2,
        candidates=[
            TurnaroundStockInfo(
                code="600828",
                name="茂业商业",
                price=3.77,
                pe_ttm=-23,
                pb=0.97,
                signal_score=10,
                revenue_yoy=-0.12,
                debt_ratio=0.58,
                ocf_ttm=2e8,
                has_negative_news=True,
                news_resilient=True,
                volume_bottomed=True,
                smart_money_active=True,
                w_bottom_found=True,
                volume_breakout=True,
                neckline_break=True,
                main_force_turned=True,
                above_ma20=True,
                ret_5d=0.05,
                industry_resonance=False,
                asset_revalue=True,
                management_action=False,
                lane="analyze",
            ),
        ][:max_candidates],
        duration_seconds=30.0,
    )


def test_result_dict_shape():
    d = result_dict_from_scan(
        _fake_turnaround_scan(), strategy=STRATEGY_TURNAROUND
    )
    assert d["strategy"] == STRATEGY_TURNAROUND
    assert d["ok"] is True
    assert d["l0_passed"] == 80
    assert d["l0_5_passed"] == 30
    assert d["l1_passed"] == 20
    assert d["l1_5_passed"] == 10
    assert d["l2_passed"] == 5
    assert d["l3_passed"] == 2


def test_result_dict_candidate_fields():
    d = result_dict_from_scan(
        _fake_turnaround_scan(), strategy=STRATEGY_TURNAROUND
    )
    c = d["candidates"][0]
    assert c["code"] == "600828"
    assert c["name"] == "茂业商业"
    assert c["pb"] == 0.97
    assert c["w_bottom_found"] is True
    assert c["asset_revalue"] is True
    assert "why" in c
    assert "factor_hits" in c
    assert "lane" in c
    assert c["score_max"] == l2_score_max()


# ── 策略注册 ─────────────────────────────────────────────────────────────

def test_strategy_registered_in_scan_store():
    assert resolve_strategy(STRATEGY_TURNAROUND) == STRATEGY_TURNAROUND


def test_expand_strategies_all_includes_turnaround():
    strategies = expand_strategies("all")
    assert STRATEGY_TURNAROUND in strategies
    assert "value_swing" in strategies
    assert "growth_accel" in strategies


def test_expand_strategies_both_excludes_turnaround():
    strategies = expand_strategies("both")
    assert STRATEGY_TURNAROUND not in strategies
    assert "value_swing" in strategies
    assert "growth_accel" in strategies


def test_expand_strategies_single_turnaround():
    strategies = expand_strategies(STRATEGY_TURNAROUND)
    assert strategies == [STRATEGY_TURNAROUND]


# ── CLI 集成 ─────────────────────────────────────────────────────────────

def test_cli_accepts_turnaround_strategy():
    from tradingagents.strategies.scan_cli import build_parser
    parser = build_parser()
    ns = parser.parse_args(["--strategy", "turnaround"])
    assert ns.strategy == "turnaround"


def test_cli_accepts_all_strategy():
    from tradingagents.strategies.scan_cli import build_parser
    parser = build_parser()
    ns = parser.parse_args(["--strategy", "all"])
    assert ns.strategy == "all"


# ── 扫描任务集成 ──────────────────────────────────────────────────────────

def test_scan_fn_resolves_turnaround():
    from tradingagents.strategies.scan_runner import _scan_fn_for_strategy
    from tradingagents.strategies.turnaround import run_turnaround_scan

    fn = _scan_fn_for_strategy(STRATEGY_TURNAROUND)
    assert fn is run_turnaround_scan


def test_turnaround_scan_store_path():
    store = ValueSwingScanStore(strategy=STRATEGY_TURNAROUND)
    assert "turnaround" in str(store.path).lower()
    assert "turnaround" in str(store.archive_dir).lower()


# ── L1 公告 API 不可用时的 bypass 行为 ────────────────────────────────────

def test_l1_bypass_when_all_no_news():
    """所有股票无负面公告 → API 不可用 → L1 全部放行。"""
    from unittest.mock import patch
    from tradingagents.strategies.turnaround import run_l1_news_filter

    stocks = [
        TurnaroundStockInfo(code=f"60000{i}", name=f"测试{i}", pb=1.5, pb_safe=True)
        for i in range(5)
    ]

    # 模拟 API 全部返回无负面公告
    with patch(
        "tradingagents.strategies.turnaround._safe_call",
        return_value=(False, None),
    ):
        result = run_l1_news_filter(stocks)

    # 全部放行
    assert len(result) == len(stocks)
    # 所有 exclude_reason 应为 None
    assert all(info.exclude_reason is None for info in result)

def test_l1_bypass_constant_reasonable():
    """Bypass 阈值在合理范围（≥2, ≤total/2）。"""
    from tradingagents.strategies.turnaround import _L1_API_BYPASS_CONSECUTIVE

    assert _L1_API_BYPASS_CONSECUTIVE >= 2
    assert _L1_API_BYPASS_CONSECUTIVE <= 20  # 不会等太久
