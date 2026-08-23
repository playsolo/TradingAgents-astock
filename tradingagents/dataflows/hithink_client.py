"""HiThink (同花顺) Financial-API HTTP client for TradingAgents-Astock.

Reads ``HITHINK_FINANCE_API_KEY`` and ``HITHINK_ENABLED`` from the environment.
When disabled or missing a key, callers should fall back to ``a_stock.py``.

API contract: https://fuyao.aicubes.cn/docs/ (envelope ``{code, message, data}``).
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from typing import Any

import requests

from .utils import safe_ticker_component

logger = logging.getLogger(__name__)

BASE_URL = "https://fuyao.aicubes.cn"
_DEFAULT_UA = (
    "Mozilla/5.0 (compatible; TradingAgents-Astock/0.2; +https://github.com/simonlin1212/TradingAgents-astock)"
)

# Serialise requests + jitter (same pattern as eastmoney _em_get).
_min_interval = float(os.environ.get("HITHINK_MIN_INTERVAL", "0.25"))
_lock = threading.Lock()
_last_request_at = 0.0


class HiThinkAPIError(Exception):
    """Business or transport error from HiThink API."""

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.request_id = request_id


class HiThinkRateLimitError(HiThinkAPIError):
    """``code=4001`` rate limit."""


def is_hithink_enabled() -> bool:
    """True when env or persisted admin config enables API and a key exists."""
    return bool(get_hithink_api_key()) and _enabled_flag_active()


def _enabled_flag_active() -> bool:
    flag = os.environ.get("HITHINK_ENABLED", "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return True
    if flag in ("0", "false", "no", "off"):
        return False
    try:
        from tradingagents.auth.data_enhancement_config import load_data_enhancement_config

        return bool(load_data_enhancement_config().get("hithink_enabled"))
    except Exception:
        return False


def get_hithink_api_key() -> str | None:
    key = os.environ.get("HITHINK_FINANCE_API_KEY", "").strip()
    if key:
        return key
    try:
        from tradingagents.auth.data_enhancement_config import load_data_enhancement_config

        persisted = str(load_data_enhancement_config().get("hithink_api_key") or "").strip()
        return persisted or None
    except Exception:
        return None


def reset_hithink_client() -> None:
    """Drop cached client after key/enable changes."""
    global _client_singleton
    with _client_lock:
        _client_singleton = None


def code_to_thscode(code: str) -> str:
    """6-digit A-share code → ``600519.SH`` style thscode."""
    pure = safe_ticker_component(code)
    if pure.startswith("92") or pure.startswith("8"):
        suffix = "BJ"
    elif pure.startswith(("6", "9")):
        suffix = "SH"
    else:
        suffix = "SZ"
    return f"{pure}.{suffix}"


def _throttle() -> None:
    global _last_request_at
    with _lock:
        now = time.monotonic()
        wait = _min_interval + random.uniform(0.05, 0.15) - (now - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


class HiThinkClient:
    """Thin REST client for fuyao.aicubes.cn."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        session: requests.Session | None = None,
        timeout: float = 20.0,
        max_retries: int = 3,
    ) -> None:
        self.api_key = api_key or get_hithink_api_key()
        self.timeout = timeout
        self.max_retries = max(1, int(max_retries))
        self._session = session or requests.Session()
        self._session.headers.setdefault("User-Agent", _DEFAULT_UA)

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET ``path`` (with or without leading slash); return envelope ``data``."""
        if not self.api_key:
            raise HiThinkAPIError("HITHINK_FINANCE_API_KEY is not set")

        url = path if path.startswith("http") else f"{BASE_URL}{path}"
        headers = {"X-api-key": self.api_key}
        last_err: Exception | None = None

        for attempt in range(self.max_retries):
            _throttle()
            try:
                resp = self._session.get(
                    url, params=params, headers=headers, timeout=self.timeout
                )
                resp.raise_for_status()
                payload = resp.json()
            except requests.RequestException as exc:
                last_err = exc
                if attempt + 1 < self.max_retries:
                    time.sleep(0.5 * (2**attempt))
                    continue
                raise HiThinkAPIError(f"HTTP request failed: {exc}") from exc
            except ValueError as exc:
                raise HiThinkAPIError(f"Invalid JSON response: {exc}") from exc

            code = payload.get("code")
            if code == 0:
                return payload.get("data")

            request_id = payload.get("request_id")
            message = payload.get("message") or "unknown error"
            if code == 4001 and attempt + 1 < self.max_retries:
                backoff = 0.8 * (2**attempt) + random.uniform(0, 0.3)
                logger.warning(
                    "HiThink rate limit (4001), retry %d/%d in %.1fs",
                    attempt + 1,
                    self.max_retries,
                    backoff,
                )
                time.sleep(backoff)
                continue
            if code == 4001:
                raise HiThinkRateLimitError(
                    message, code=code, request_id=request_id
                )
            raise HiThinkAPIError(message, code=code, request_id=request_id)

        raise HiThinkAPIError(f"request failed after retries: {last_err}")

    # ── Meta ─────────────────────────────────────────────────────────────

    def search_tickers(self, q: str, *, limit: int = 5) -> list[dict[str, Any]]:
        data = self.get(
            "/api/meta/tickers/search",
            {"q": q, "limit": min(max(1, limit), 50)},
        )
        return list((data or {}).get("item") or [])

    def resolve_thscode(self, symbol: str) -> str | None:
        """Resolve name or code to a unique A-share thscode, or None."""
        q = symbol.strip()
        if not q:
            return None
        items = self.search_tickers(q, limit=10)
        a_shares = [i for i in items if i.get("asset_type") == "a-share"]
        if not a_shares:
            return None
        pure: str | None = None
        try:
            pure = safe_ticker_component(q)
        except ValueError:
            pass
        if pure:
            target = code_to_thscode(pure)
            for item in a_shares:
                if item.get("thscode") == target:
                    return target
        if len(a_shares) == 1:
            return a_shares[0].get("thscode")
        return None

    # ── Valuation ────────────────────────────────────────────────────────

    def valuations_snapshot(self, thscodes: list[str]) -> list[dict[str, Any]]:
        if not thscodes:
            return []
        joined = ",".join(thscodes[:100])
        data = self.get("/api/a-share/valuations/snapshot", {"thscodes": joined})
        return list((data or {}).get("item") or [])

    # ── Special data ───────────────────────────────────────────────────

    def hot_stock_list(self, *, period: str = "day") -> list[dict[str, Any]]:
        data = self.get(
            "/api/a-share/special-data/hot-stock-list",
            {"period": period},
        )
        return list((data or {}).get("item") or [])

    def skyrocket_list(self, *, period: str = "day") -> list[dict[str, Any]]:
        data = self.get(
            "/api/a-share/special-data/skyrocket-list",
            {"period": period},
        )
        return list((data or {}).get("item") or [])

    def limit_up_pool(self, *, size: int = 50, page: int = 1) -> dict[str, Any]:
        return self.get(
            "/api/a-share/special-data/limit-up-pool",
            {"size": min(max(1, size), 200), "page": max(1, page)},
        ) or {}

    def limit_down_pool(self, *, size: int = 50, page: int = 1) -> dict[str, Any]:
        return self.get(
            "/api/a-share/special-data/limit-down-pool",
            {"size": min(max(1, size), 200), "page": max(1, page)},
        ) or {}

    def limit_break_pool(self, *, size: int = 50, page: int = 1) -> dict[str, Any]:
        return self.get(
            "/api/a-share/special-data/limit-break-pool",
            {"size": min(max(1, size), 200), "page": max(1, page)},
        ) or {}

    def limit_up_ladder(self) -> dict[str, Any]:
        return self.get("/api/a-share/special-data/limit-up-ladder") or {}

    def dragon_tiger_list(
        self, *, board_type: str = "all", date: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"board_type": board_type}
        if date:
            params["date"] = date
        return self.get("/api/a-share/special-data/dragon-tiger-list", params) or {}

    def anomaly_analysis_stock(self, thscodes: list[str]) -> list[dict[str, Any]]:
        if not thscodes:
            return []
        joined = ",".join(thscodes[:50])
        data = self.get(
            "/api/a-share/special-data/anomaly-analysis-stock",
            {"thscodes": joined},
        )
        return list((data or {}).get("item") or [])

    # ── Auction ────────────────────────────────────────────────────────

    def auction_snapshot(
        self, thscodes: list[str], *, stage: str = "final"
    ) -> list[dict[str, Any]]:
        if not thscodes:
            return []
        joined = ",".join(thscodes[:100])
        data = self.get(
            "/api/a-share/auction/snapshot",
            {"thscodes": joined, "stage": stage},
        )
        return list((data or {}).get("item") or [])

    def auction_short_term_benchmark(self, date: str | None = None) -> list[dict[str, Any]]:
        params = {"date": date} if date else None
        data = self.get("/api/a-share/auction/short-term-benchmark", params)
        return list((data or {}).get("item") or [])

    # ── Financials ─────────────────────────────────────────────────────

    def financial_indicators(self, thscode: str, report: str) -> dict[str, Any]:
        """``report`` format ``YYYY-[1-4]`` e.g. ``2024-4`` for annual."""
        return self.get(
            "/api/a-share/financials/indicators",
            {"thscode": thscode, "report": report},
        ) or {}

    def income_statements(
        self,
        thscode: str,
        *,
        period: str = "quarterly",
        limit: int = 4,
    ) -> list[dict[str, Any]]:
        data = self.get(
            "/api/a-share/financials/income-statements",
            {"thscode": thscode, "period": period, "limit": limit},
        )
        return list((data or {}).get("item") or [])

    def balance_sheets(
        self,
        thscode: str,
        *,
        period: str = "quarterly",
        limit: int = 4,
    ) -> list[dict[str, Any]]:
        data = self.get(
            "/api/a-share/financials/balance-sheets",
            {"thscode": thscode, "period": period, "limit": limit},
        )
        return list((data or {}).get("item") or [])

    def cash_flow_statements(
        self,
        thscode: str,
        *,
        period: str = "quarterly",
        limit: int = 4,
    ) -> list[dict[str, Any]]:
        data = self.get(
            "/api/a-share/financials/cash-flow-statements",
            {"thscode": thscode, "period": period, "limit": limit},
        )
        return list((data or {}).get("item") or [])


_client_singleton: HiThinkClient | None = None
_client_lock = threading.Lock()


def get_hithink_client() -> HiThinkClient:
    """Process-wide lazy client (reuses HTTP session)."""
    global _client_singleton
    with _client_lock:
        if _client_singleton is None:
            _client_singleton = HiThinkClient()
        return _client_singleton
