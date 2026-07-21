"""Report-prose must not pollute stock_names.json; refresh CLI uses L1 sources."""

from __future__ import annotations

import json

import web.stock_display as stock_display
from web.refresh_stock_names import refresh_stock_names


def test_rejects_infq_become_quote_prose():
    prose = 'INFQ 成为"投资者正在逃离的10只股票" 之一'
    assert stock_display._extract_stock_name_from_text("INFQ", prose) is None
    assert not stock_display._is_plausible_stock_name('成为"', "INFQ")
    assert stock_display.is_junk_stock_name('成为"')
    assert stock_display.is_junk_stock_name("是一家")
    assert stock_display.is_junk_stock_name("上调至")
    assert stock_display.is_junk_stock_name("通信服务")
    assert not stock_display.is_junk_stock_name("Infleqtion")
    assert not stock_display.is_junk_stock_name("紫金矿业")


def test_display_label_does_not_cache_report_extraction(monkeypatch, tmp_path):
    cache = stock_display.StockNameCache(tmp_path / "names.json")
    monkeypatch.setattr(stock_display, "_NAME_CACHE", cache)
    stock_display.resolve_stock_name.cache_clear()
    monkeypatch.setattr(
        stock_display,
        "resolve_stock_name",
        lambda ticker: "Infleqtion",
    )

    state = {
        "news_report": 'INFQ 成为"投资者正在逃离的10只股票" 之一',
        "fundamentals_report": "INFQ（Infleqtion）量子计算",
    }
    assert stock_display.stock_display_label("INFQ", state) == "INFQ Infleqtion"
    # Resolve already trusted; extraction must not overwrite or add junk.
    assert cache.get("INFQ") is None


def test_display_label_does_not_cache_cn_extract_over_english(monkeypatch, tmp_path):
    """CN overlay from reports is display-only — aliases/yfinance own the cache."""
    cache = stock_display.StockNameCache(tmp_path / "names.json")
    monkeypatch.setattr(stock_display, "_NAME_CACHE", cache)
    monkeypatch.setattr(
        stock_display,
        "resolve_stock_name",
        lambda ticker: "MINISO Group Holding Limited",
    )
    state = {"fundamentals_report": "MINISO（名创优品）主营零售"}
    assert stock_display.stock_display_label("MNSO", state) == "MNSO 名创优品"
    assert cache.get("MNSO") is None


def test_resolve_skips_junk_cache_entries(monkeypatch, tmp_path):
    cache = stock_display.StockNameCache(tmp_path / "names.json")
    cache.set("INFQ", '成为"')
    cache.set("AMPX", "是一家")
    monkeypatch.setattr(stock_display, "_NAME_CACHE", cache)
    stock_display.resolve_stock_name.cache_clear()
    stock_display._yfinance_name.cache_clear()
    monkeypatch.setattr(stock_display, "_US_CN_ALIASES", {})
    monkeypatch.setattr(stock_display, "_yfinance_name", lambda code: {
        "INFQ": "Infleqtion",
        "AMPX": "Amprius",
    }.get(code))

    assert stock_display.resolve_stock_name("INFQ") == "Infleqtion"
    assert cache.get("INFQ") == "Infleqtion"
    assert stock_display.resolve_stock_name("AMPX") == "Amprius"
    assert cache.get("AMPX") == "Amprius"


def test_refresh_fix_junk_rewrites_from_resolve(monkeypatch, tmp_path):
    path = tmp_path / "stock_names.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "names": {
                    "INFQ": '成为"',
                    "600519": "贵州茅台",
                    "AMPX": "是一家",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    cache = stock_display.StockNameCache(path)
    monkeypatch.setattr(stock_display, "_NAME_CACHE", cache)
    stock_display.resolve_stock_name.cache_clear()
    stock_display._yfinance_name.cache_clear()
    stock_display._tencent_name.cache_clear()
    monkeypatch.setattr(stock_display, "_US_CN_ALIASES", {})
    monkeypatch.setattr(
        stock_display,
        "_yfinance_name",
        lambda code: {"INFQ": "Infleqtion", "AMPX": "Amprius"}.get(code),
    )
    monkeypatch.setattr(stock_display, "_tencent_name", lambda code: None)
    monkeypatch.setattr(stock_display, "_mootdx_name_if_cached", lambda code: None)

    changes = refresh_stock_names(path=path, fix_junk=True)
    assert {c["code"]: c["new"] for c in changes} == {
        "INFQ": "Infleqtion",
        "AMPX": "Amprius",
    }
    disk = json.loads(path.read_text(encoding="utf-8"))["names"]
    assert disk["INFQ"] == "Infleqtion"
    assert disk["AMPX"] == "Amprius"
    assert disk["600519"] == "贵州茅台"


def test_refresh_codes_only(monkeypatch, tmp_path):
    path = tmp_path / "stock_names.json"
    path.write_text(
        json.dumps({"version": 1, "names": {"INFQ": "Old", "MU": "Keep"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    cache = stock_display.StockNameCache(path)
    monkeypatch.setattr(stock_display, "_NAME_CACHE", cache)
    stock_display.resolve_stock_name.cache_clear()
    stock_display._yfinance_name.cache_clear()
    monkeypatch.setattr(stock_display, "_US_CN_ALIASES", {})
    monkeypatch.setattr(
        stock_display, "_yfinance_name", lambda code: "Infleqtion" if code == "INFQ" else "Micron"
    )

    changes = refresh_stock_names(path=path, codes=["INFQ"])
    assert len(changes) == 1
    assert changes[0]["code"] == "INFQ"
    assert changes[0]["new"] == "Infleqtion"
    disk = json.loads(path.read_text(encoding="utf-8"))["names"]
    assert disk["INFQ"] == "Infleqtion"
    assert disk["MU"] == "Keep"
