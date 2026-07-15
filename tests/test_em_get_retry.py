"""Eastmoney _em_get should retry transient SSL/connection failures."""

from __future__ import annotations

import requests


def test_em_get_retries_ssl_error_then_succeeds(monkeypatch):
    from tradingagents.dataflows import a_stock

    monkeypatch.setattr(a_stock, "_EM_MIN_INTERVAL", 0.0)
    monkeypatch.setattr(a_stock, "_EM_RETRY_BASE_DELAY", 0.0)
    a_stock._em_last_call[0] = 0.0

    calls = {"n": 0}

    class _Ok:
        status_code = 200

    def fake_get(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise requests.exceptions.SSLError("UNEXPECTED_EOF_WHILE_READING")
        return _Ok()

    monkeypatch.setattr(a_stock._EM_SESSION, "get", fake_get)

    resp = a_stock._em_get("https://push2.eastmoney.com/api/qt/stock/get")
    assert resp.status_code == 200
    assert calls["n"] == 3


def test_em_get_raises_after_retries_exhausted(monkeypatch):
    from tradingagents.dataflows import a_stock

    monkeypatch.setattr(a_stock, "_EM_MIN_INTERVAL", 0.0)
    monkeypatch.setattr(a_stock, "_EM_RETRY_BASE_DELAY", 0.0)
    a_stock._em_last_call[0] = 0.0

    def always_fail(*args, **kwargs):
        raise requests.exceptions.ConnectionError("boom")

    monkeypatch.setattr(a_stock._EM_SESSION, "get", always_fail)

    try:
        a_stock._em_get("https://push2.eastmoney.com/api/qt/stock/get")
        raise AssertionError("expected ConnectionError")
    except requests.exceptions.ConnectionError as exc:
        assert "boom" in str(exc)
