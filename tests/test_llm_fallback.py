"""Tests for provider-level fallback (MiniMax → DeepSeek).

Covers:
1. Primary succeeds → fallback never invoked
2. Primary raises quota-style error (429 / 402 / RateLimitError / TimeoutError)
   → fallback is invoked transparently
3. Primary raises non-quota error (ValueError, RuntimeError) → NOT swallowed,
   caller sees the real error (avoid masking bugs)
4. Both primary and fallback fail → AggregateError-like exception carrying
   both messages so the operator can debug
5. Cooldown: 5 consecutive failures mark the primary as degraded; further
   calls within the cooldown window skip the primary entirely
6. Cooldown expiry: once the window passes, the primary is retried again
7. Different providers have independent cooldowns (MiniMax failing doesn't
   put DeepSeek into cooldown)
8. Per-provider thresholds / cooldown durations can be customised via the
   circuit-breaker config so future providers can tune their own numbers
9. Stream mode forwards chunks from whichever provider actually responded
10. with_structured_output routes through the same fallback chain

The tests use a fake LLM client (not a real ChatOpenAI) so they stay fast and
deterministic.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from tradingagents.llm_clients.fallback import (
    CircuitBreaker,
    FallbackLLMClient,
    QuotaLikeError,
    classify_error,
    default_breaker_config,
)


# ---------------------------------------------------------------------------
# Test helpers — a controllable fake LLM that the fallback wrapper can drive.
# ---------------------------------------------------------------------------


class FakeLLM:
    """Mimics langchain's ChatOpenAI surface used by TradingAgents agents.

    Records every invoke call so tests can assert on the sequence, and lets
    each test preset the next response or exception.
    """

    def __init__(self, label: str) -> None:
        self.label = label
        self.calls = 0
        self.scripted: list[Any] = []  # each entry: str (return) or Exception

    def invoke(self, input_, config=None, **kwargs):
        self.calls += 1
        return self._next()

    def _next(self):
        if not self.scripted:
            raise AssertionError(
                f"FakeLLM[{self.label}] ran out of scripted responses "
                f"after {self.calls} call(s)"
            )
        item = self.scripted.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def stream(self, input_, config=None, **kwargs):
        self.calls += 1
        item = self._next()
        if isinstance(item, Exception):
            raise item
        yield item

    def bind_tools(self, tools):
        bound = MagicMock()
        bound.invoke = self.invoke
        return bound

    def with_structured_output(self, schema, **kwargs):
        return self


class _FakeClient:
    """Wraps a FakeLLM in the BaseLLMClient shape FallbackLLMClient expects."""

    def __init__(self, label: str) -> None:
        self._llm = FakeLLM(label)
        self.label = label
        # FallbackLLMClient keys breakers off ``provider``; expose it so the
        # breaker_config in tests can target the right name.
        self.provider = label

    def get_llm(self) -> Any:
        return self._llm


# ---------------------------------------------------------------------------
# classify_error — single source of truth for what counts as a quota failure
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestClassifyError:
    @pytest.mark.parametrize(
        "exc",
        [
            QuotaLikeError("quota"),
            RuntimeError("HTTP 429 rate limit"),
            RuntimeError("Error code: 402 - Payment required"),
            TimeoutError("upstream timed out"),
            ConnectionError("reset"),
            # 422 / content-safety errors — different providers have different
            # safety thresholds; MiniMax may reject what DeepSeek accepts.
            RuntimeError(
                "Error code: 422 - {'error': {'type': 'unprocessable_entity_error', "
                "'message': 'input new_sensitive (1026)'}}"
            ),
            RuntimeError("HTTP 422 Unprocessable Entity"),
            RuntimeError("content_moderation triggered"),
        ],
    )
    def test_quota_like_errors_are_fallback_eligible(self, exc):
        assert classify_error(exc) is True

    @pytest.mark.parametrize(
        "exc",
        [
            ValueError("ticker not found"),
            RuntimeError("validation failed: schema mismatch"),
            KeyError("missing_key"),
            TypeError("bad arg"),
        ],
    )
    def test_non_quota_errors_are_not_fallback_eligible(self, exc):
        assert classify_error(exc) is False


# ---------------------------------------------------------------------------
# FallbackLLMClient — happy paths
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFallbackHappyPath:
    def test_primary_success_skips_fallback(self):
        primary = _FakeClient("primary")
        secondary = _FakeClient("secondary")
        primary._llm.scripted = ["primary-result"]

        client = FallbackLLMClient([primary, secondary])
        result = client.get_llm().invoke("hi")

        assert result == "primary-result"
        assert primary._llm.calls == 1
        assert secondary._llm.calls == 0

    def test_secondary_only_when_primary_raises_quota(self):
        primary = _FakeClient("primary")
        secondary = _FakeClient("secondary")
        primary._llm.scripted = [RuntimeError("HTTP 429 rate limit")]
        secondary._llm.scripted = ["secondary-result"]

        client = FallbackLLMClient([primary, secondary])
        result = client.get_llm().invoke("hi")

        assert result == "secondary-result"
        assert primary._llm.calls == 1
        assert secondary._llm.calls == 1


# ---------------------------------------------------------------------------
# FallbackLLMClient — non-quota errors must propagate (don't mask real bugs)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFallbackNonQuotaErrors:
    def test_value_error_is_not_swallowed(self):
        primary = _FakeClient("primary")
        secondary = _FakeClient("secondary")
        primary._llm.scripted = [ValueError("ticker 'foo' not found")]
        secondary._llm.scripted = ["should-not-be-used"]

        client = FallbackLLMClient([primary, secondary])

        with pytest.raises(ValueError, match="ticker 'foo' not found"):
            client.get_llm().invoke("hi")
        assert secondary._llm.calls == 0

    def test_runtime_validation_error_is_not_swallowed(self):
        primary = _FakeClient("primary")
        secondary = _FakeClient("secondary")
        primary._llm.scripted = [RuntimeError("schema validation failed")]
        secondary._llm.scripted = ["should-not-be-used"]

        client = FallbackLLMClient([primary, secondary])

        with pytest.raises(RuntimeError, match="schema validation failed"):
            client.get_llm().invoke("hi")


# ---------------------------------------------------------------------------
# FallbackLLMClient — both providers fail
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFallbackBothFailed:
    def test_aggregated_error_carries_both_messages(self):
        primary = _FakeClient("primary")
        secondary = _FakeClient("secondary")
        tertiary = _FakeClient("tertiary")
        primary._llm.scripted = [RuntimeError("HTTP 429 rate limit")]
        secondary._llm.scripted = [TimeoutError("upstream timed out")]
        tertiary._llm.scripted = [QuotaLikeError("quota exhausted")]

        client = FallbackLLMClient([primary, secondary, tertiary])

        with pytest.raises(RuntimeError) as excinfo:
            client.get_llm().invoke("hi")
        msg = str(excinfo.value)
        assert "primary: HTTP 429 rate limit" in msg
        assert "secondary: upstream timed out" in msg
        assert "tertiary: quota exhausted" in msg


# ---------------------------------------------------------------------------
# CircuitBreaker — cooldown for each provider independently
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCircuitBreakerCooldown:
    def test_default_thresholds_match_design(self):
        cfg = default_breaker_config()
        assert cfg["failure_threshold"] == 5
        assert cfg["cooldown_seconds"] == 5 * 3600  # 5 hours

    def test_5_consecutive_failures_open_circuit(self, monkeypatch):
        # Pin time so we can assert "still in cooldown" without sleeping.
        fake_now = [1000.0]
        monkeypatch.setattr(
            "tradingagents.llm_clients.fallback.time.time",
            lambda: fake_now[0],
        )

        breaker = CircuitBreaker(
            "minimax",
            failure_threshold=5,
            cooldown_seconds=5 * 3600,
        )
        for _ in range(5):
            breaker.record_failure()

        assert breaker.is_open() is True

    def test_open_circuit_skips_provider(self, monkeypatch):
        fake_now = [1000.0]
        monkeypatch.setattr(
            "tradingagents.llm_clients.fallback.time.time",
            lambda: fake_now[0],
        )

        breaker = CircuitBreaker("minimax", failure_threshold=2, cooldown_seconds=300)
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.is_open() is True

    def test_circuit_closes_after_cooldown(self, monkeypatch):
        fake_now = [1000.0]
        monkeypatch.setattr(
            "tradingagents.llm_clients.fallback.time.time",
            lambda: fake_now[0],
        )

        breaker = CircuitBreaker("minimax", failure_threshold=2, cooldown_seconds=60)
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.is_open() is True

        fake_now[0] += 61
        assert breaker.is_open() is False

    def test_success_resets_failure_count(self, monkeypatch):
        fake_now = [1000.0]
        monkeypatch.setattr(
            "tradingagents.llm_clients.fallback.time.time",
            lambda: fake_now[0],
        )

        breaker = CircuitBreaker("minimax", failure_threshold=5, cooldown_seconds=300)
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_success()
        breaker.record_failure()
        breaker.record_failure()
        # Only 2 consecutive failures since the success — not yet open.
        assert breaker.is_open() is False

    def test_independent_breakers_per_provider(self, monkeypatch):
        fake_now = [1000.0]
        monkeypatch.setattr(
            "tradingagents.llm_clients.fallback.time.time",
            lambda: fake_now[0],
        )

        minimax = CircuitBreaker("minimax", failure_threshold=2, cooldown_seconds=300)
        deepseek = CircuitBreaker("deepseek", failure_threshold=2, cooldown_seconds=300)
        minimax.record_failure()
        minimax.record_failure()
        assert minimax.is_open() is True
        assert deepseek.is_open() is False


# ---------------------------------------------------------------------------
# FallbackLLMClient + CircuitBreaker integration
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFallbackWithCircuitBreaker:
    def test_open_circuit_skips_primary(self, monkeypatch):
        fake_now = [1000.0]
        monkeypatch.setattr(
            "tradingagents.llm_clients.fallback.time.time",
            lambda: fake_now[0],
        )

        primary = _FakeClient("primary")
        secondary = _FakeClient("secondary")
        secondary._llm.scripted = ["ok"]

        client = FallbackLLMClient(
            [primary, secondary],
            breaker_config={"primary": {"failure_threshold": 1, "cooldown_seconds": 3600}},
        )

        # Trip the breaker on the primary.
        primary._llm.scripted = [RuntimeError("HTTP 429")]
        secondary._llm.scripted = ["ok-1", "ok-2"]
        client.get_llm().invoke("hi")  # uses secondary, primary breaker tripped
        assert primary._llm.calls == 1
        assert secondary._llm.calls == 1

        # Next call within cooldown must NOT touch primary.
        client.get_llm().invoke("hi")
        assert primary._llm.calls == 1
        assert secondary._llm.calls == 2

    def test_breaker_resets_on_primary_success(self, monkeypatch):
        fake_now = [1000.0]
        monkeypatch.setattr(
            "tradingagents.llm_clients.fallback.time.time",
            lambda: fake_now[0],
        )

        primary = _FakeClient("primary")
        secondary = _FakeClient("secondary")
        primary._llm.scripted = ["p1", "p2", "p3", "p4", "p5"]
        secondary._llm.scripted = []  # never used

        client = FallbackLLMClient([primary, secondary])

        for _ in range(5):
            client.get_llm().invoke("hi")

        # 5 successful primary calls in a row — breaker should be closed/healthy.
        assert primary._llm.calls == 5
        assert secondary._llm.calls == 0


# ---------------------------------------------------------------------------
# Stream mode + structured output
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFallbackStreamAndStructured:
    def test_stream_propagates_primary_error_without_fallback(self):
        """Stream mode is intentionally not fallback-wrapped: once the primary
        starts emitting, a mid-stream switch would leave callers with a
        half-response from one model and a half-response from another. We
        let primary stream errors propagate so callers see the real failure.
        """
        primary = _FakeClient("primary")
        secondary = _FakeClient("secondary")
        primary._llm.scripted = [RuntimeError("HTTP 429")]
        secondary._llm.scripted = ["chunk-a", "chunk-b"]

        client = FallbackLLMClient([primary, secondary])

        with pytest.raises(RuntimeError, match="HTTP 429"):
            list(client.get_llm().stream("hi"))
        # Secondary must NOT be touched in stream mode.
        assert secondary._llm.calls == 0

    def test_with_structured_output_falls_back(self):
        primary = _FakeClient("primary")
        secondary = _FakeClient("secondary")
        primary._llm.scripted = [QuotaLikeError("quota")]
        secondary._llm.scripted = ["structured-result"]

        client = FallbackLLMClient([primary, secondary])
        result = client.get_llm().with_structured_output(schema=None).invoke("hi")

        assert result == "structured-result"
        assert primary._llm.calls == 1
        assert secondary._llm.calls == 1


# ---------------------------------------------------------------------------
# Configuration knobs
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFallbackConfig:
    def test_threshold_and_cooldown_per_provider(self, monkeypatch):
        fake_now = [1000.0]
        monkeypatch.setattr(
            "tradingagents.llm_clients.fallback.time.time",
            lambda: fake_now[0],
        )

        # MiniMax: 5 failures / 5h. DeepSeek: 3 failures / 1h.
        cfg = {
            "minimax": {"failure_threshold": 5, "cooldown_seconds": 5 * 3600},
            "deepseek": {"failure_threshold": 3, "cooldown_seconds": 3600},
        }
        breaker = CircuitBreaker.from_config("minimax", cfg)
        assert breaker.failure_threshold == 5
        assert breaker.cooldown_seconds == 18000

        breaker2 = CircuitBreaker.from_config("deepseek", cfg)
        assert breaker2.failure_threshold == 3
        assert breaker2.cooldown_seconds == 3600

    def test_fallback_can_be_disabled_via_kwarg(self, monkeypatch):
        primary = _FakeClient("primary")
        secondary = _FakeClient("secondary")
        primary._llm.scripted = [RuntimeError("HTTP 429")]
        secondary._llm.scripted = ["should-not-be-used"]

        client = FallbackLLMClient([primary, secondary], enabled=False)

        with pytest.raises(RuntimeError, match="HTTP 429"):
            client.get_llm().invoke("hi")
        assert secondary._llm.calls == 0


# ---------------------------------------------------------------------------
# Observability — the operator must be able to see fallback activity
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFallbackObservability:
    def test_event_log_records_fallback(self, caplog):
        primary = _FakeClient("primary")
        secondary = _FakeClient("secondary")
        primary._llm.scripted = [RuntimeError("HTTP 429")]
        secondary._llm.scripted = ["ok"]

        client = FallbackLLMClient([primary, secondary])
        with caplog.at_level("INFO"):
            client.get_llm().invoke("hi")

        messages = [record.getMessage() for record in caplog.records]
        assert any("fallback" in m.lower() for m in messages)