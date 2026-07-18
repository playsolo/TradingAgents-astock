"""US TradingAgents bridge path / interpreter resolution."""

from __future__ import annotations

from pathlib import Path

from web.us_bridge.config import (
    DEFAULT_US_ROOT,
    build_worker_command,
    resolve_us_python,
    resolve_us_root,
)


def test_resolve_us_root_default(monkeypatch, tmp_path):
    monkeypatch.delenv("US_TRADINGAGENTS_ROOT", raising=False)
    # Force default existence check via monkeypatch of DEFAULT used in resolve — we
    # exercise the env override path with a real temp tree instead.
    us_root = tmp_path / "tradingAgents"
    (us_root / "tradingagents").mkdir(parents=True)
    (us_root / "tradingagents" / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.setenv("US_TRADINGAGENTS_ROOT", str(us_root))
    assert resolve_us_root() == us_root.resolve()


def test_resolve_us_root_rejects_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("US_TRADINGAGENTS_ROOT", str(tmp_path / "missing"))
    try:
        resolve_us_root()
        assert False, "expected FileNotFoundError"
    except FileNotFoundError as exc:
        assert "US_TRADINGAGENTS_ROOT" in str(exc) or "missing" in str(exc)


def test_resolve_us_python_prefers_env(monkeypatch, tmp_path):
    py = tmp_path / "python"
    py.write_text("#!/bin/sh\n", encoding="utf-8")
    py.chmod(0o755)
    monkeypatch.setenv("US_TRADINGAGENTS_PYTHON", str(py))
    assert resolve_us_python(tmp_path) == py.resolve()


def test_resolve_us_python_falls_back_to_venv(monkeypatch, tmp_path):
    monkeypatch.delenv("US_TRADINGAGENTS_PYTHON", raising=False)
    venv_py = tmp_path / ".venv" / "bin" / "python"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("#!/bin/sh\n", encoding="utf-8")
    venv_py.chmod(0o755)
    assert resolve_us_python(tmp_path) == venv_py.resolve()


def test_build_worker_command_points_at_worker(monkeypatch, tmp_path):
    us_root = tmp_path / "tradingAgents"
    (us_root / "tradingagents").mkdir(parents=True)
    (us_root / "tradingagents" / "__init__.py").write_text("", encoding="utf-8")
    py = tmp_path / "python"
    py.write_text("#!/bin/sh\n", encoding="utf-8")
    py.chmod(0o755)
    monkeypatch.setenv("US_TRADINGAGENTS_ROOT", str(us_root))
    monkeypatch.setenv("US_TRADINGAGENTS_PYTHON", str(py))

    cmd, env, cwd = build_worker_command(
        ticker="NVDA",
        trade_date="2024-05-10",
        llm_config={
            "llm_provider": "openai",
            "deep_think_llm": "gpt-4o",
            "quick_think_llm": "gpt-4o-mini",
            "backend_url": "https://example.com/v1",
        },
    )
    assert cmd[0] == str(py.resolve())
    assert "-u" in cmd
    assert any(str(p).endswith("worker.py") for p in cmd)
    assert cwd == us_root.resolve()
    assert env["PYTHONPATH"] == str(us_root.resolve())
    assert env["US_BRIDGE_TICKER"] == "NVDA"
    assert env["US_BRIDGE_TRADE_DATE"] == "2024-05-10"
    assert env["TRADINGAGENTS_LLM_PROVIDER"] == "openai"
    assert env["TRADINGAGENTS_LLM_BACKEND_URL"] == "https://example.com/v1"
    assert DEFAULT_US_ROOT.name == "tradingAgents"


def test_build_worker_command_remaps_minimax_to_cn(monkeypatch, tmp_path):
    """A-stock minimax is China; US upstream ``minimax`` is international (.io)."""
    from web.us_bridge.config import remap_provider_for_us

    assert remap_provider_for_us("minimax") == "minimax-cn"
    assert remap_provider_for_us("deepseek") == "deepseek"

    us_root = tmp_path / "tradingAgents"
    (us_root / "tradingagents").mkdir(parents=True)
    (us_root / "tradingagents" / "__init__.py").write_text("", encoding="utf-8")
    py = tmp_path / "python"
    py.write_text("#!/bin/sh\n", encoding="utf-8")
    py.chmod(0o755)
    monkeypatch.setenv("US_TRADINGAGENTS_ROOT", str(us_root))
    monkeypatch.setenv("US_TRADINGAGENTS_PYTHON", str(py))
    monkeypatch.setenv("MINIMAX_API_KEY", "sk-cn-test-key")
    monkeypatch.delenv("MINIMAX_CN_API_KEY", raising=False)

    _cmd, env, _cwd = build_worker_command(
        ticker="MU",
        trade_date="2026-07-18",
        llm_config={"llm_provider": "minimax", "deep_think_llm": "MiniMax-M3"},
    )
    assert env["TRADINGAGENTS_LLM_PROVIDER"] == "minimax-cn"
    assert env["MINIMAX_CN_API_KEY"] == "sk-cn-test-key"
