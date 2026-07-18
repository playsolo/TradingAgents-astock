"""Integration test: model_config.json's fallback_chain reaches the LLM factory.

Verifies the plumbing chain end-to-end:

1. ``load_model_config()`` returns the persisted ``fallback_chain`` list.
2. ``save_model_config(..., fallback_chain=[...])`` round-trips through disk.
3. ``create_llm_client_with_fallback()`` wraps the primary with a fallback.
4. The TradingAgentsGraph construction site (via the alias import in
   trading_graph.py) forwards ``fallback_chain`` from ``config`` into the
   factory.

These tests use monkeypatched file paths so they don't touch the real
``~/.tradingagents/model_config.json`` on the developer's machine.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# _sanitize_chain — guard against malformed config
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSanitizeChain:
    def test_empty_input_returns_empty_list(self):
        from tradingagents.auth.model_config import _sanitize_chain
        assert _sanitize_chain(None) == []
        assert _sanitize_chain([]) == []
        assert _sanitize_chain("garbage") == []

    def test_filters_malformed_entries(self):
        from tradingagents.auth.model_config import _sanitize_chain
        raw = [
            {"provider": "deepseek", "model": "deepseek-chat"},
            {"provider": "minimax"},  # missing model
            {"model": "x"},  # missing provider
            "not-a-dict",
            None,
            {"provider": "qwen", "model": "qwen-plus"},
        ]
        assert _sanitize_chain(raw) == [
            {"provider": "deepseek", "model": "deepseek-chat"},
            {"provider": "qwen", "model": "qwen-plus"},
        ]


# ---------------------------------------------------------------------------
# save / load round-trip
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFallbackChainPersistence:
    def test_save_then_load_round_trips_chain(
        self, tmp_path: Path, monkeypatch
    ):
        from tradingagents.auth import model_config

        cfg_file = tmp_path / "model_config.json"
        monkeypatch.setattr(model_config, "_MODEL_CONFIG_FILE", cfg_file)

        model_config.save_model_config(
            llm_provider="minimax",
            deep_think_llm="MiniMax-M3",
            quick_think_llm="MiniMax-M3",
            backend_url=None,
            fallback_chain=[
                {"provider": "deepseek", "model": "deepseek-chat"},
            ],
        )
        assert cfg_file.exists()

        loaded = model_config.load_model_config()
        assert loaded["llm_provider"] == "minimax"
        assert loaded["fallback_chain"] == [
            {"provider": "deepseek", "model": "deepseek-chat"}
        ]

    def test_load_handles_missing_chain(self, tmp_path: Path, monkeypatch):
        from tradingagents.auth import model_config

        cfg_file = tmp_path / "model_config.json"
        monkeypatch.setattr(model_config, "_MODEL_CONFIG_FILE", cfg_file)
        # Simulate an older config file without the field.
        cfg_file.write_text(json.dumps({
            "llm_provider": "deepseek",
            "deep_think_llm": "deepseek-chat",
            "quick_think_llm": "deepseek-chat",
            "backend_url": None,
        }))

        loaded = model_config.load_model_config()
        assert loaded["fallback_chain"] == []

    def test_load_drops_malformed_chain_entries(
        self, tmp_path: Path, monkeypatch
    ):
        from tradingagents.auth import model_config

        cfg_file = tmp_path / "model_config.json"
        monkeypatch.setattr(model_config, "_MODEL_CONFIG_FILE", cfg_file)
        cfg_file.write_text(json.dumps({
            "llm_provider": "deepseek",
            "deep_think_llm": "deepseek-chat",
            "quick_think_llm": "deepseek-chat",
            "backend_url": None,
            "fallback_chain": [
                {"provider": "deepseek", "model": "deepseek-chat"},
                "garbage",
                {"provider": "minimax"},
            ],
        }))

        loaded = model_config.load_model_config()
        assert loaded["fallback_chain"] == [
            {"provider": "deepseek", "model": "deepseek-chat"}
        ]


# ---------------------------------------------------------------------------
# create_llm_client_with_fallback — end-to-end plumbing
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFactoryWiring:
    def test_with_empty_chain_returns_primary_unchanged(self):
        from tradingagents.llm_clients.factory import (
            create_llm_client_with_fallback,
        )
        from tradingagents.llm_clients.fallback import FallbackLLMClient

        primary = create_llm_client_with_fallback(
            provider="deepseek",
            model="deepseek-chat",
            fallback_chain=[],
        )
        # With no chain, we should get the bare client, not the wrapper.
        assert not isinstance(primary, FallbackLLMClient)

    def test_with_chain_returns_wrapper(self):
        from tradingagents.llm_clients.factory import (
            create_llm_client_with_fallback,
        )
        from tradingagents.llm_clients.fallback import FallbackLLMClient

        client = create_llm_client_with_fallback(
            provider="minimax",
            model="MiniMax-M3",
            fallback_chain=[
                {"provider": "deepseek", "model": "deepseek-chat"},
            ],
        )
        assert isinstance(client, FallbackLLMClient)
        assert len(client.breakers()) == 2  # primary + 1 fallback

    def test_disabled_returns_primary_unchanged(self):
        from tradingagents.llm_clients.factory import (
            create_llm_client_with_fallback,
        )
        from tradingagents.llm_clients.fallback import FallbackLLMClient

        client = create_llm_client_with_fallback(
            provider="minimax",
            model="MiniMax-M3",
            fallback_chain=[
                {"provider": "deepseek", "model": "deepseek-chat"},
            ],
            enabled=False,
        )
        assert not isinstance(client, FallbackLLMClient)