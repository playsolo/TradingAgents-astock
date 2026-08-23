import os

_TRADINGAGENTS_HOME = os.path.join(os.path.expanduser("~"), ".tradingagents")

# Env-var to config-key mapping. Config values that can be set via environment
# variables are listed here. The env-var value (parsed where needed) wins when set.
_ENV_TO_CONFIG = {
    "TRADINGAGENTS_LLM_MAX_RETRIES": "llm_max_retries",
}


def _build_config() -> dict:
    """Build default config with env-var overrides."""
    config = {
        "project_dir": os.path.abspath(os.path.join(os.path.dirname(__file__), ".")),
        "results_dir": os.getenv("TRADINGAGENTS_RESULTS_DIR", os.path.join(_TRADINGAGENTS_HOME, "logs")),
        "data_cache_dir": os.getenv("TRADINGAGENTS_CACHE_DIR", os.path.join(_TRADINGAGENTS_HOME, "cache")),
        "memory_log_path": os.getenv("TRADINGAGENTS_MEMORY_LOG_PATH", os.path.join(_TRADINGAGENTS_HOME, "memory", "trading_memory.md")),
        # Optional cap on the number of resolved memory log entries. When set,
        # the oldest resolved entries are pruned once this limit is exceeded.
        # Pending entries are never pruned. None disables rotation entirely.
        "memory_log_max_entries": None,
        # Direction-hit accuracy ledger (JSON beside memory log by default).
        "signal_accuracy_path": os.getenv(
            "TRADINGAGENTS_SIGNAL_ACCURACY_PATH",
            os.path.join(_TRADINGAGENTS_HOME, "memory", "signal_accuracy.json"),
        ),
        "signal_accuracy_horizons": (1, 5, 20),
        "signal_accuracy_eps": 0.005,
        # LLM settings
        "llm_provider": "openai",
        "deep_think_llm": "gpt-5.4",
        "quick_think_llm": "gpt-5.4-mini",
        # When None, each provider's client falls back to its own default endpoint
        # (api.openai.com for OpenAI, generativelanguage.googleapis.com for Gemini, ...).
        # The CLI overrides this per provider when the user picks one. Keeping a
        # provider-specific URL here would leak (e.g. OpenAI's /v1 was previously
        # being forwarded to Gemini, producing malformed request URLs).
        "backend_url": None,
        # Provider-specific thinking configuration
        "google_thinking_level": None,      # "high", "minimal", etc.
        "openai_reasoning_effort": None,    # "medium", "high", "low"
        "anthropic_effort": None,           # "high", "medium", "low"
        # Configurable LLM retry budget forwarded to every provider.
        # Unset leaves each SDK at its own default (usually 2).
        "llm_max_retries": None,
        # Checkpoint/resume: when True, LangGraph saves state after each node
        # so a crashed run can resume from the last successful step.
        "checkpoint_enabled": False,
        # Output language for analyst reports and final decision
        # Internal agent debate stays in English for reasoning quality
        "output_language": "Chinese",
        # How many days of price/indicator history the market analyst covers
        # (the "analysis window", ending at the analysis date). Drives the
        # look_back_days the market analyst passes to get_stock_data /
        # get_indicators. The Web sidebar / CLI derive this from a user-picked
        # start date (default: first day of the current month → "monthly" view);
        # None keeps the previous behaviour (the model's own default, ~30). (#16)
        "market_lookback_days": None,
        # Debate and discussion settings
        "max_debate_rounds": 1,
        "max_risk_discuss_rounds": 1,
        "max_recur_limit": 100,
        # Data vendor configuration
        # Category-level configuration (default for all tools in category)
        "data_vendors": {
            "core_stock_apis": "a_stock",        # Options: a_stock, alpha_vantage, yfinance
            "technical_indicators": "a_stock",   # Options: a_stock, alpha_vantage, yfinance
            "fundamental_data": "a_stock",       # Options: a_stock, alpha_vantage, yfinance
            "news_data": "a_stock",              # Options: a_stock, alpha_vantage, yfinance
            "signal_data": "a_stock",            # A-stock only: topic attribution, capital flow, consensus
            "hithink_enhanced": "hithink",       # HiThink optional structured metrics
        },
        # Tool-level configuration (takes precedence over category-level)
        "tool_vendors": {
            # Example: "get_stock_data": "alpha_vantage",  # Override category default
        },
    }

    # Apply env-var overrides
    for env_key, config_key in _ENV_TO_CONFIG.items():
        value = os.environ.get(env_key)
        if value is not None:
            if env_key == "TRADINGAGENTS_LLM_MAX_RETRIES":
                try:
                    value = int(value)
                except (ValueError, TypeError):
                    pass
            config[config_key] = value

    return config


DEFAULT_CONFIG = _build_config()
