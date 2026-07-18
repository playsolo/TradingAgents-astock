"""Provider-level fallback with per-provider circuit breaker.

Design:
- ``FallbackLLMClient`` wraps a list of ``BaseLLMClient`` and proxies
  ``invoke``/``stream``/``bind_tools``/``with_structured_output`` to whichever
  one responds first.
- Only quota-style errors trigger a fallback attempt (``classify_error`` is
  the single source of truth). Non-quota errors propagate immediately so real
  bugs aren't masked.
- A ``CircuitBreaker`` tracks consecutive failures per provider and, once a
  threshold is crossed, short-circuits further calls for a cooldown window.
  The breaker is keyed by provider name so MiniMax going down doesn't put
  DeepSeek into cooldown.
- The default thresholds (5 failures → 5-hour cooldown) match the design
  discussion: MiniMax refreshes its 5-hour subscription roughly that often, so
  we wait one full window before retrying. Override per-provider via
  ``breaker_config``.

This module is deliberately small and framework-agnostic — it doesn't import
Streamlit, langchain-openai, or any vendor SDK, so it's trivial to test.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


class QuotaLikeError(Exception):
    """Marker exception for quota / billing exhaustion we explicitly want to
    fall back on. Raised by the wrapper itself if a caller needs it; tests
    use it to simulate provider quota exhaustion."""


# Match common shapes we see in production HTTP errors. Keep this conservative
# — when in doubt, *don't* classify an error as quota-related, because
# silently falling back hides real bugs (bad prompt, schema mismatch, etc.).
_QUOTA_PATTERNS = (
    re.compile(r"\b429\b"),
    re.compile(r"\b402\b"),
    re.compile(r"\bquota\b", re.IGNORECASE),
    re.compile(r"\brate[_ ]?limit\b", re.IGNORECASE),
    re.compile(r"\bbilling\b", re.IGNORECASE),
    re.compile(r"\binsufficient[_ ]?(?:balance|quota|credit)", re.IGNORECASE),
    re.compile(r"\bexceeded\b.*\b(quota|limit|budget)\b", re.IGNORECASE),
    re.compile(r"\bpayment[_ ]?required\b", re.IGNORECASE),
    re.compile(r"\btokens?[_ ]?per[_ ]?(?:min|minute)\b", re.IGNORECASE),
)

# Network-level timeouts / resets are also worth retrying on the next provider
# — a transient blip in one provider shouldn't take down the whole pipeline.
_TIMEOUT_PATTERNS = (
    re.compile(r"\btimed?\s*out\b", re.IGNORECASE),
    re.compile(r"\bdeadline[_ ]?exceeded\b", re.IGNORECASE),
    re.compile(r"\bconnection[_ ]?(?:reset|refused|closed)\b", re.IGNORECASE),
)


def classify_error(exc: BaseException) -> bool:
    """Return True if ``exc`` looks like a quota / rate-limit / timeout that
    justifies falling back to the next provider.

    Only stringified message matching is used — we don't introspect exception
    types because vendor SDKs wrap their errors inconsistently (openai raises
    ``RateLimitError``; deepseek raises ``APIError`` with a 429 string; some
    others just return ``RuntimeError("HTTP 429")``).
    """
    if isinstance(exc, QuotaLikeError):
        return True
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True

    msg = str(exc) or ""
    if not msg:
        return False

    for pattern in _QUOTA_PATTERNS:
        if pattern.search(msg):
            return True
    for pattern in _TIMEOUT_PATTERNS:
        if pattern.search(msg):
            return True
    return False


# ---------------------------------------------------------------------------
# Circuit breaker — per provider, independent
# ---------------------------------------------------------------------------


@dataclass
class CircuitBreaker:
    """Track consecutive failures for one provider; open the circuit once the
    threshold is crossed, then close it again after the cooldown elapses.

    The breaker is intentionally lightweight: just a counter and a deadline.
    No background thread, no half-open probing state. Half-open behaviour is
    provided by ``is_open()`` returning False once the cooldown elapses — the
    next call will try the provider again, and either succeed (resetting the
    failure count) or fail (re-opening the circuit for another cooldown).
    """

    provider: str
    failure_threshold: int = 5
    cooldown_seconds: int = 5 * 3600

    _consecutive_failures: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    @classmethod
    def from_config(
        cls,
        provider: str,
        config: dict[str, dict[str, int]],
        defaults: dict[str, int] | None = None,
    ) -> "CircuitBreaker":
        """Build a breaker from the per-provider config dict.

        ``config`` shape: ``{provider_name: {"failure_threshold": N, "cooldown_seconds": S}}``.
        Missing providers fall back to ``defaults`` (then to module defaults).
        """
        spec = config.get(provider) or {}
        merged = dict(defaults or {})
        merged.update(spec)
        return cls(
            provider=provider,
            failure_threshold=int(merged.get("failure_threshold", 5)),
            cooldown_seconds=int(merged.get("cooldown_seconds", 5 * 3600)),
        )

    def record_failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.failure_threshold:
                self._opened_at = time.time()
                logger.warning(
                    "circuit breaker opened for provider %r after %d consecutive "
                    "failures; cooldown %ds",
                    self.provider,
                    self._consecutive_failures,
                    self.cooldown_seconds,
                )

    def record_success(self) -> None:
        with self._lock:
            if self._consecutive_failures or self._opened_at is not None:
                logger.info(
                    "circuit breaker reset for provider %r after success",
                    self.provider,
                )
            self._consecutive_failures = 0
            self._opened_at = None

    def is_open(self) -> bool:
        """Return True if calls should be short-circuited.

        Auto-closes once the cooldown elapses; the caller will then try the
        provider again on the next call.
        """
        with self._lock:
            if self._opened_at is None:
                return False
            if time.time() - self._opened_at >= self.cooldown_seconds:
                # Cooldown elapsed — let the next call try. Don't reset the
                # counter here; let the next record_success() do it so a
                # half-open probe that fails re-opens cleanly.
                self._opened_at = None
                return False
            return True


def default_breaker_config() -> dict[str, int]:
    """Return the project-wide default breaker thresholds."""
    return {
        "failure_threshold": 5,
        "cooldown_seconds": 5 * 3600,
    }


# ---------------------------------------------------------------------------
# FallbackLLMClient — the user-facing wrapper
# ---------------------------------------------------------------------------


class FallbackLLMClient:
    """A list of ``BaseLLMClient`` instances tried in order.

    Callers get back a transparent proxy LLM (the result of the primary
    ``get_llm()``), whose ``invoke``/``stream``/``bind_tools``/
    ``with_structured_output`` are intercepted to attempt fallback on quota
    errors. Non-quota errors propagate untouched.

    The wrapper itself doesn't depend on langchain — the underlying clients do
    the vendor-specific work. This keeps the fallback logic testable with a
    plain ``FakeLLM``.
    """

    def __init__(
        self,
        clients: list[Any],
        breaker_config: dict[str, dict[str, int]] | None = None,
        enabled: bool = True,
        provider_labels: list[str] | None = None,
    ) -> None:
        if not clients:
            raise ValueError("FallbackLLMClient requires at least one client")
        self._clients = list(clients)
        self._enabled = enabled
        # Provider labels are derived from clients when not supplied so the
        # breaker keys line up with the catalog ("minimax", "deepseek", ...).
        self._provider_labels = list(
            provider_labels
            or [self._label_for(c, idx) for idx, c in enumerate(self._clients)]
        )
        self._breakers = {
            label: CircuitBreaker.from_config(
                label, breaker_config or {}, defaults=default_breaker_config()
            )
            for label in self._provider_labels
        }
        # Ordered unique providers that returned a successful response.
        # Surfaced in report provenance as "actual models used".
        self._used_providers: list[str] = []
        self._used_lock = threading.Lock()

    @staticmethod
    def _label_for(client: Any, idx: int) -> str:
        """Best-effort provider name from a BaseLLMClient."""
        for attr in ("provider", "name", "label"):
            value = getattr(client, attr, None)
            if value:
                return str(value)
        return f"client-{idx}"

    def _record_used(self, label: str) -> None:
        if not label:
            return
        with self._used_lock:
            if label not in self._used_providers:
                self._used_providers.append(label)

    def used_providers(self) -> list[str]:
        """Providers that successfully answered at least one call, in order."""
        with self._used_lock:
            return list(self._used_providers)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def get_llm(self) -> Any:
        """Return a proxy LLM that runs the fallback chain on every call."""
        primary = self._clients[0].get_llm()
        if not self._enabled or len(self._clients) == 1:
            return primary

        proxy = _FallbackLLMProxy(
            llm_getters=[c.get_llm for c in self._clients],
            labels=self._provider_labels,
            breakers=self._breakers,
            primary=primary,
            on_success=self._record_used,
        )
        return proxy

    # Read-only accessors used by tests / observability surfaces.
    def breakers(self) -> dict[str, CircuitBreaker]:
        return dict(self._breakers)

    def status(self) -> dict[str, dict[str, Any]]:
        return {
            label: {
                "open": breaker.is_open(),
                "consecutive_failures": breaker._consecutive_failures,
                "opened_at": breaker._opened_at,
            }
            for label, breaker in self._breakers.items()
        }


class _FallbackLLMProxy:
    """Proxy that re-resolves the underlying LLM on each call so that
    ``with_structured_output`` / ``bind_tools`` chains compose correctly with
    the fallback wrapper.

    The proxy keeps a reference to the *primary* LLM so that
    ``isinstance(...)`` checks and attribute lookups that callers may do
    (e.g. ``llm.model_name``) see the primary's identity.
    """

    def __init__(
        self,
        llm_getters: list[Any],
        labels: list[str],
        breakers: dict[str, CircuitBreaker],
        primary: Any,
        on_success: Any | None = None,
    ) -> None:
        self._llm_getters = llm_getters
        self._labels = labels
        self._breakers = breakers
        self._primary = primary
        self._on_success = on_success

    # Forward attribute access on the proxy to the primary LLM so that callers
    # inspecting ``llm.model_name`` / ``llm.temperature`` see sensible values.
    def __getattr__(self, name: str) -> Any:
        return getattr(self._primary, name)

    def _dispatch(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        errors: list[str] = []

        for idx, getter in enumerate(self._llm_getters):
            label = self._labels[idx]
            breaker = self._breakers.get(label)
            if breaker is not None and breaker.is_open():
                logger.info(
                    "skipping provider %r — circuit breaker open", label
                )
                continue

            llm = getter()
            method = getattr(llm, method_name)
            try:
                result = method(*args, **kwargs)
                if breaker is not None:
                    breaker.record_success()
                if self._on_success is not None:
                    self._on_success(label)
                if idx > 0:
                    logger.info(
                        "fallback succeeded on provider %r (after %d prior failure(s))",
                        label,
                        idx,
                    )
                return result
            except Exception as exc:  # noqa: BLE001 — classifier decides
                if breaker is not None:
                    breaker.record_failure()
                if not classify_error(exc):
                    # Real bug — don't try the next provider.
                    raise
                logger.warning(
                    "provider %r failed with quota-like error: %s",
                    label,
                    exc,
                )
                errors.append(f"{label}: {exc}")

        raise RuntimeError(
            "all LLM providers failed: " + "; ".join(errors)
            if errors
            else "all LLM providers skipped (circuit breakers open)"
        )

    # ---- proxied methods ---------------------------------------------------

    def invoke(self, input_, config=None, **kwargs):
        return self._dispatch("invoke", input_, config, **kwargs)

    def stream(self, input_, config=None, **kwargs):
        """Stream mode bypasses the fallback chain by design.

        LangChain stream consumers iterate the returned generator and may
        already have done partial UI work on the first chunks. Silently
        switching providers mid-stream would leave the user with a Frankenstein
        response (first half from MiniMax, second half from DeepSeek) which
        is worse than a clean error. Callers that want fallback for long
        generations should use ``invoke`` and chunk locally.
        """
        return self._primary.stream(input_, config, **kwargs)

    def batch(self, inputs, config=None, **kwargs):
        return self._dispatch("batch", inputs, config, **kwargs)

    def bind_tools(self, tools, **kwargs):
        """Return a new proxy with the same fallback chain but tool bindings
        applied on every underlying LLM."""
        bound_getters = [
            (lambda g=getter, t=tools, k=kwargs: g().bind_tools(t, **k))
            for getter in self._llm_getters
        ]
        return _FallbackLLMProxy(
            llm_getters=bound_getters,
            labels=self._labels,
            breakers=self._breakers,
            primary=self._primary.bind_tools(tools, **kwargs),
            on_success=self._on_success,
        )

    def with_structured_output(self, schema, **kwargs):
        """Return a new proxy with structured-output binding applied on every
        underlying LLM. We resolve structured output lazily on each call so a
        fallback's underlying LLM also receives the binding."""
        bound_getters = [
            (
                lambda g=getter, s=schema, k=kwargs: g().with_structured_output(
                    s, **k
                )
            )
            for getter in self._llm_getters
        ]
        return _FallbackLLMProxy(
            llm_getters=bound_getters,
            labels=self._labels,
            breakers=self._breakers,
            primary=self._primary.with_structured_output(schema, **kwargs),
            on_success=self._on_success,
        )


# ---------------------------------------------------------------------------
# Convenience constructor used by the agent factories.
# ---------------------------------------------------------------------------


def build_fallback_client(
    primary_client: Any,
    fallback_clients: Iterable[Any] = (),
    enabled: bool = True,
) -> Any:
    """Wrap ``primary_client`` with ``fallback_clients`` as the fallback chain.

    Returns ``primary_client`` unchanged when there are no fallbacks or
    fallback is disabled, so callers can use the result transparently.
    """
    fallbacks = [c for c in fallback_clients if c is not None]
    if not enabled or not fallbacks:
        return primary_client
    labels = [getattr(c, "provider", None) or _label(c, "fallback") for c in fallbacks]
    primary_label = getattr(primary_client, "provider", None) or "primary"
    return FallbackLLMClient(
        clients=[primary_client, *fallbacks],
        provider_labels=[primary_label, *labels],
        enabled=enabled,
    )


def _label(client: Any, default: str) -> str:
    for attr in ("provider", "name", "label"):
        value = getattr(client, attr, None)
        if value:
            return str(value)
    return default