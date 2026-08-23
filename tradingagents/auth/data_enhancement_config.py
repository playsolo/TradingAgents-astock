"""Persisted HiThink / data-enhancement settings (admin Web UI)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_CONFIG_FILE = Path.home() / ".tradingagents" / "data_enhancement.json"


def _defaults() -> dict[str, Any]:
    return {
        "hithink_enabled": False,
        "hithink_api_key": "",
    }


def data_enhancement_config_exists() -> bool:
    return _CONFIG_FILE.exists()


def load_data_enhancement_config() -> dict[str, Any]:
    if not _CONFIG_FILE.exists():
        return _defaults()
    try:
        data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _defaults()
        out = _defaults()
        out["hithink_enabled"] = bool(data.get("hithink_enabled", False))
        key = str(data.get("hithink_api_key") or "").strip()
        out["hithink_api_key"] = key
        return out
    except (OSError, json.JSONDecodeError):
        return _defaults()


def save_data_enhancement_config(
    *,
    hithink_enabled: bool,
    hithink_api_key: str | None = None,
) -> dict[str, Any]:
    existing = load_data_enhancement_config()
    key = existing.get("hithink_api_key") or ""
    if hithink_api_key is not None:
        key = str(hithink_api_key).strip()
    payload = {
        "hithink_enabled": bool(hithink_enabled),
        "hithink_api_key": key,
    }
    _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_CONFIG_FILE)
    apply_data_enhancement_to_process(payload)
    return payload


def apply_data_enhancement_to_process(config: dict[str, Any] | None = None) -> None:
    """Mirror saved config into ``os.environ`` for the current process."""
    cfg = config or load_data_enhancement_config()
    enabled = bool(cfg.get("hithink_enabled"))
    key = str(cfg.get("hithink_api_key") or "").strip()
    os.environ["HITHINK_ENABLED"] = "true" if enabled and key else "false"
    if key:
        os.environ["HITHINK_FINANCE_API_KEY"] = key
    from tradingagents.dataflows.hithink_client import reset_hithink_client

    reset_hithink_client()
