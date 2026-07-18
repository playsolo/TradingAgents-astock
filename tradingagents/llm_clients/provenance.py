"""Helpers for recording / displaying which LLM providers actually answered."""

from __future__ import annotations

from typing import Any, Mapping


def collect_used_providers(*clients: Any) -> list[str]:
    """Merge ordered unique providers that successfully answered.

    Falls back to each client's ``provider`` attribute when the client has no
    usage tracker (bare OpenAIClient / disabled fallback wrapper).
    """
    seen: list[str] = []
    for client in clients:
        if client is None:
            continue
        used: list[str] = []
        getter = getattr(client, "used_providers", None)
        if callable(getter):
            used = list(getter() or [])
        if not used:
            provider = getattr(client, "provider", None)
            if provider:
                used = [str(provider)]
        for label in used:
            if label and label not in seen:
                seen.append(label)
    return seen


def models_for_providers(
    providers: list[str],
    *,
    primary_provider: str | None,
    primary_model: str | None,
    fallback_chain: list[Mapping[str, str]] | None = None,
) -> list[str]:
    """Map each used provider to a model name from config / fallback chain."""
    chain_models = {
        str(entry.get("provider")): str(entry.get("model"))
        for entry in (fallback_chain or [])
        if entry.get("provider") and entry.get("model")
    }
    primary = (primary_provider or "").strip()
    primary_m = (primary_model or "").strip()
    out: list[str] = []
    for provider in providers:
        if primary and provider == primary and primary_m:
            out.append(primary_m)
        elif provider in chain_models:
            out.append(chain_models[provider])
        elif primary_m and not primary:
            out.append(primary_m)
        else:
            out.append(provider)
    return out


def provenance_fields_from_config(
    config: Mapping[str, Any],
    *,
    used_providers: list[str] | None = None,
) -> dict[str, Any]:
    """Build the llm_* provenance keys written into analysis logs."""
    provider = config.get("llm_provider")
    deep = config.get("deep_think_llm")
    quick = config.get("quick_think_llm")
    chain = list(config.get("fallback_chain") or [])
    used = list(used_providers or config.get("llm_providers_used") or [])
    if not used and provider:
        used = [str(provider)]
    models = list(config.get("llm_models_used") or [])
    if not models:
        models = models_for_providers(
            used,
            primary_provider=str(provider) if provider else None,
            primary_model=str(deep or quick or "") or None,
            fallback_chain=chain,
        )
    return {
        "llm_provider": provider,
        "deep_think_llm": deep,
        "quick_think_llm": quick,
        "llm_backend_url": config.get("backend_url"),
        "llm_fallback_chain": chain,
        "llm_providers_used": used,
        "llm_models_used": models,
    }
