"""Resolve the local US TradingAgents checkout for the out-of-process bridge."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

DEFAULT_US_ROOT = Path("/Users/solo/workspace/tradingAgents")

_WORKER_PATH = Path(__file__).resolve().parent / "worker.py"


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

    provider = llm_config.get("llm_provider")
    if provider:
        env["TRADINGAGENTS_LLM_PROVIDER"] = str(provider)
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

    cmd = [str(python), "-u", str(_WORKER_PATH)]
    return cmd, env, us_root
