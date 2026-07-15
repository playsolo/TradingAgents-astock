"""中文名解析应优先走本地 stock_names 反查，避免卡住 Streamlit 主线程。"""

import web.stock_display as stock_display
from web.components import sidebar as sidebar_mod


def test_lookup_code_by_cached_name_exact(tmp_path, monkeypatch):
    cache_path = tmp_path / "stock_names.json"
    cache = stock_display.StockNameCache(cache_path)
    cache.set("002648", "卫星化学")
    monkeypatch.setattr(stock_display, "_NAME_CACHE", cache)

    assert stock_display.lookup_code_by_cached_name("卫星化学") == "002648"
    assert stock_display.lookup_code_by_cached_name(" 卫星化学 ") == "002648"


def test_lookup_code_by_cached_name_misses_without_mootdx(tmp_path, monkeypatch):
    cache = stock_display.StockNameCache(tmp_path / "stock_names.json")
    monkeypatch.setattr(stock_display, "_NAME_CACHE", cache)

    assert stock_display.lookup_code_by_cached_name("不存在的公司") is None


def test_lookup_code_by_cached_name_rejects_partial_match(tmp_path, monkeypatch):
    cache = stock_display.StockNameCache(tmp_path / "stock_names.json")
    cache.set("002648", "卫星化学")
    monkeypatch.setattr(stock_display, "_NAME_CACHE", cache)

    # Sparse cache + substring would wrongly bind; exact-only keeps it safe.
    assert stock_display.lookup_code_by_cached_name("卫星") is None


def test_sidebar_resolve_prefers_cached_name(monkeypatch):
    """侧栏解析命中本地缓存时不得触发 mootdx 全市场映射。"""
    calls = {"mootdx": 0}

    def boom_resolve(raw: str) -> str:
        calls["mootdx"] += 1
        raise AssertionError("must not call resolve_ticker when cache hits")

    monkeypatch.setattr(
        "tradingagents.dataflows.a_stock.resolve_ticker",
        boom_resolve,
    )
    monkeypatch.setattr(
        stock_display,
        "lookup_code_by_cached_name",
        lambda name: "002648" if "卫星" in name else None,
    )

    code, err = sidebar_mod._resolve_user_input("卫星化学")
    assert code == "002648"
    assert err is None
    assert calls["mootdx"] == 0
