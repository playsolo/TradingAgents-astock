"""Fund-flow history SSL failures must not wipe realtime data."""

from __future__ import annotations

import requests


def test_get_fund_flow_keeps_realtime_when_history_ssl_fails(monkeypatch):
    from tradingagents.dataflows import a_stock

    monkeypatch.setattr(a_stock, "_EM_MIN_INTERVAL", 0.0)
    monkeypatch.setattr(a_stock, "_EM_RETRY_BASE_DELAY", 0.0)
    a_stock._em_last_call[0] = 0.0

    class _RtResp:
        def json(self):
            return {
                "data": {
                    "klines": [
                        "2026-07-14 14:00,1000000,0,0,200000,800000",
                    ]
                }
            }

    def fake_em_get(url, params=None, headers=None, timeout=15, **kwargs):
        if "daykline" in url:
            raise requests.exceptions.SSLError("record layer failure")
        return _RtResp()

    monkeypatch.setattr(a_stock, "_em_get", fake_em_get)
    monkeypatch.setattr(
        a_stock, "_sina_fund_flow_history", lambda *a, **k: []
    )

    text = a_stock.get_fund_flow("002648", "2026-07-14", include_history=True)

    assert text.startswith("Error") is False
    assert "Realtime Minute Flow" in text
    assert "主力净流入" in text
    assert "历史日度暂不可用" in text
    assert "[数据缺失" not in text


def test_get_fund_flow_falls_back_to_sina_when_em_realtime_fails(monkeypatch):
    """Eastmoney push2 disconnect must not yield Error if Sina daily works."""
    from tradingagents.dataflows import a_stock

    monkeypatch.setattr(a_stock, "_EM_MIN_INTERVAL", 0.0)
    monkeypatch.setattr(a_stock, "_EM_RETRY_BASE_DELAY", 0.0)
    a_stock._em_last_call[0] = 0.0

    def fake_em_get(url, params=None, headers=None, timeout=15, **kwargs):
        raise requests.exceptions.ConnectionError(
            "Remote end closed connection without response"
        )

    monkeypatch.setattr(a_stock, "_em_get", fake_em_get)
    monkeypatch.setattr(
        a_stock,
        "_sina_fund_flow_history",
        lambda code, days=20: [
            {
                "date": "2026-07-15",
                "main_net": 1_190_000.0,
                "net_amount": 940_000.0,
            },
            {
                "date": "2026-07-14",
                "main_net": -1_550_000.0,
                "net_amount": -3_670_000.0,
            },
        ],
    )

    text = a_stock.get_fund_flow("603809", "2026-07-15", include_history=True)

    assert text.startswith("Error") is False
    assert "主力净流入" in text
    assert "新浪" in text or "sina" in text.lower()
    assert "119" in text or "1,190,000" in text or "119万" in text
    assert "勿写报告级数据缺失标记" in text or "勿因此标注报告级数据缺失" in text
    assert "[数据缺失" not in text

def test_get_realtime_main_net_inflow_falls_back_to_sina(monkeypatch):
    from tradingagents.dataflows import a_stock

    def fake_em_get(url, params=None, headers=None, timeout=15, **kwargs):
        raise requests.exceptions.ConnectionError("aborted")

    monkeypatch.setattr(a_stock, "_em_get", fake_em_get)
    monkeypatch.setattr(
        a_stock,
        "_sina_fund_flow_history",
        lambda code, days=20: [
            {"date": "2026-07-15", "main_net": 4_360_000.0, "net_amount": 1.0},
        ],
    )

    assert a_stock.get_realtime_main_net_inflow("603809") == 4_360_000.0


def test_get_fund_flow_keeps_soft_body_when_all_sources_fail(monkeypatch):
    from tradingagents.dataflows import a_stock

    monkeypatch.setattr(a_stock, "_EM_MIN_INTERVAL", 0.0)
    monkeypatch.setattr(a_stock, "_EM_RETRY_BASE_DELAY", 0.0)
    a_stock._em_last_call[0] = 0.0

    def fake_em_get(url, params=None, headers=None, timeout=15, **kwargs):
        raise requests.exceptions.ConnectionError("aborted")

    monkeypatch.setattr(a_stock, "_em_get", fake_em_get)
    monkeypatch.setattr(
        a_stock, "_sina_fund_flow_history", lambda *a, **k: []
    )

    text = a_stock.get_fund_flow("603809", "2026-07-15", include_history=True)
    assert text.startswith("Error") is False
    assert "Historical Daily Fund Flow" in text
    assert "勿写报告级数据缺失标记" in text or "勿因此标注报告级数据缺失" in text


def test_get_fund_flow_retries_sina_after_transient_exception(monkeypatch):
    from tradingagents.dataflows import a_stock

    monkeypatch.setattr(a_stock, "_EM_MIN_INTERVAL", 0.0)
    monkeypatch.setattr(a_stock, "_EM_RETRY_BASE_DELAY", 0.0)
    a_stock._em_last_call[0] = 0.0

    def fake_em_get(url, params=None, headers=None, timeout=15, **kwargs):
        raise requests.exceptions.ConnectionError("aborted")

    calls = {"n": 0}

    def flaky_sina(code, days=20):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.exceptions.Timeout("sina timeout")
        return [
            {
                "date": "2026-07-15",
                "main_net": 2_000_000.0,
                "net_amount": 1_000_000.0,
            },
        ]

    monkeypatch.setattr(a_stock, "_em_get", fake_em_get)
    monkeypatch.setattr(a_stock, "_sina_fund_flow_history", flaky_sina)

    text = a_stock.get_fund_flow("603809", "2026-07-15", include_history=True)
    assert calls["n"] >= 2
    assert "Historical Daily Fund Flow" in text
    assert "Close: 主力净流入" in text
    assert "新浪兜底也失败" not in text
    assert "200" in text or "主力净流入≈200" in text


def test_get_fund_flow_does_not_double_close_on_truncated_last_bar(monkeypatch):
    from tradingagents.dataflows import a_stock

    monkeypatch.setattr(a_stock, "_EM_MIN_INTERVAL", 0.0)
    monkeypatch.setattr(a_stock, "_EM_RETRY_BASE_DELAY", 0.0)
    a_stock._em_last_call[0] = 0.0

    class _Rt:
        def json(self):
            return {
                "data": {
                    "klines": [
                        "2026-07-15 14:59,1000000,0,0,200000,800000",
                        "2026-07-15 15:00,broken",  # truncated last bar
                    ]
                }
            }

    def fake_em(url, params=None, headers=None, timeout=15, **kwargs):
        if "daykline" in url:
            raise requests.exceptions.SSLError("ssl")
        return _Rt()

    monkeypatch.setattr(a_stock, "_em_get", fake_em)
    monkeypatch.setattr(
        a_stock,
        "_sina_fund_flow_history",
        lambda *a, **k: [
            {
                "date": "2026-07-15",
                "main_net": 9_999_000.0,
                "net_amount": 1.0,
            },
        ],
    )

    text = a_stock.get_fund_flow("603809", "2026-07-15", include_history=True)
    assert "Realtime Minute Flow" in text
    assert "新浪 MoneyFlow (分时兜底)" not in text
    assert "Daily Fund Flow (新浪" not in text
    assert text.count("Close:") == 0
