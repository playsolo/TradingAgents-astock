from typing import Iterable, Optional

from .base_client import BaseLLMClient

# Providers that use the OpenAI-compatible chat completions API.
# "openai_compatible" is the generic pass-through for any relay/gateway that
# speaks the OpenAI Chat Completions API (9Router, AI Router, self-hosted
# proxies, …): the user supplies base_url + model + a generic API key, with no
# hard-coded vendor defaults (#77 / #81).
_OPENAI_COMPATIBLE = (
    "openai", "xai", "deepseek", "qwen", "glm", "ollama", "openrouter", "minimax",
    "openai_compatible",
)


def create_llm_client(
    provider: str,
    model: str,
    base_url: Optional[str] = None,
    **kwargs,
) -> BaseLLMClient:
    """Create an LLM client for the specified provider.

    Provider modules are imported lazily so that simply importing this
    factory (e.g. during test collection) does not pull in heavy LLM SDKs
    or fail when their API keys are absent.

    Args:
        provider: LLM provider name
        model: Model name/identifier
        base_url: Optional base URL for API endpoint
        **kwargs: Additional provider-specific arguments

    Returns:
        Configured BaseLLMClient instance

    Raises:
        ValueError: If provider is not supported
    """
    provider_lower = provider.lower()

    if provider_lower in _OPENAI_COMPATIBLE:
        from .openai_client import OpenAIClient
        return OpenAIClient(model, base_url, provider=provider_lower, **kwargs)

    if provider_lower == "anthropic":
        from .anthropic_client import AnthropicClient
        return AnthropicClient(model, base_url, **kwargs)

    if provider_lower == "google":
        from .google_client import GoogleClient
        return GoogleClient(model, base_url, **kwargs)

    if provider_lower == "azure":
        from .azure_client import AzureOpenAIClient
        return AzureOpenAIClient(model, base_url, **kwargs)

    raise ValueError(f"Unsupported LLM provider: {provider}")


def create_llm_client_with_fallback(
    provider: str,
    model: str,
    fallback_chain: Optional[Iterable[dict]] = None,
    base_url: Optional[str] = None,
    enabled: bool = True,
    **kwargs,
) -> BaseLLMClient:
    """Create a client wrapped with a fallback chain.

    The primary client is constructed exactly as ``create_llm_client`` would.
    Each entry in ``fallback_chain`` (a sequence of ``{"provider", "model"}``
    dicts) becomes a fallback. When the primary client raises a quota / rate-
    limit error, the wrapper transparently retries on the next fallback. A
    per-provider circuit breaker tracks consecutive failures and short-circuits
    further calls once a threshold is crossed (default 5 failures → 5-hour
    cooldown).

    Pass ``enabled=False`` to bypass the wrapper and return the bare primary
    client — useful for tests and for operators who want to opt out without
    editing the config file.

    If ``fallback_chain`` is empty/None, returns the primary client unchanged
    (no wrapping overhead).
    """
    primary = create_llm_client(provider, model, base_url, **kwargs)
    chain = [c for c in (fallback_chain or []) if c]
    if not enabled or not chain:
        return primary

    fallback_clients = [
        create_llm_client(entry["provider"], entry["model"], base_url, **kwargs)
        for entry in chain
    ]
    from .fallback import FallbackLLMClient

    return FallbackLLMClient(
        clients=[primary, *fallback_clients],
        provider_labels=[provider, *(c.provider for c in fallback_clients)],
        enabled=enabled,
    )
