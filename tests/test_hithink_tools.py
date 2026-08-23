"""Tests for HiThink agent tools and routing."""

import pytest
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=True)

from tradingagents.dataflows.hithink_data import (
    get_hithink_market_sentiment,
    get_hithink_valuation_snapshot,
)
from tradingagents.dataflows.interface import get_category_for_method, route_to_vendor


@pytest.mark.unit
class TestHiThinkRouting:
    def test_category_mapping(self):
        assert get_category_for_method("get_market_sentiment") == "hithink_enhanced"
        assert get_category_for_method("get_valuation_snapshot") == "hithink_enhanced"

    def test_disabled_returns_guidance(self, monkeypatch):
        monkeypatch.setenv("HITHINK_ENABLED", "false")
        monkeypatch.delenv("HITHINK_FINANCE_API_KEY", raising=False)
        out = get_hithink_market_sentiment("600519")
        assert "未启用" in out
        assert not out.strip().startswith("[数据缺失")


@pytest.mark.integration
class TestHiThinkToolsLive:
    def test_route_valuation_snapshot(self):
        from tradingagents.dataflows.hithink_client import is_hithink_enabled

        if not is_hithink_enabled():
            pytest.skip("HiThink not enabled")
        out = route_to_vendor("get_valuation_snapshot", "600519")
        assert "Valuation Snapshot" in out
        assert "pe_ttm" in out or "pb_mrq" in out

    def test_route_market_regime(self):
        from tradingagents.dataflows.hithink_client import is_hithink_enabled

        if not is_hithink_enabled():
            pytest.skip("HiThink not enabled")
        out = route_to_vendor("get_market_regime", "")
        assert "Market Regime" in out
