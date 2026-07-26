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
            "deep_think_llm": "MiniMax-M3",
            "quick_think_llm": "MiniMax-M3",
            "backend_url": None,
        },
    )
    cfg = executor.build_worker_config()
    assert cfg["llm_provider"] == "minimax"
    assert cfg["deep_think_llm"] == "MiniMax-M3"
    assert cfg["max_debate_rounds"] == 5
    assert cfg["max_risk_discuss_rounds"] == 5
    assert cfg["checkpoint_enabled"] is True
    assert cfg["data_vendors"]["core_stock_apis"] == "a_stock"


def test_build_worker_config_env_fallback(monkeypatch):
    monkeypatch.setattr(
        "tradingagents.auth.model_config.model_config_exists", lambda: False
    )
    monkeypatch.setenv("DEFAULT_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEP_THINK_LLM", "deepseek-v4-pro")
    cfg = executor.build_worker_config()
    assert cfg["llm_provider"] == "deepseek"
    assert cfg["deep_think_llm"] == "deepseek-v4-pro"


def test_build_worker_config_reads_fallback_chain(monkeypatch):
    """Worker must honour the admin-configured fallback_chain so a primary
    provider outage degrades to deepseek instead of crashing. Previously
    build_worker_config only copied provider/deep/quick/backend and dropped
    fallback_chain on the floor, silently disabling the entire fallback
    mechanism for the analyze-worker entry point."""
    monkeypatch.setattr(
        "tradingagents.auth.model_config.model_config_exists", lambda: True
    )
    monkeypatch.setattr(
        "tradingagents.auth.model_config.load_model_config",
        lambda: {
            "llm_provider": "minimax",
            "deep_think_llm": "MiniMax-M3",
            "quick_think_llm": "MiniMax-M3",
            "backend_url": None,
            "fallback_chain": [
                {"provider": "deepseek", "model": "deepseek-v4-flash"},
            ],
        },
    )
    cfg = executor.build_worker_config()
    assert cfg.get("fallback_chain") == [
        {"provider": "deepseek", "model": "deepseek-v4-flash"}
    ], (
        "build_worker_config must mirror fallback_chain from model_config.json "
        "so the analyze worker gets the same failover semantics as web/runner."
    )


def test_build_worker_config_fallback_chain_default_when_missing(monkeypatch):
    """When the persisted config has no fallback_chain key at all (legacy
    installs), build_worker_config should still produce a working chain —
    an empty list preserves the operator's intent to disable fallback."""
    monkeypatch.setattr(
        "tradingagents.auth.model_config.model_config_exists", lambda: True
    )
    monkeypatch.setattr(
        "tradingagents.auth.model_config.load_model_config",
        lambda: {
            "llm_provider": "deepseek",
            "deep_think_llm": "deepseek-v4-pro",
            "quick_think_llm": "deepseek-v4-flash",
            "backend_url": None,
        },
    )
    cfg = executor.build_worker_config()
    assert cfg.get("fallback_chain") == [], (
        "fallback_chain must default to an empty list (operator intent to "
        "disable fallback is preserved); never raise KeyError."
    )


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
        calls["extra_past_context"] = kw.get("extra_past_context", "")
        tracker.signal = "BUY"
        tracker.final_state = {"final_trade_decision": "Buy"}
        tracker.is_complete = True
        return tracker

    monkeypatch.setattr("web.runner.execute_analysis_run", fake_execute)
    monkeypatch.setattr(
        "tradingagents.analysis.mode_router.resolve_analysis_mode",
        lambda **kw: __import__(
            "tradingagents.analysis.mode_router", fromlist=["AnalysisRouteDecision"]
        ).AnalysisRouteDecision(mode="full_reeval", reason="force"),
    )
    monkeypatch.setattr(
        "tradingagents.analysis.persist.save_calibration_from_state",
        lambda *a, **k: None,
    )
    job = AnalysisJob(ticker="300253", trade_date="2026-07-15", market="CN")
    executor.run_one_job(job, {"llm_provider": "deepseek", "data_cache_dir": "/tmp"})
    assert calls["ticker"] == "300253"
    assert calls["market"] == "CN"
    assert "extra_past_context" in calls


def test_run_one_job_skips_deep_for_scan_narrow(monkeypatch):
    from tradingagents.analysis.mode_router import MODE_SKIP, AnalysisRouteDecision
    from web.analysis_queue import AnalysisJob

    calls: dict = {"execute": 0, "skipped": 0}

    def fake_resolve(**kw):
        return AnalysisRouteDecision(mode=MODE_SKIP, reason="narrow_ok_scan_skip")

    def fake_execute(*a, **k):
        calls["execute"] += 1

    def fake_skip(*a, **k):
        calls["skipped"] += 1
        return {}

    monkeypatch.setattr(
        "tradingagents.analysis.mode_router.resolve_analysis_mode", fake_resolve
    )
    monkeypatch.setattr("web.runner.execute_analysis_run", fake_execute)
    monkeypatch.setattr("tradingagents.inbox.emit_analysis_skipped", fake_skip)
    job = AnalysisJob(
        ticker="300253",
        trade_date="2026-07-15",
        market="CN",
        source="scan",
    )
    executor.run_one_job(job, {"llm_provider": "deepseek", "data_cache_dir": "/tmp"})
    assert calls["execute"] == 0
    assert calls["skipped"] == 1
