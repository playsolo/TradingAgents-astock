"""worker executor：文件-only 配置 + 无 Streamlit 依赖。"""

from __future__ import annotations

from pathlib import Path

from tradingagents.analyze_worker import executor
from tradingagents.analyze_worker.mode import analysis_executor, is_worker_mode


def test_default_executor_is_web(monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_ANALYSIS_EXECUTOR", raising=False)
    assert analysis_executor() == "web"
    assert is_worker_mode() is False


def test_worker_mode_env(monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_ANALYSIS_EXECUTOR", "worker")
    assert analysis_executor() == "worker"
    assert is_worker_mode() is True


def test_worker_mode_unknown_value_falls_back_to_web(monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_ANALYSIS_EXECUTOR", "nonsense")
    assert is_worker_mode() is False


def test_build_worker_config_uses_model_config(monkeypatch):
    monkeypatch.setattr(
        "tradingagents.auth.model_config.model_config_exists", lambda: True
    )
    monkeypatch.setattr(
        "tradingagents.auth.model_config.load_model_config",
        lambda: {
            "llm_provider": "minimax",
            "deep_think_llm": "MiniMax-M2.7",
            "quick_think_llm": "MiniMax-M2.7-highspeed",
            "backend_url": None,
        },
    )
    cfg = executor.build_worker_config()
    assert cfg["llm_provider"] == "minimax"
    assert cfg["deep_think_llm"] == "MiniMax-M2.7"
    assert cfg["max_debate_rounds"] == 5
    assert cfg["max_risk_discuss_rounds"] == 5
    assert cfg["checkpoint_enabled"] is True
    assert cfg["data_vendors"]["core_stock_apis"] == "a_stock"


def test_build_worker_config_env_fallback(monkeypatch):
    monkeypatch.setattr(
        "tradingagents.auth.model_config.model_config_exists", lambda: False
    )
    monkeypatch.setenv("DEFAULT_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEP_THINK_LLM", "deepseek-chat")
    cfg = executor.build_worker_config()
    assert cfg["llm_provider"] == "deepseek"
    assert cfg["deep_think_llm"] == "deepseek-chat"


def test_executor_module_is_streamlit_free():
    src = Path("tradingagents/analyze_worker/executor.py").read_text(encoding="utf-8")
    assert "import streamlit" not in src
    assert "from streamlit" not in src


def test_run_one_job_delegates_to_execute_analysis_run(monkeypatch):
    from web.analysis_queue import AnalysisJob

    calls: dict = {}

    def fake_execute(ticker, trade_date, config, tracker, market="CN", **kw):
        calls["ticker"] = ticker
        calls["market"] = market
        tracker.signal = "BUY"
        return tracker

    monkeypatch.setattr("web.runner.execute_analysis_run", fake_execute)
    job = AnalysisJob(ticker="300253", trade_date="2026-07-15", market="CN")
    executor.run_one_job(job, {"llm_provider": "deepseek"})
    assert calls == {"ticker": "300253", "market": "CN"}
