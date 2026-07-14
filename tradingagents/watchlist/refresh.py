"""观察池：完整分析过期后的 headless 再分析。"""

from __future__ import annotations

import logging
from copy import deepcopy
from typing import Any

from tradingagents.watchlist.models import Baseline, WatchItem
from tradingagents.watchlist.service import prior_context_from_baseline

logger = logging.getLogger(__name__)

# 与 daemon 默认保持一致：配置缺 deep_think_llm 时按供应商补齐
_PROVIDER_DEEP_DEFAULTS = {
    "deepseek": "deepseek-chat",
    "minimax": "MiniMax-M2.7",
    "qwen": "qwen3.5-plus",
    "glm": "glm-4-plus",
    "openai": "gpt-5.4",
    "anthropic": "claude-sonnet-4-6",
}
# 与 Web `_build_config` 对齐，避免守护进程完整再分析辩论过浅
_FULL_REFRESH_DEBATE_ROUNDS = 5
_FULL_REFRESH_RISK_ROUNDS = 5


def run_full_refresh_analysis(
    item: WatchItem,
    *,
    trade_date: str,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """在旧基准先验上跑一轮完整 TradingAgents 图，返回 final_state。"""
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    baseline: Baseline = item.baseline
    cfg = deepcopy(DEFAULT_CONFIG)
    if config:
        cfg.update({k: v for k, v in config.items() if v is not None})
    # 避免只覆盖 quick 时残留 DEFAULT_CONFIG 的 OpenAI deep 模型
    if config and not config.get("deep_think_llm"):
        provider = str(cfg.get("llm_provider") or "").lower()
        cfg["deep_think_llm"] = _PROVIDER_DEEP_DEFAULTS.get(
            provider, cfg["deep_think_llm"]
        )
    # 调用方未显式覆盖时，与 Web 完整再分析辩论深度对齐
    if not config or config.get("max_debate_rounds") is None:
        cfg["max_debate_rounds"] = _FULL_REFRESH_DEBATE_ROUNDS
    if not config or config.get("max_risk_discuss_rounds") is None:
        cfg["max_risk_discuss_rounds"] = _FULL_REFRESH_RISK_ROUNDS

    prior = prior_context_from_baseline(baseline)
    graph = TradingAgentsGraph(debug=False, config=cfg)
    try:
        final_state, _signal = graph.propagate(
            baseline.ticker,
            trade_date,
            extra_past_context=prior,
        )
        return final_state
    finally:
        graph.close_graph_run()
