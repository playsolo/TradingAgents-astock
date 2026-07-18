"""Pre-flight provider health check for the US TradingAgents subprocess.

The upstream TradingAgents (US) does not understand our ``fallback_chain``
config — its ``TradingAgentsGraph.__init__`` reads a single
``TRADINGAGENTS_LLM_PROVIDER`` env var and uses it unconditionally. When
the operator's primary provider has an expired / missing API key (e.g.
``minimax`` rotated), every US analysis fails with a 401 instead of
degrading to ``deepseek``.

This module provides a small compensator:

* :func:`probe_provider` does a cheap ``GET /v1/models`` (or equivalent)
  against the configured provider and returns True/False.
* :func:`choose_provider_for_us_bridge` walks the ``fallback_chain`` and
  returns the first healthy provider, plus a ``fell_back`` flag so the
  runner can surface "this US analysis actually ran on deepseek, not the
  primary minimax" in the report header.

The probe is intentionally conservative — any network error or non-2xx
counts as unhealthy. False negatives (probe says unhealthy but the
provider would have worked) just cause us to fall back unnecessarily;
they never cause a false-positive that sends a doomed request to the
upstream worker.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

import requests

logger = logging.getLogger(__name__)


# Providers we know how to probe. Anthropic and ``custom`` are skipped
# because they don't expose a models endpoint at the same path; we
# assume they're healthy and let the upstream run fail loudly if not.
# Paths are relative to ``_default_base_url`` (which already includes
# ``/v1`` / ``/v4`` for OpenAI-compatible hosts). Do NOT prefix ``/v1``
# again — that produced ``…/v1/v1/models`` (404) and falsely marked a
# healthy MiniMax key as unhealthy, forcing every US run onto deepseek.
_PROBE_ENDPOINTS: dict[str, str] = {
    "minimax": "/models",
    "deepseek": "/models",
    "openai": "/models",
    "qwen": "/models",
    "glm": "/models",
    "xai": "/models",
    "openrouter": "/models",
    "ollama": "/api/tags",
}

_PROBE_TIMEOUT_S = 4.0


def probe_provider(
    provider: str,
    *,
    api_key: str | None,
    base_url: str | None = None,
) -> bool:
    """Return True if ``provider`` looks healthy enough to run an analysis.

    Healthy == the configured API key is non-empty AND a quick HTTP probe
    returns 2xx. Anything else (network error, 4xx, 5xx) is unhealthy.
    """
    if not provider:
        return False
    if not api_key:
        return False
    endpoint = _PROBE_ENDPOINTS.get(provider.lower())
    if endpoint is None:
        # We don't know how to probe this provider — assume healthy and
        # let the upstream call decide. This keeps ``anthropic``, ``custom``,
        # ``ollama`` (when base_url is custom) working without forcing
        # operators to register a probe endpoint for every conceivable
        # backend.
        return True

    base = (base_url or _default_base_url(provider)).rstrip("/")
    url = f"{base}{endpoint}"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        resp = requests.get(url, headers=headers, timeout=_PROBE_TIMEOUT_S)
    except requests.RequestException as exc:
        logger.warning("probe %s failed: %s", provider, exc.__class__.__name__)
        return False
    return 200 <= resp.status_code < 300


def _default_base_url(provider: str) -> str:
    """Default base URL for providers we know about. Mirrors the
    ``_PROVIDER_CONFIG`` table in ``tradingagents/llm_clients/openai_client.py``
    so the probe reaches the same endpoint the real client would."""
    return {
        "minimax": "https://api.minimaxi.com/v1",
        "deepseek": "https://api.deepseek.com",
        "openai": "https://api.openai.com/v1",
        "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "glm": "https://open.bigmodel.cn/api/paas/v4",
        "xai": "https://api.x.ai/v1",
        "openrouter": "https://openrouter.ai/api/v1",
        "ollama": "http://localhost:11434",
    }.get(provider.lower(), "")


# Per-provider env var that holds the API key in the daemon's process
# env. Mirrors ``_PROVIDER_CONFIG`` from openai_client.py.
_API_KEY_ENV: dict[str, str] = {
    "minimax": "MINIMAX_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
    "qwen": "DASHSCOPE_API_KEY",
    "glm": "ZHIPU_API_KEY",
    "xai": "XAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}


def _api_key_for(provider: str, explicit_key: str | None) -> str | None:
    if explicit_key:
        return explicit_key
    import os

    var = _API_KEY_ENV.get(provider.lower())
    if not var:
        return None
    return os.environ.get(var)


def choose_provider_for_us_bridge(
    *,
    llm_provider: str,
    api_key: str | None,
    base_url: str | None,
    deep_think_llm: str,
    quick_think_llm: str,
    fallback_chain: list[Mapping[str, str]] | None,
) -> dict[str, Any]:
    """Decide which provider the US subprocess will actually use.

    Returns a dict with the (possibly overwritten) provider/model fields
    plus a ``fell_back`` boolean so callers can annotate the analysis with
    "this report was produced by deepseek because minimax was unhealthy".

    The fallback chain is walked in order; each candidate is probed the
    same way as the primary. We do NOT retry the primary — if it failed
    the probe, every subsequent call would also fail and waste 401s.
    """
    primary_key = _api_key_for(llm_provider, api_key)
    if probe_provider(llm_provider, api_key=primary_key, base_url=base_url):
        return {
            "llm_provider": llm_provider,
            "deep_think_llm": deep_think_llm,
            "quick_think_llm": quick_think_llm,
            "fell_back": False,
        }

    logger.warning(
        "primary provider %s unhealthy; attempting fallback chain", llm_provider
    )
    for entry in fallback_chain or []:
        candidate_provider = (entry.get("provider") or "").strip()
        candidate_model = (entry.get("model") or "").strip()
        if not candidate_provider or not candidate_model:
            continue
        candidate_key = _api_key_for(candidate_provider, None)
        if probe_provider(candidate_provider, api_key=candidate_key):
            logger.info(
                "US bridge fallback: %s -> %s/%s",
                llm_provider,
                candidate_provider,
                candidate_model,
            )
            return {
                "llm_provider": candidate_provider,
                "deep_think_llm": candidate_model,
                "quick_think_llm": candidate_model,
                "fell_back": True,
            }

    # No fallback worked (or none configured). Pass through the primary
    # and let the upstream run fail loudly — that's the operator's signal
    # that they need to rotate a key or fix the chain.
    return {
        "llm_provider": llm_provider,
        "deep_think_llm": deep_think_llm,
        "quick_think_llm": quick_think_llm,
        "fell_back": False,
    }