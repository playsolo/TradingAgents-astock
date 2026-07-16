"""Mixed market input: auto-infer CN vs US from raw token."""

from __future__ import annotations

from web.analysis_queue import _infer_token_market


def test_infer_market_cn_by_digit_length():
    assert _infer_token_market("300750") == "CN"
    assert _infer_token_market("600519") == "CN"
    assert _infer_token_market("000001") == "CN"
    assert _infer_token_market("688256") == "CN"


def test_infer_market_us_by_non_digit():
    assert _infer_token_market("AAPL") == "US"
    assert _infer_token_market("NVDA") == "US"
    assert _infer_token_market("BRK.B") == "US"
    assert _infer_token_market("MSFT") == "US"
    assert _infer_token_market("FIG") == "US"
    assert _infer_token_market("AMPX") == "US"


def test_infer_market_cn_by_cached_name():
    """If the token is a Chinese name known in the local cache, it's CN."""
    from web.stock_display import remember_resolved_name

    remember_resolved_name("601899", "紫金矿业")
    assert _infer_token_market("紫金矿业") == "CN"


def test_infer_market_us_fallback_when_not_digit_not_cached():
    assert _infer_token_market("SOMETHING_NEW") == "US"
