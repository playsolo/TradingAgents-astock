"""Ticker 名称解析健壮性测试。

复现 #: mootdx 的 get_security_count() 瞬时返回 None 时旧实现一次即放弃。
另外：client.stocks() 走 pandas→pyarrow，在 Streamlit 工作线程会触发 macOS
libarrow/mimalloc SIGSEGV（Python quit）。名称映射必须绕过 stocks()/pandas。
"""

from __future__ import annotations

import pytest


_NONE_COUNT_ERR = "'>' not supported between instances of 'NoneType' and 'int'"


def _fake_rows():
    return [
        {"code": "002648", "name": "卫星化学"},
        {"code": "600519", "name": "贵州茅台"},
    ]


class _FlakyRawClient:
    """前 `fail_times` 次 stock_count 抛 None-count TypeError，之后返回正常列表。"""

    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.calls = 0
        self.client = self  # mootdx Quotes: .client 为底层 tdx client

    def stock_count(self, market=0):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise TypeError(_NONE_COUNT_ERR)
        return len(_fake_rows())

    def get_security_list(self, market=0, start=0):
        if start == 0:
            return _fake_rows()
        return []

    def stocks(self, market=0):
        raise AssertionError("name map must not call client.stocks() (pandas/pyarrow crash path)")


def test_build_name_code_map_retries_after_none_count(tmp_path, monkeypatch):
    from tradingagents.dataflows import a_stock

    a_stock._name_to_code = None
    a_stock._code_to_name = None
    flaky = _FlakyRawClient(fail_times=2)
    resets = {"n": 0}

    monkeypatch.setattr(
        a_stock, "_name_map_cache_path", lambda: tmp_path / "missing.json"
    )
    monkeypatch.setattr(a_stock, "_NAME_MAP_MIN_DISK_ENTRIES", 1)
    monkeypatch.setattr(a_stock, "_get_mootdx_client", lambda: flaky)
    monkeypatch.setattr(
        a_stock,
        "_reset_mootdx_client",
        lambda: resets.__setitem__("n", resets["n"] + 1),
    )

    n2c, c2n = a_stock._build_name_code_map()
    assert n2c["卫星化学"] == "002648"
    assert c2n["600519"] == "贵州茅台"
    assert resets["n"] >= 1


def test_build_name_code_map_never_calls_stocks(tmp_path, monkeypatch):
    """回归：名称映射不得走 mootdx.stocks()（pandas→pyarrow→SIGSEGV）。"""
    from tradingagents.dataflows import a_stock

    a_stock._name_to_code = None
    a_stock._code_to_name = None
    client = _FlakyRawClient(fail_times=0)
    monkeypatch.setattr(
        a_stock, "_name_map_cache_path", lambda: tmp_path / "missing.json"
    )
    monkeypatch.setattr(a_stock, "_NAME_MAP_MIN_DISK_ENTRIES", 1)
    monkeypatch.setattr(a_stock, "_get_mootdx_client", lambda: client)

    def _boom(*_a, **_k):
        raise AssertionError("must not call stocks()")

    monkeypatch.setattr(client, "stocks", _boom)
    a_stock._build_name_code_map()  # stocks() 若被调用会 AssertionError
