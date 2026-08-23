"""Unit tests for feature_snapshot helpers."""

import pytest

from tradingagents.dataflows.feature_snapshot import (
    MarketRegimeSnapshot,
    StockFeatureSnapshot,
    _compute_financial_quality,
    format_stock_features_text,
)


@pytest.mark.unit
class TestFinancialQuality:
    def test_cash_conversion_ratio(self):
        income = [{"net_profit": 100, "operating_income": 500}]
        balance = [{"assets_total": 1000, "accounts_receivable": 50, "cash": 200, "total_debt": 100}]
        cash = [{"act_cash_flow_net": 80, "pay_fixed_assets_etc_cash": 20}]
        out = _compute_financial_quality(income, balance, cash)
        assert out["cash_conversion_ratio"] == 0.8
        assert out["fcf_margin"] == 0.12
        assert out["accrual_ratio"] == 0.02
        assert out["receivable_pressure"] == 0.1
        assert out["net_cash_ratio"] == 0.1

    def test_empty_rows(self):
        assert _compute_financial_quality([], [], []) == {}


@pytest.mark.unit
class TestFormatStockFeatures:
    def test_includes_sections(self):
        snap = StockFeatureSnapshot(
            code="600519",
            thscode="600519.SH",
            retrieved_at="2026-01-01 12:00:00",
            valuation={"pe_ttm": 25.0, "ps_ttm": 10.0},
            hot_rank=3,
            hot_heat=99.5,
            financial_quality={"cash_conversion_ratio": 0.95},
        )
        text = format_stock_features_text(snap)
        assert "600519" in text
        assert "pe_ttm" in text
        assert "hot_rank" in text
        assert "cash_conversion_ratio" in text
