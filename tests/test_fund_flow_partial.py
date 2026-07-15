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

    text = a_stock.get_fund_flow("002648", "2026-07-14", include_history=True)

    assert text.startswith("Error") is False
    assert "Realtime Minute Flow" in text
    assert "主力净流入" in text
    assert "历史日度暂不可用" in text
    assert "[数据缺失" not in text
