"""Persistent model configuration shared across all users.

Only admins should set this; non-admin users inherit the admin's model choices.
Stored at ``~/.tradingagents/model_config.json``.

The ``fallback_chain`` field is an ordered list of ``{"provider": ..., "model": ...}``
entries tried in sequence when the primary client raises a quota / rate-limit
error. The chain is *not* secret (no API keys live here) so it lives alongside
the rest of the model config rather than in env vars.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from tradingagents.default_config import DEFAULT_CONFIG

_MODEL_CONFIG_FILE = Path.home() / ".tradingagents" / "model_config.json"


def _defaults() -> dict[str, Any]:
    return {
        "llm_provider": "deepseek",
        "deep_think_llm": "deepseek-chat",
        "quick_think_llm": "deepseek-chat",
        "backend_url": None,
        # Default fallback chain: deepseek-chat is the always-on safety net.
        # Operators overriding ``llm_provider`` (e.g. to MiniMax) will still
        # get this fallback automatically — the circuit breaker flips to
        # DeepSeek after 5 consecutive quota errors and waits 5 hours before
        # retrying the primary.
        "fallback_chain": [
            {"provider": "deepseek", "model": "deepseek-chat"},
        ],
    }


def model_config_exists() -> bool:
    """Return True if an admin has explicitly saved a model_config.json."""
    return _MODEL_CONFIG_FILE.exists()


def load_model_config() -> dict[str, Any]:
    """Load persisted model config, or return defaults if missing / corrupt."""
    if not _MODEL_CONFIG_FILE.exists():
        return _defaults()
    try:
        data = json.loads(_MODEL_CONFIG_FILE.read_text("utf-8"))
        required = {"llm_provider", "deep_think_llm", "quick_think_llm"}
        if not required.issubset(data):
            return _defaults()
        # ``fallback_chain`` was added in a later version; older configs on
        # disk don't have the field. Treat absence / explicit ``null`` as
        # "operator hasn't opted in" and inject the project default. An
        # explicit empty list ``[]`` means "I don't want fallback" and is
        # preserved (use ``save_model_config(..., fallback_chain=[])``).
        if "fallback_chain" in data and data["fallback_chain"] is not None:
            chain = _sanitize_chain(data["fallback_chain"])
        else:
            chain = list(_defaults()["fallback_chain"])
        return {
            "llm_provider": str(data["llm_provider"]),
            "deep_think_llm": str(data["deep_think_llm"]),
            "quick_think_llm": str(data["quick_think_llm"]),
            "backend_url": data.get("backend_url") or None,
            "fallback_chain": chain,
        }
    except (json.JSONDecodeError, OSError):
        return _defaults()


def _sanitize_chain(chain_raw: Any) -> list[dict[str, str]]:
    """Coerce the persisted chain into ``[{"provider": str, "model": str}, ...]``.

    Drops malformed entries silently rather than raising — a corrupt chain
    shouldn't take the entire config down. Operators editing the JSON by hand
    will notice missing entries on the next analysis run.
    """
    if not isinstance(chain_raw, list):
        return []
    out: list[dict[str, str]] = []
    for entry in chain_raw:
        if not isinstance(entry, dict):
            continue
        provider = entry.get("provider")
        model = entry.get("model")
        if not provider or not model:
            continue
        out.append({"provider": str(provider), "model": str(model)})
    return out


def save_model_config(
    llm_provider: str,
    deep_think_llm: str,
    quick_think_llm: str,
    backend_url: str | None = None,
    fallback_chain: list[dict[str, str]] | None = None,
) -> None:
    """Persist model configuration to disk."""
    _MODEL_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "llm_provider": llm_provider,
        "deep_think_llm": deep_think_llm,
        "quick_think_llm": quick_think_llm,
        "backend_url": backend_url or None,
        "fallback_chain": _sanitize_chain(fallback_chain or []),
    }
    _MODEL_CONFIG_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
