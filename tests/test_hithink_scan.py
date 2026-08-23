"""Unit tests for HiThink scan funnel cache (mocked)."""

from unittest.mock import MagicMock, patch

import pytest

from tradingagents.dataflows.feature_snapshot import MarketRegimeSnapshot
from tradingagents.strategies.hithink_scan import (
    HiThinkScanCache,
    auction_fund_flow_proxy,
    build_hithink_scan_cache,
    clear_hithink_scan_cache,
    dragon_tiger_inst_net_wan,
    market_regime_summary,
    merge_hithink_hot_topics,
    value_swing_ps_ttm_too_high,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_hithink_scan_cache()
    yield
    clear_hithink_scan_cache()


def _mock_client() -> MagicMock:
    cli = MagicMock()
    cli.available = True
    cli.skyrocket_list.return_value = [{"ticker": "300001"}]
    cli.dragon_tiger_list.return_value = {
        "stock_items": [
            {"ticker": "600519", "org_net_value": 2_000_000, "net_value": 1_500_000},
        ]
    }
    cli.auction_snapshot.return_value = [
        {"ticker": "600519", "auction_volume_ratio": 1.5, "auction_pct": 0.8},
        {"ticker": "000001", "auction_volume_ratio": 0.9, "auction_pct": 0.2},
    ]
    cli.valuations_snapshot.return_value = [
        {"ticker": "600519", "ps_ttm": 8.5},
        {"ticker": "000001", "ps_ttm": 25.0},
    ]
    return cli


@pytest.mark.unit
@patch("tradingagents.strategies.hithink_scan.is_hithink_enabled", return_value=True)
@patch("tradingagents.strategies.hithink_scan.build_market_regime_snapshot")
def test_build_cache_merges_auction_and_valuations(_regime, _enabled):
    _regime.return_value = MarketRegimeSnapshot(
        retrieved_at="2026-08-23T10:00:00",
        limit_up_count=100,
        limit_down_count=5,
        limit_break_count=50,
        hot_top=[{"ticker": "600519", "rank": 3}],
    )
    cli = _mock_client()

    cache = build_hithink_scan_cache(["600519"], client=cli, fetch_valuations=True)
    assert cache is not None
    assert cache.market_penalty == -1
    assert cache.hot_rank("600519") == 3
    assert dragon_tiger_inst_net_wan(cache, "600519") == 200.0
    assert auction_fund_flow_proxy(cache, "600519") == 1.5
    assert auction_fund_flow_proxy(cache, "000001") is None

    cache2 = build_hithink_scan_cache(["000001"], client=cli, fetch_valuations=True)
    assert cache2 is cache
    assert "000001" in cache2.valuations
    assert cache2.valuations["000001"]["ps_ttm"] == 25.0


@pytest.mark.unit
def test_value_swing_ps_ttm_too_high():
    cache = HiThinkScanCache(scan_date="2026-08-23", valuations={"600519": {"ps_ttm": 21}})
    assert value_swing_ps_ttm_too_high(cache, "600519") is True
    assert value_swing_ps_ttm_too_high(cache, "000001") is False


@pytest.mark.unit
@patch("tradingagents.strategies.hithink_scan.is_hithink_enabled", return_value=True)
@patch("tradingagents.strategies.hithink_scan.build_hithink_scan_cache")
def test_merge_hithink_hot_topics(_build, _enabled):
    _build.return_value = HiThinkScanCache(
        scan_date="2026-08-23",
        hot_rank_by_code={"600519": 2},
    )
    merged = merge_hithink_hot_topics({"AI": ["000001"]})
    assert "AI" in merged
    assert "600519" in merged["HiThink热榜#2"]


@pytest.mark.unit
def test_market_regime_summary_fragile():
    cache = HiThinkScanCache(
        scan_date="2026-08-23",
        market_limit_up=100,
        market_limit_down=8,
        market_limit_break=40,
        market_penalty=-1,
    )
    text = market_regime_summary(cache)
    assert "涨停100" in text
    assert "市场脆弱" in text
