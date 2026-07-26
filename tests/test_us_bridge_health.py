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

import json

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
    """A 200 from ``/models`` plus a 200 chat ping is healthy."""
    fake_resp = MagicMock(status_code=200)
    seen: dict[str, str] = {}

    def capture_get(url, *args, **kwargs):
        seen["url"] = url
        return fake_resp

    monkeypatch.setattr("web.us_bridge.health.requests.get", capture_get)
    monkeypatch.setattr(
        "web.us_bridge.health.requests.post", lambda *_a, **_k: fake_resp
    )
    assert probe_provider("minimax", api_key="sk-test", base_url="https://api.minimaxi.com/v1") is True
    assert seen["url"] == "https://api.minimaxi.com/v1/models"


def test_probe_provider_minimax_default_base_no_double_v1(monkeypatch):
    """Default MiniMax base already ends in ``/v1``; path must not add another."""
    fake_resp = MagicMock(status_code=200)
    seen: dict[str, str] = {}

    def capture(url, *args, **kwargs):
        seen["url"] = url
        return fake_resp

    monkeypatch.setattr("web.us_bridge.health.requests.get", capture)
    monkeypatch.setattr(
        "web.us_bridge.health.requests.post", lambda *_a, **_k: fake_resp
    )
    assert probe_provider("minimax", api_key="sk-test") is True
    assert seen["url"] == "https://api.minimaxi.com/v1/models"
    assert "/v1/v1/" not in seen["url"]


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


def test_probe_provider_models_ok_but_chat_429_unhealthy(monkeypatch):
    """MiniMax Token Plan exhaustion: /models=200, chat/completions=429."""
    monkeypatch.setattr(
        "web.us_bridge.health.requests.get",
        lambda *_a, **_k: MagicMock(status_code=200),
    )
    monkeypatch.setattr(
        "web.us_bridge.health.requests.post",
        lambda *_a, **_k: MagicMock(status_code=429),
    )
    assert probe_provider("minimax", api_key="sk-test") is False


def test_probe_provider_chat_ping_uses_configured_model(monkeypatch):
    """When ``model`` is passed, the chat probe must use that model name."""
    seen: dict[str, object] = {}

    def capture_post(url, *args, **kwargs):
        seen["url"] = url
        seen["json"] = kwargs.get("json")
        return MagicMock(status_code=200)

    monkeypatch.setattr(
        "web.us_bridge.health.requests.get",
        lambda *_a, **_k: MagicMock(status_code=200),
    )
    monkeypatch.setattr("web.us_bridge.health.requests.post", capture_post)
    assert (
        probe_provider(
            "minimax",
            api_key="sk-test",
            model="MiniMax-M3",
        )
        is True
    )
    assert seen["url"] == "https://api.minimaxi.com/v1/chat/completions"
    assert seen["json"]["model"] == "MiniMax-M3"
    assert seen["json"]["max_tokens"] == 1


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
        fallback_chain=[{"provider": "deepseek", "model": "deepseek-v4-flash"}],
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
            {"provider": "deepseek", "model": "deepseek-v4-flash"},
            {"provider": "openai", "model": "gpt-4o"},
        ],
    )
    assert chosen == {
        "llm_provider": "deepseek",
        "deep_think_llm": "deepseek-v4-flash",
        "quick_think_llm": "deepseek-v4-flash",
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
            {"provider": "deepseek", "model": "deepseek-v4-flash"},
            {"provider": "openai", "model": "gpt-4o"},
        ],
    )
    assert chosen["llm_provider"] == "deepseek"
    assert chosen["fell_back"] is True


# ---------------------------------------------------------------------------
# _has_recent_quota_failures — incomplete_tasks.json 429 guard
# ---------------------------------------------------------------------------


def test_has_recent_quota_failures_no_file(tmp_path):
    """Missing incomplete_tasks.json → no quota failures."""
    from web.us_bridge.health import _has_recent_quota_failures

    assert _has_recent_quota_failures("minimax") is False


