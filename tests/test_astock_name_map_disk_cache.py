"""全市场名称↔代码磁盘缓存：命中跳过 mootdx；远程失败可回退磁盘。"""

from __future__ import annotations

import json
import time


def _clear_maps(a_stock):
    a_stock._name_to_code = None
    a_stock._code_to_name = None
    a_stock._mootdx_client = None


def _write_disk(path, n2c, c2n, *, updated_at: float | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "updated_at": updated_at if updated_at is not None else time.time(),
        "name_to_code": n2c,
        "code_to_name": c2n,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_disk_cache_hit_skips_mootdx(tmp_path, monkeypatch):
    from tradingagents.dataflows import a_stock

    _clear_maps(a_stock)
    cache = tmp_path / "astock_name_code_map.json"
    _write_disk(
        cache,
        {"卫星化学": "002648", "贵州茅台": "600519"},
        {"002648": "卫星化学", "600519": "贵州茅台"},
    )
    monkeypatch.setattr(a_stock, "_name_map_cache_path", lambda: cache)
    monkeypatch.setattr(a_stock, "_NAME_MAP_MIN_DISK_ENTRIES", 1)

    def boom_session(*_a, **_k):
        raise AssertionError("must not open mootdx when disk cache hits")

    monkeypatch.setattr(a_stock, "_mootdx_client_session", boom_session)

    n2c, c2n = a_stock._build_name_code_map()
    assert n2c["卫星化学"] == "002648"
    assert c2n["600519"] == "贵州茅台"
    assert a_stock.resolve_ticker("贵州茅台") == "600519"


def test_remote_success_persists_disk_cache(tmp_path, monkeypatch):
    from tradingagents.dataflows import a_stock
    from tests.test_astock_ticker_resolution import _FlakyRawClient

    _clear_maps(a_stock)
    cache = tmp_path / "astock_name_code_map.json"
    monkeypatch.setattr(a_stock, "_name_map_cache_path", lambda: cache)
    monkeypatch.setattr(a_stock, "_NAME_MAP_MIN_DISK_ENTRIES", 1)
    client = _FlakyRawClient(fail_times=0)
    monkeypatch.setattr(a_stock, "_get_mootdx_client", lambda: client)
    monkeypatch.setattr(a_stock, "_reset_mootdx_client", lambda: None)

    n2c, _ = a_stock._build_name_code_map()
    assert n2c["卫星化学"] == "002648"
    assert cache.exists()
    payload = json.loads(cache.read_text(encoding="utf-8"))
    assert payload["name_to_code"]["贵州茅台"] == "600519"


def test_remote_fail_falls_back_to_stale_disk(tmp_path, monkeypatch):
    from tradingagents.dataflows import a_stock

    _clear_maps(a_stock)
    cache = tmp_path / "astock_name_code_map.json"
    _write_disk(
        cache,
        {"科森科技": "603626"},
        {"603626": "科森科技"},
        updated_at=time.time() - a_stock._NAME_MAP_DISK_TTL_SECONDS - 100,
    )
    monkeypatch.setattr(a_stock, "_name_map_cache_path", lambda: cache)
    monkeypatch.setattr(a_stock, "_NAME_MAP_MIN_DISK_ENTRIES", 1)

    def boom_session(*_a, **_k):
        raise ConnectionResetError(104, "Connection reset by peer")

    monkeypatch.setattr(a_stock, "_mootdx_client_session", boom_session)
    monkeypatch.setattr(a_stock, "_reset_mootdx_client", lambda: None)

    assert a_stock.resolve_ticker("科森科技") == "603626"


def test_fresh_disk_skips_remote(tmp_path, monkeypatch):
    from tradingagents.dataflows import a_stock

    _clear_maps(a_stock)
    cache = tmp_path / "astock_name_code_map.json"
    _write_disk(
        cache,
        {"七彩化学": "300758"},
        {"300758": "七彩化学"},
        updated_at=time.time(),
    )
    monkeypatch.setattr(a_stock, "_name_map_cache_path", lambda: cache)
    monkeypatch.setattr(a_stock, "_NAME_MAP_MIN_DISK_ENTRIES", 1)

    def counting_session(*_a, **_k):
        raise AssertionError("fresh disk must skip remote")

    monkeypatch.setattr(a_stock, "_mootdx_client_session", counting_session)
    assert a_stock.resolve_ticker("七彩化学") == "300758"


def test_get_mootdx_client_skips_handshake_failures(monkeypatch):
    from tradingagents.dataflows import a_stock

    a_stock._mootdx_client = None
    monkeypatch.setattr(
        a_stock,
        "_TDX_SERVERS",
        [("1.1.1.1", 7709), ("2.2.2.2", 7709)],
    )
    monkeypatch.setattr(a_stock, "_probe_tdx", lambda ip, port, timeout=2.0: True)

    class _OkClient:
        def stock_count(self, market=0):
            return 10

        def close(self):
            pass

    def try_connect(ip, port, timeout=5.0):
        if ip == "1.1.1.1":
            raise ConnectionResetError(104, "Connection reset by peer")
        if ip == "2.2.2.2":
            return _OkClient()
        raise RuntimeError(f"unexpected {ip}")

    monkeypatch.setattr(a_stock, "_connect_tdx_quotes", try_connect)

    client = a_stock._get_mootdx_client()
    assert isinstance(client, _OkClient)
