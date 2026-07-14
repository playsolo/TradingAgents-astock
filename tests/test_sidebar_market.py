"""Sidebar market selection: A-share resolve vs US ticker passthrough."""

from __future__ import annotations

from web.components import sidebar


def test_normalize_us_ticker_uppercases_and_strips():
    assert sidebar._normalize_us_ticker("  nvda  ") == "NVDA"
    assert sidebar._normalize_us_ticker("brk.b") == "BRK.B"


def test_normalize_us_ticker_rejects_empty():
    code, err = sidebar._resolve_user_input_for_market("   ", "US")
    assert code == ""
    assert err


def test_resolve_us_skips_a_share_resolver(monkeypatch):
    def boom(_raw):
        raise AssertionError("resolve_ticker should not be called for US")

    monkeypatch.setattr(
        "tradingagents.dataflows.a_stock.resolve_ticker",
        boom,
        raising=False,
    )
    # Patch the import site used by sidebar helper
    import tradingagents.dataflows.a_stock as a_stock

    monkeypatch.setattr(a_stock, "resolve_ticker", boom)

    code, err = sidebar._resolve_user_input_for_market("AAPL", "US")
    assert err is None
    assert code == "AAPL"


def test_resolve_cn_still_uses_resolve_ticker(monkeypatch):
    monkeypatch.setattr(
        "tradingagents.dataflows.a_stock.resolve_ticker",
        lambda raw: "300750" if "宁德" in raw or raw == "300750" else (_ for _ in ()).throw(
            ValueError("bad")
        ),
    )
    code, err = sidebar._resolve_user_input_for_market("宁德时代", "CN")
    assert err is None
    assert code == "300750"