def test_has_recent_quota_failures_empty(monkeypatch, tmp_path):
    """Empty file → no quota failures."""
    f = tmp_path / "incomplete_tasks.json"
    f.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(
        "web.us_bridge.health._INCOMPLETE_TASKS_FILE", f
    )
    from web.us_bridge.health import _has_recent_quota_failures

    assert _has_recent_quota_failures("minimax") is False


def test_has_recent_quota_failures_429_found(monkeypatch, tmp_path):
    """A recent entry with 429 in error → True."""
    import time

    f = tmp_path / "incomplete_tasks.json"
    f.write_text(
        json.dumps(
            [
                {
                    "ticker": "MU",
                    "trade_date": "2026-07-21",
                    "status": "error",
                    "error": "Error code: 429 - rate limit exceeded",
                    "updated_at": time.time() - 60,
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("web.us_bridge.health._INCOMPLETE_TASKS_FILE", f)
    from web.us_bridge.health import _has_recent_quota_failures

    assert _has_recent_quota_failures("minimax") is True


def test_has_recent_quota_failures_old_entry(monkeypatch, tmp_path):
    """Entry older than window → ignored."""
    f = tmp_path / "incomplete_tasks.json"
    f.write_text(
        json.dumps(
            [
                {
                    "ticker": "MU",
                    "trade_date": "2026-05-01",
                    "status": "error",
                    "error": "Error code: 429",
                    "updated_at": 1000000000,
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("web.us_bridge.health._INCOMPLETE_TASKS_FILE", f)
    from web.us_bridge.health import _has_recent_quota_failures

    assert _has_recent_quota_failures("minimax") is False


def test_has_recent_quota_failures_402_found(monkeypatch, tmp_path):
    """402 (payment required) also treated as quota failure."""
    import time

    f = tmp_path / "incomplete_tasks.json"
    f.write_text(
        json.dumps(
            [
                {
                    "ticker": "ISRG",
                    "trade_date": "2026-07-21",
                    "status": "error",
                    "error": "HTTP 402 Payment Required",
                    "updated_at": time.time() - 120,
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("web.us_bridge.health._INCOMPLETE_TASKS_FILE", f)
    from web.us_bridge.health import _has_recent_quota_failures

    assert _has_recent_quota_failures("minimax") is True


def test_has_recent_quota_failures_token_plan_text(monkeypatch, tmp_path):
    """'Token Plan' in error text (MiniMax Chinese) → True."""
    import time

    f = tmp_path / "incomplete_tasks.json"
    f.write_text(
        json.dumps(
            [
                {
                    "ticker": "MSFT",
                    "trade_date": "2026-07-21",
                    "status": "error",
                    "error": "已达到 Token Plan 用量上限",
                    "updated_at": time.time() - 30,
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("web.us_bridge.health._INCOMPLETE_TASKS_FILE", f)
    from web.us_bridge.health import _has_recent_quota_failures

    assert _has_recent_quota_failures("minimax") is True


def test_choose_provider_falls_back_on_quota_failures(monkeypatch):
    """Primary probe passes but _has_recent_quota_failures True → fall back."""
    from web.us_bridge.health import choose_provider_for_us_bridge

    monkeypatch.setattr(
        "web.us_bridge.health.probe_provider", lambda *_a, **_k: True
    )
    monkeypatch.setattr(
        "web.us_bridge.health._has_recent_quota_failures", lambda *_a, **_k: True
    )
    chosen = choose_provider_for_us_bridge(
        llm_provider="minimax",
        api_key="sk-test",
        base_url=None,
        deep_think_llm="MiniMax-M3",
        quick_think_llm="MiniMax-M3",
        fallback_chain=[{"provider": "deepseek", "model": "deepseek-v4-flash"}],
    )
    assert chosen == {
        "llm_provider": "deepseek",
        "deep_think_llm": "deepseek-v4-flash",
        "quick_think_llm": "deepseek-v4-flash",
        "fell_back": True,
    }