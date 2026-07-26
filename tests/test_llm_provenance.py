"""Helpers that map LLM clients → actual providers used for report provenance."""

from __future__ import annotations

from tradingagents.llm_clients.fallback import FallbackLLMClient, QuotaLikeError
from tradingagents.llm_clients.provenance import (
    collect_used_providers,
    models_for_providers,
    provenance_fields_from_config,
)


class _FakeClient:
    def __init__(self, label: str) -> None:
        self.provider = label
        self._calls: list = []
        self.scripted: list = []

    def get_llm(self):
        return self

    def invoke(self, *_a, **_k):
        self._calls.append("invoke")
        item = self.scripted.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_collect_used_providers_from_fallback_mid_run_switch():
    primary = _FakeClient("minimax")
    secondary = _FakeClient("deepseek")
    primary.scripted = ["ok", QuotaLikeError("429")]
    secondary.scripted = ["fallback-ok"]

    client = FallbackLLMClient([primary, secondary])
    llm = client.get_llm()
    assert llm.invoke("a") == "ok"
    assert llm.invoke("b") == "fallback-ok"
    assert client.used_providers() == ["minimax", "deepseek"]
    assert collect_used_providers(client) == ["minimax", "deepseek"]


def test_collect_used_providers_bare_client_uses_provider_attr():
    bare = _FakeClient("minimax")
    assert collect_used_providers(bare) == ["minimax"]


def test_models_for_providers_maps_primary_and_chain():
    models = models_for_providers(
        ["minimax", "deepseek"],
        primary_provider="minimax",
        primary_model="MiniMax-M3",
        fallback_chain=[{"provider": "deepseek", "model": "deepseek-v4-flash"}],
    )
    assert models == ["MiniMax-M3", "deepseek-v4-flash"]


def test_provenance_fields_default_used_to_configured_primary():
    fields = provenance_fields_from_config(
        {
            "llm_provider": "minimax",
            "deep_think_llm": "MiniMax-M3",
            "quick_think_llm": "MiniMax-M3",
            "backend_url": None,
            "fallback_chain": [{"provider": "deepseek", "model": "deepseek-v4-flash"}],
        }
    )
    assert fields["llm_providers_used"] == ["minimax"]
    assert fields["llm_models_used"] == ["MiniMax-M3"]
    assert fields["llm_fallback_chain"] == [
        {"provider": "deepseek", "model": "deepseek-v4-flash"}
    ]
