"""Persistent model configuration shared across all users.

Only admins should set this; non-admin users inherit the admin's model choices.
Stored at ``~/.tradingagents/model_config.json``.
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
        return {
            "llm_provider": str(data["llm_provider"]),
            "deep_think_llm": str(data["deep_think_llm"]),
            "quick_think_llm": str(data["quick_think_llm"]),
            "backend_url": data.get("backend_url") or None,
        }
    except (json.JSONDecodeError, OSError):
        return _defaults()


def save_model_config(
    llm_provider: str,
    deep_think_llm: str,
    quick_think_llm: str,
    backend_url: str | None = None,
) -> None:
    """Persist model configuration to disk."""
    _MODEL_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "llm_provider": llm_provider,
        "deep_think_llm": deep_think_llm,
        "quick_think_llm": quick_think_llm,
        "backend_url": backend_url or None,
    }
    _MODEL_CONFIG_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
