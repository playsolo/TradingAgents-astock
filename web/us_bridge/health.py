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

import json
import logging
import os
import time
from pathlib import Path
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

# Cheap chat models used when ``model`` is not passed to the probe.
# ``GET /models`` alone is insufficient for MiniMax Token Plan quotas:
# listing models still returns 200 while ``chat/completions`` returns 429.
_PROBE_CHAT_MODELS: dict[str, str] = {
    "minimax": "MiniMax-M3",
    "deepseek": "deepseek-chat",
    "openai": "gpt-4o-mini",
    "qwen": "qwen-turbo",
    "glm": "glm-4-flash",
    "xai": "grok-2-latest",
    "openrouter": "openai/gpt-4o-mini",
}

# Quota / billing statuses that mean "don't send a full US analysis here".
_QUOTA_STATUS_CODES = frozenset({402, 429})

_PROBE_TIMEOUT_S = 4.0

# Look back window for recent quota failures in incomplete_tasks.json.
# A 1-token probe can pass while the real analysis exhausts remaining
# quota mid-run; this window catches those cases.
_QUOTA_FAILURE_WINDOW_S = 2700  # 45 minutes

# Path to incomplete_tasks.json (same file as web/history.py).
_INCOMPLETE_TASKS_FILE = Path.home() / ".tradingagents" / "incomplete_tasks.json"


def _has_recent_quota_failures(provider: str, *, window_s: int = _QUOTA_FAILURE_WINDOW_S) -> bool:
    """Check incomplete_tasks.json for recent 429/402 failures attributed to *provider*.

    Looks back *window_s* seconds. The error_text field is scanned for
    ``429``, ``402``, ``rate_limit``, and ``quota`` patterns. A match means
    this provider exhausted its quota on a prior real analysis and should be
    skipped in favour of the fallback.

    Returns True when *any* recent quota failure exists — conservative, but
    the cost of a false positive is just an unnecessary fallback.
    """
    if not _INCOMPLETE_TASKS_FILE.exists():
        return False
    try:
        entries = json.loads(_INCOMPLETE_TASKS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(entries, list):
        return False

    cutoff = time.time() - window_s
    for entry in entries:
        updated = entry.get("updated_at", 0)
        if not isinstance(updated, (int, float)) or updated < cutoff:
            continue
        error = str(entry.get("error") or "")
        if not error:
            continue
        # Check for quota/rate-limit indicators in the error text
        if "429" in error or "402" in error:
            return True
        err_lower = error.lower()
        if "rate_limit" in err_lower or "quota" in err_lower or "token plan" in err_lower:
            return True
    return False


def probe_provider(
    provider: str,
    *,
    api_key: str | None,
    base_url: str | None = None,
    model: str | None = None,
) -> bool:
    """Return True if ``provider`` looks healthy enough to run an analysis.

    Healthy == the configured API key is non-empty AND a quick HTTP probe
    returns 2xx. For OpenAI-compatible hosts we also fire a 1-token
    ``chat/completions`` ping: MiniMax Token Plan exhaustion still serves
    ``GET /models`` as 200 while chat returns 429 — without the chat probe
    the US bridge would spawn a doomed MiniMax subprocess.
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
    if not (200 <= resp.status_code < 300):
        return False

    # Ollama uses /api/tags, not chat/completions — models list is enough.
    if provider.lower() == "ollama":
        return True

    return _probe_chat_completions(
        provider,
        base=base,
        api_key=api_key,
        model=model,
    )


def _probe_chat_completions(
    provider: str,
    *,
    base: str,
    api_key: str,
    model: str | None,
) -> bool:
    """1-token chat ping so Token Plan / rate-limit exhaustion is visible."""
    chat_model = (model or "").strip() or _PROBE_CHAT_MODELS.get(provider.lower())
    if not chat_model:
        return True

    url = f"{base.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": chat_model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
    }
    try:
        resp = requests.post(
            url, headers=headers, json=payload, timeout=_PROBE_TIMEOUT_S
        )
    except requests.RequestException as exc:
        logger.warning(
            "chat probe %s failed: %s", provider, exc.__class__.__name__
        )
        return False

    if resp.status_code in _QUOTA_STATUS_CODES:
        logger.warning(
            "chat probe %s unhealthy: HTTP %s (quota/rate-limit)",
            provider,
            resp.status_code,
        )
        return False
    if not (200 <= resp.status_code < 300):
        logger.warning(
            "chat probe %s unhealthy: HTTP %s", provider, resp.status_code
        )
        return False
    return True


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

    # ── Probe-based health check ──────────────────────────────────
    # A 1-token probe can succeed while the account has only a few
    # tokens left — enough for the probe, not enough for a full analysis.
    # If probe passes, also check for recent 429/402 from real runs.
    probe_healthy = probe_provider(
        llm_provider,
        api_key=primary_key,
        base_url=base_url,
        model=deep_think_llm or quick_think_llm,
    )
    quota_exhausted = _has_recent_quota_failures(llm_provider)

    if probe_healthy and not quota_exhausted:
        return {
            "llm_provider": llm_provider,
            "deep_think_llm": deep_think_llm,
            "quick_think_llm": quick_think_llm,
            "fell_back": False,
        }

    if quota_exhausted:
        logger.warning(
            "primary provider %s has recent quota failures; attempting fallback chain",
            llm_provider,
        )
    else:
        logger.warning(
            "primary provider %s unhealthy; attempting fallback chain", llm_provider
        )
    for entry in fallback_chain or []:
        candidate_provider = (entry.get("provider") or "").strip()
        candidate_model = (entry.get("model") or "").strip()
        if not candidate_provider or not candidate_model:
            continue
        candidate_key = _api_key_for(candidate_provider, None)
        if probe_provider(
            candidate_provider,
            api_key=candidate_key,
            model=candidate_model,
        ):
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