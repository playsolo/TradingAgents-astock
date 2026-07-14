"""股票中文名本地缓存：避免每次 HTTP/mootdx 拉取。"""

from web.stock_display import StockNameCache, format_list_ticker_label, resolve_stock_name
import web.stock_display as stock_display


def test_cache_get_set_persists(tmp_path):
    cache = StockNameCache(tmp_path / "names.json")
    assert cache.get("002648") is None
    cache.set("002648", "卫星化学")
    assert cache.get("002648") == "卫星化学"
    # reload from disk
    cache2 = StockNameCache(tmp_path / "names.json")
    assert cache2.get("002648") == "卫星化学"


def test_resolve_uses_cache_before_network(monkeypatch, tmp_path):
    cache = StockNameCache(tmp_path / "names.json")
    cache.set("600519", "贵州茅台")
    monkeypatch.setattr(stock_display, "_NAME_CACHE", cache)
    stock_display.resolve_stock_name.cache_clear()
    stock_display._tencent_name.cache_clear()

    def boom(_code):
        raise AssertionError("should not hit network when cached")

    monkeypatch.setattr(stock_display, "_tencent_name", boom)
    assert resolve_stock_name("600519") == "贵州茅台"


def test_resolve_writes_cache_after_tencent(monkeypatch, tmp_path):
    cache = StockNameCache(tmp_path / "names.json")
    monkeypatch.setattr(stock_display, "_NAME_CACHE", cache)
    stock_display.resolve_stock_name.cache_clear()
    stock_display._tencent_name.cache_clear()

    monkeypatch.setattr(stock_display, "_tencent_name", lambda code: "卫星化学")
    monkeypatch.setattr(stock_display, "_mootdx_name_if_cached", lambda code: None)

    assert resolve_stock_name("002648") == "卫星化学"
    assert cache.get("002648") == "卫星化学"


def test_format_list_uses_cached_name(monkeypatch, tmp_path):
    cache = StockNameCache(tmp_path / "names.json")
    cache.set("002648", "卫星化学")
    monkeypatch.setattr(stock_display, "_NAME_CACHE", cache)
    stock_display.resolve_stock_name.cache_clear()

    monkeypatch.setattr(
        stock_display,
        "_tencent_name",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no network")),
    )
    assert format_list_ticker_label("002648", "2026-07-14") == "002648 卫星化学  ·  2026-07-14"
