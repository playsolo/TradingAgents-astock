"""独立观察守护：配置解析与单次时段执行。"""

from datetime import datetime

from tradingagents.watchlist.daemon import config_from_env, run_slot_once
from tradingagents.watchlist.models import Baseline, MarketSnapshot, WatchItem
from tradingagents.watchlist.store import WatchlistStore


def test_config_from_env_prefers_default_provider(monkeypatch):
    monkeypatch.setenv("DEFAULT_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    cfg = config_from_env()
    assert cfg["llm_provider"] == "deepseek"
    assert cfg["quick_think_llm"]
    assert cfg["deep_think_llm"]
    assert "gpt-" not in cfg["deep_think_llm"]


def test_run_slot_once_observes_when_in_window(monkeypatch, tmp_path):
    from tradingagents.watchlist import daemon as daemon_mod
    from tradingagents.watchlist import observe as observe_mod

    store = WatchlistStore(tmp_path / "w.json")
    store.add(
        WatchItem(
            baseline=Baseline(
                ticker="002648",
                trade_date="2026-07-13",
                market="CN",
                stance="Hold",
                position_pct=10.0,
                baseline_price=100.0,
                entry_price=None,
                stop_loss=None,
                thesis_summary="t",
                major_risks=[],
                log_path="",
            )
        )
    )

    monkeypatch.setattr(
        observe_mod,
        "fetch_snapshot",
        lambda *_a, **_k: MarketSnapshot(price=101.0, change_pct=1.0, name="x"),
    )
    monkeypatch.setattr(
        observe_mod,
        "judge_vs_baseline",
        lambda *_a, **_k: {
            "suggested_stance": "Hold",
            "suggested_position_pct": 10.0,
            "new_major_risks": [],
            "summary": "ok",
            "watch_point": "",
            "avoid": "",
            "market_brief": "窄幅震荡",
            "lean": "neutral",
            "lean_reason": "缺乏催化剂",
            "scenarios": {
                "optimistic": {"view": "反弹", "reason": "超跌"},
                "neutral": {"view": "震荡", "reason": "观望"},
                "pessimistic": {"view": "下行", "reason": "破位"},
            },
        },
    )
    monkeypatch.setattr(daemon_mod, "build_quick_llm", lambda *_a, **_k: None)

    result = run_slot_once(
        store=store,
        config={},
        now=datetime(2026, 7, 14, 9, 36),
    )
    assert result["slot_key"] == "2026-07-14T09:35"
    assert "002648" in result["observed"]


def test_run_slot_once_noop_outside_window(tmp_path):
    store = WatchlistStore(tmp_path / "w.json")
    result = run_slot_once(
        store=store,
        config={},
        now=datetime(2026, 7, 14, 10, 0),
    )
    assert result["slot_key"] is None
    assert result["observed"] == {}
