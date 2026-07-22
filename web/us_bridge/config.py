"""Resolve the local US TradingAgents checkout for the out-of-process bridge."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

DEFAULT_US_ROOT = Path("/Users/solo/workspace/tradingAgents")

_WORKER_PATH = Path(__file__).resolve().parent / "worker.py"

# A-stock's provider names always mean the *China* OpenAI-compatible
# endpoint (api.minimaxi.com / dashscope.aliyuncs.com / open.bigmodel.cn).
# Upstream US TradingAgents split dual-region vendors (#758): ``minimax``
# hits api.minimax.io (international) and ``minimax-cn`` hits .com. Passing
# our ``minimax`` through unchanged makes a valid CN key look like
# ``invalid api key (2049)`` on .io — exactly the MU failure mode.
_US_PROVIDER_REMAP: dict[str, str] = {
    "minimax": "minimax-cn",
    "qwen": "qwen-cn",
    "glm": "glm-cn",
}

# When we remap to a *-cn provider, the upstream client reads a separate
# CN-specific env var. Mirror the A-stock key into it if unset so operators
# only need one key in .env.
_US_KEY_ENV_ALIAS: dict[str, tuple[str, str]] = {
    "minimax-cn": ("MINIMAX_CN_API_KEY", "MINIMAX_API_KEY"),
    "qwen-cn": ("DASHSCOPE_CN_API_KEY", "DASHSCOPE_API_KEY"),
    "glm-cn": ("ZHIPU_CN_API_KEY", "ZHIPU_API_KEY"),
}


def remap_provider_for_us(provider: str | None) -> str | None:
    """Map an A-stock provider id onto the US upstream dual-region id."""
    if not provider:
        return provider
    key = str(provider).strip().lower()
    return _US_PROVIDER_REMAP.get(key, str(provider).strip())


def resolve_us_root(root: str | Path | None = None) -> Path:
    raw = root or os.getenv("US_TRADINGAGENTS_ROOT") or str(DEFAULT_US_ROOT)
    path = Path(raw).expanduser().resolve()
    package_init = path / "tradingagents" / "__init__.py"
    if not package_init.is_file():
        raise FileNotFoundError(
            f"US_TRADINGAGENTS_ROOT 无效或不存在包 tradingagents: {path}"
        )
    return path


def resolve_us_python(us_root: Path) -> Path:
    env_py = (os.getenv("US_TRADINGAGENTS_PYTHON") or "").strip()
    if env_py:
        # Use absolute() not resolve(): venv's bin/python is usually a symlink
        # into /usr/bin; following it would drop the venv site-packages when the
        # subprocess is launched with the resolved system interpreter.
        path = Path(env_py).expanduser().absolute()
        if not path.is_file():
            raise FileNotFoundError(f"US_TRADINGAGENTS_PYTHON 不存在: {path}")
        return path

    for candidate in (
        us_root / ".venv" / "bin" / "python",
        us_root / "venv" / "bin" / "python",
    ):
        if candidate.is_file():
            return candidate.absolute()

    return Path(sys.executable).absolute()


def build_worker_command(
    *,
    ticker: str,
    trade_date: str,
    llm_config: dict[str, Any] | None = None,
) -> tuple[list[str], dict[str, str], Path]:
    """Return (argv, env, cwd) for launching the US worker subprocess."""
    us_root = resolve_us_root()
    python = resolve_us_python(us_root)
    llm_config = llm_config or {}

    env = os.environ.copy()
    # Ensure the US package wins over this A-stock checkout in the child.
    env["PYTHONPATH"] = str(us_root)
    env["US_BRIDGE_TICKER"] = ticker
    env["US_BRIDGE_TRADE_DATE"] = trade_date
    env["US_BRIDGE_PROJECT_ROOT"] = str(us_root)

    provider = remap_provider_for_us(llm_config.get("llm_provider"))
    if provider:
        env["TRADINGAGENTS_LLM_PROVIDER"] = str(provider)
        _mirror_cn_api_key(env, provider)
    if llm_config.get("deep_think_llm"):
        env["TRADINGAGENTS_DEEP_THINK_LLM"] = str(llm_config["deep_think_llm"])
    if llm_config.get("quick_think_llm"):
        env["TRADINGAGENTS_QUICK_THINK_LLM"] = str(llm_config["quick_think_llm"])
    backend = (llm_config.get("backend_url") or "").strip()
    if backend:
        env["TRADINGAGENTS_LLM_BACKEND_URL"] = backend
    if llm_config.get("output_language"):
        env["TRADINGAGENTS_OUTPUT_LANGUAGE"] = str(llm_config["output_language"])
    if llm_config.get("max_debate_rounds") is not None:
        env["TRADINGAGENTS_MAX_DEBATE_ROUNDS"] = str(llm_config["max_debate_rounds"])
    if llm_config.get("max_risk_discuss_rounds") is not None:
        env["TRADINGAGENTS_MAX_RISK_ROUNDS"] = str(llm_config["max_risk_discuss_rounds"])

    # Forward the fallback chain so the US subprocess can create a
    # FallbackLLMClient. The upstream TradingAgents does not know about
    # fallback chains natively — we inject it via a custom env var that
    # worker.py reads and merges into the graph config.
    fallback_chain = list(llm_config.get("fallback_chain") or [])
    if fallback_chain:
        import json

        env["US_BRIDGE_FALLBACK_CHAIN"] = json.dumps(fallback_chain, ensure_ascii=False)

    cmd = [str(python), "-u", str(_WORKER_PATH)]
    return cmd, env, us_root


def _mirror_cn_api_key(env: dict[str, str], provider: str) -> None:
    """Ensure the upstream CN key env is populated for remapped providers."""
    alias = _US_KEY_ENV_ALIAS.get(provider.lower())
    if not alias:
        return
    cn_var, shared_var = alias
    if env.get(cn_var):
        return
    shared = env.get(shared_var) or os.environ.get(shared_var)
    if shared:
        env[cn_var] = shared
