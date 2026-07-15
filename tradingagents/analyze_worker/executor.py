"""单只股票的分析执行（进程内、无 Streamlit）。

复用 ``web.runner.execute_analysis_run`` 的核心管线，但配置只来自磁盘
（``model_config.json`` / 环境变量），因为 worker 进程没有 Streamlit session。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable

from tradingagents.default_config import DEFAULT_CONFIG

logger = logging.getLogger(__name__)


def build_worker_config() -> dict[str, Any]:
    """Build a runtime config from disk-only sources (no session_state).

    Model choice priority: admin ``model_config.json`` → env fallback.
    Mirrors ``web.app._build_config`` (Deep depth: 5 debate / 5 risk rounds).
    """
    from tradingagents.auth.model_config import load_model_config, model_config_exists

    config = DEFAULT_CONFIG.copy()

    if model_config_exists():
        persisted = load_model_config()
        config["llm_provider"] = persisted["llm_provider"]
        config["deep_think_llm"] = persisted["deep_think_llm"]
        config["quick_think_llm"] = persisted["quick_think_llm"]
        backend_url = (persisted.get("backend_url") or os.getenv("BACKEND_URL") or "").strip()
        config["backend_url"] = backend_url or None
    else:
        # No admin config yet: fall back to env so a fresh install still runs.
        config["llm_provider"] = os.getenv("DEFAULT_LLM_PROVIDER", "deepseek").strip() or "deepseek"
        config["deep_think_llm"] = os.getenv("DEEP_THINK_LLM", "deepseek-chat").strip() or "deepseek-chat"
        config["quick_think_llm"] = (
            os.getenv("QUICK_THINK_LLM", "deepseek-chat").strip() or "deepseek-chat"
        )
        backend_url = (os.getenv("BACKEND_URL") or "").strip()
        config["backend_url"] = backend_url or None

    config["data_vendors"] = {
        "core_stock_apis": "a_stock",
        "technical_indicators": "a_stock",
        "fundamental_data": "a_stock",
        "news_data": "a_stock",
        "signal_data": "a_stock",
    }
    # Align with CLI Research Depth=Deep.
    config["max_debate_rounds"] = 5
    config["max_risk_discuss_rounds"] = 5
    config["checkpoint_enabled"] = True
    config["output_language"] = "Chinese"
    return config


def run_one_job(job: Any, config: dict[str, Any]) -> None:
    """Execute one :class:`AnalysisJob` synchronously to a terminal state.

    Never raises: pipeline errors are recorded in the incomplete index by the
    underlying runner. Runs inside a worker pool thread.
    """
    from web.progress import ProgressTracker
    from web.runner import execute_analysis_run

    market = job.market if job.market in {"CN", "US"} else "CN"
    tracker = ProgressTracker(
        ticker=job.ticker,
        trade_date=job.trade_date,
        market=market,
    )
    logger.info("analyze start %s %s (%s)", job.ticker, job.trade_date, market)
    execute_analysis_run(
        job.ticker,
        job.trade_date,
        config,
        tracker,
        market=market,
    )
    if tracker.error:
        logger.warning("analyze error %s: %s", job.ticker, tracker.error)
    else:
        logger.info("analyze done %s -> %s", job.ticker, tracker.signal or "N/A")


# Type alias for daemon injection / testing.
RunFn = Callable[[Any, dict[str, Any]], None]
