"""US bridge provider health check + automatic fallback to deepseek.

Bug D regression: previously the upstream TradingAgents (US) does not
understand our ``fallback_chain`` config. When the operator chose a primary
provider whose API key is missing/expired (e.g. ``minimax`` key rotated),
every US analysis failed with a 401 instead of degrading to ``deepseek``.

We compensate by probing the primary provider *before* spawning the US
worker subprocess: if the probe fails, swap ``TRADINGAGENTS_LLM_PROVIDER``
(plus the deep/quick model names) to the first entry in ``fallback_chain``
so the upstream graph runs against a healthy provider instead.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from web.us_bridge.health import (
    choose_provider_for_us_bridge,
    probe_provider,
)


# ---------------------------------------------------------------------------
# probe_provider — fast HEAD against the provider's base URL
# ---------------------------------------------------------------------------


def test_probe_provider_minimax_success(monkeypatch):
    """A 200 from ``/v1/models`` is healthy."""
    fake_resp = MagicMock(status_code=200)
    monkeypatch.setattr(
        "web.us_bridge.health.requests.get", lambda *_a, **_k: fake_resp
    )
    assert probe_provider("minimax", api_key="sk-test", base_url="https://api.minimaxi.com/v1") is True


def test_probe_provider_401_unhealthy(monkeypatch):
    """401 / 403 means the key is bad — not safe to run an analysis."""
    fake_resp = MagicMock(status_code=401)
    monkeypatch.setattr(
        "web.us_bridge.health.requests.get", lambda *_a, **_k: fake_resp
    )
    assert probe_provider("minimax", api_key="sk-bad") is False


def test_probe_provider_5xx_unhealthy(monkeypatch):
    """5xx is unhealthy too — better to skip than waste tokens on a doomed run."""
    fake_resp = MagicMock(status_code=503)
    monkeypatch.setattr(
        "web.us_bridge.health.requests.get", lambda *_a, **_k: fake_resp
    )
    assert probe_provider("minimax", api_key="sk-test") is False


def test_probe_provider_network_error_unhealthy(monkeypatch):
    """Connection refused / DNS failure must NOT crash — just report unhealthy."""
    import requests as req

    def boom(*_a, **_k):
        raise req.exceptions.ConnectionError("dns failed")

    monkeypatch.setattr("web.us_bridge.health.requests.get", boom)
    assert probe_provider("minimax", api_key="sk-test") is False


def test_probe_provider_missing_key_unhealthy(monkeypatch):
    """No API key configured → cannot be healthy. Skip the request entirely."""
    called = {"n": 0}

    def spy(*_a, **_k):
        called["n"] += 1
        return MagicMock(status_code=200)

    monkeypatch.setattr("web.us_bridge.health.requests.get", spy)
    assert probe_provider("minimax", api_key=None) is False
    assert probe_provider("minimax", api_key="") is False
    assert called["n"] == 0  # never even hit the network


def test_probe_provider_skips_request_for_unknown_provider(monkeypatch):
    """Providers we have no probe endpoint for (e.g. ``custom``) are
    *assumed healthy* — the cost of a failed run is the operator's to bear."""
    called = {"n": 0}

    def spy(*_a, **_k):
        called["n"] += 1
        return MagicMock(status_code=200)

    monkeypatch.setattr("web.us_bridge.health.requests.get", spy)
    assert probe_provider("anthropic", api_key="sk-test") is True
    assert called["n"] == 0


# ---------------------------------------------------------------------------
# choose_provider_for_us_bridge — primary / fallback decision
# ---------------------------------------------------------------------------


def test_choose_provider_keeps_primary_when_healthy(monkeypatch):
    """Healthy primary → pass through unchanged."""
    monkeypatch.setattr(
        "web.us_bridge.health.probe_provider", lambda *_a, **_k: True
    )
    chosen = choose_provider_for_us_bridge(
        llm_provider="minimax",
        api_key="sk-test",
        base_url=None,
        deep_think_llm="MiniMax-M3",
        quick_think_llm="MiniMax-M3",
        fallback_chain=[{"provider": "deepseek", "model": "deepseek-chat"}],
    )
    assert chosen == {
        "llm_provider": "minimax",
        "deep_think_llm": "MiniMax-M3",
        "quick_think_llm": "MiniMax-M3",
        "fell_back": False,
    }


def test_choose_provider_falls_back_when_primary_unhealthy(monkeypatch):
    """Primary unhealthy + fallback chain present → swap to first fallback."""
    def fake_probe(provider, **_kw):
        # minimax unhealthy, deepseek healthy, openai unhealthy
        return {"minimax": False, "deepseek": True, "openai": False}.get(provider, True)

    monkeypatch.setattr("web.us_bridge.health.probe_provider", fake_probe)
    chosen = choose_provider_for_us_bridge(
        llm_provider="minimax",
        api_key="sk-bad",
        base_url=None,
        deep_think_llm="MiniMax-M3",
        quick_think_llm="MiniMax-M3",
        fallback_chain=[
            {"provider": "deepseek", "model": "deepseek-chat"},
            {"provider": "openai", "model": "gpt-4o"},
        ],
    )
    assert chosen == {
        "llm_provider": "deepseek",
        "deep_think_llm": "deepseek-chat",
        "quick_think_llm": "deepseek-chat",
        "fell_back": True,
    }


def test_choose_provider_returns_primary_when_no_fallback(monkeypatch):
    """No fallback configured → return primary even if unhealthy; the
    upstream run will fail loudly so the operator knows to act."""
    monkeypatch.setattr(
        "web.us_bridge.health.probe_provider", lambda *_a, **_k: False
    )
    chosen = choose_provider_for_us_bridge(
        llm_provider="minimax",
        api_key="sk-bad",
        base_url=None,
        deep_think_llm="MiniMax-M3",
        quick_think_llm="MiniMax-M3",
        fallback_chain=[],
    )
    assert chosen["llm_provider"] == "minimax"
    assert chosen["fell_back"] is False


def test_choose_provider_skips_unhealthy_fallbacks(monkeypatch):
    """If the first fallback is also unhealthy, try the next one."""
    calls = {"count": 0}

    def probe(provider, **_kw):
        calls["count"] += 1
        # minimax unhealthy, deepseek healthy, openai unhealthy
        return {"minimax": False, "deepseek": True, "openai": False}.get(provider, True)

    monkeypatch.setattr("web.us_bridge.health.probe_provider", probe)
    chosen = choose_provider_for_us_bridge(
        llm_provider="minimax",
        api_key="sk-bad",
        base_url=None,
        deep_think_llm="MiniMax-M3",
        quick_think_llm="MiniMax-M3",
        fallback_chain=[
            {"provider": "deepseek", "model": "deepseek-chat"},
            {"provider": "openai", "model": "gpt-4o"},
        ],
    )
    assert chosen["llm_provider"] == "deepseek"
    assert chosen["fell_back"] is True