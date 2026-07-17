"""US session quote: extended-hours preference + context formatting."""

from __future__ import annotations

import sys
import types

from tradingagents.dataflows import us_session_quote
from web.us_bridge import us_quote


def test_last_tradable_prefers_post_when_market_post():
    price, src = us_session_quote.last_tradable_price(
        {
            "market_state": "POST",
            "regular_market_price": 402.33,
            "post_market_price": 377.0,
        }
    )
    assert price == 377.0
    assert src == "post_market"


def test_last_tradable_prefers_post_when_diverges_without_state():
    price, src = us_session_quote.last_tradable_price(
        {
            "regular_market_price": 402.33,
            "post_market_price": 377.0,
        }
    )
    assert price == 377.0
    assert src == "post_market"


def test_last_tradable_keeps_regular_when_post_matches():
    price, src = us_session_quote.last_tradable_price(
        {
            "market_state": "REGULAR",
            "regular_market_price": 402.33,
            "post_market_price": 402.40,
        }
    )
    assert price == 402.33
    assert src == "regular_session"


def test_format_block_warns_not_to_treat_close_as_current():
    block = us_session_quote.format_us_session_quote_block(
        "ISRG",
        {
            "symbol": "ISRG",
            "market_state": "POST",
            "regular_market_price": 402.33,
            "post_market_price": 377.0,
            "post_market_change_pct": -6.3,
            "previous_close": 394.0,
        },
    )
    assert "ISRG" in block
    assert "402.33" in block
    assert "377" in block
    assert "post_market" in block
    assert "Do NOT describe the regular close" in block
    assert "extended-hours" in block


def test_format_block_empty_when_no_prices():
    assert (
        us_session_quote.format_us_session_quote_block("ISRG", {"symbol": "ISRG"})
        == ""
    )


def test_bridge_shim_reexports_format():
    block = us_quote.format_us_session_quote_block(
        "ISRG",
        {
            "symbol": "ISRG",
            "market_state": "POST",
            "regular_market_price": 400.0,
            "post_market_price": 380.0,
        },
    )
    assert "380" in block


def test_fetch_us_session_quote_maps_yfinance_info(monkeypatch):
    class _Ticker:
        info = {
            "regularMarketPrice": 402.33,
            "postMarketPrice": 377.0,
            "postMarketChangePercent": -6.3,
            "marketState": "POST",
            "regularMarketPreviousClose": 394.0,
            "shortName": "Intuitive Surgical",
        }
        fast_info = {}

    fake_yf = types.ModuleType("yfinance")
    fake_yf.Ticker = lambda symbol: _Ticker()
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    quote = us_session_quote.fetch_us_session_quote("isrg")
    assert quote["symbol"] == "ISRG"
    assert quote["regular_market_price"] == 402.33
    assert quote["post_market_price"] == 377.0
    assert quote["market_state"] == "POST"
    assert quote["short_name"] == "Intuitive Surgical"
