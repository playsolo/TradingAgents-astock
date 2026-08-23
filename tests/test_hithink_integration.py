"""Live integration tests against HiThink API (requires .env key)."""

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=True)

from tradingagents.dataflows.feature_snapshot import (
    build_market_regime_snapshot,
    build_stock_feature_snapshot,
    format_market_regime_text,
    format_stock_features_text,
)
from tradingagents.dataflows.hithink_client import (
    get_hithink_client,
    is_hithink_enabled,
)


def _live_enabled() -> bool:
    return is_hithink_enabled() and bool(os.getenv("HITHINK_FINANCE_API_KEY"))


pytestmark = pytest.mark.skipif(
    not _live_enabled(),
    reason="HITHINK_FINANCE_API_KEY + HITHINK_ENABLED required for live tests",
)


@pytest.mark.integration
class TestHiThinkLive:
    def test_search_moutai(self):
        cli = get_hithink_client()
        items = cli.search_tickers("600519", limit=1)
        assert items
        assert items[0]["thscode"] == "600519.SH"
        assert "茅台" in items[0]["name"]

    def test_valuations_snapshot(self):
        cli = get_hithink_client()
        rows = cli.valuations_snapshot(["600519.SH"])
        assert rows
        assert rows[0].get("pe_ttm") is not None or rows[0].get("pb_mrq") is not None

    def test_hot_stock_list(self):
        cli = get_hithink_client()
        rows = cli.hot_stock_list(period="day")
        assert isinstance(rows, list)

    def test_stock_feature_snapshot(self):
        snap = build_stock_feature_snapshot("600519", include_financials=True)
        assert snap is not None
        assert snap.thscode == "600519.SH"
        text = format_stock_features_text(snap)
        assert "HiThink Feature Snapshot" in text
        assert snap.valuation or snap.hot_rank is not None or snap.financial_quality

    def test_market_regime_snapshot(self):
        snap = build_market_regime_snapshot()
        assert snap is not None
        text = format_market_regime_text(snap)
        assert "Market Regime" in text
