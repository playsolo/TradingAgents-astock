"""Tests for signal accuracy ledger — direction hit + auto settle."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from tradingagents.agents.utils.signal_accuracy import (
    DEFAULT_EPS,
    DEFAULT_HORIZONS,
    SignalAccuracyLedger,
    config_fingerprint,
    direction_hit,
    rating_to_direction,
    return_at_horizon,
    settle_due,
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestRatingToDirection:
    def test_long(self):
        assert rating_to_direction("Buy") == "long"
        assert rating_to_direction("Overweight") == "long"
        assert rating_to_direction("buy") == "long"

    def test_short(self):
        assert rating_to_direction("Sell") == "short"
        assert rating_to_direction("Underweight") == "short"

    def test_neutral(self):
        assert rating_to_direction("Hold") == "neutral"
        assert rating_to_direction("Unknown") == "neutral"
        assert rating_to_direction("") == "neutral"


class TestDirectionHit:
    def test_long_up_hits(self):
        assert direction_hit("long", 0.01, eps=DEFAULT_EPS) is True

    def test_long_flat_misses(self):
        assert direction_hit("long", 0.001, eps=DEFAULT_EPS) is False

    def test_long_down_misses(self):
        assert direction_hit("long", -0.02, eps=DEFAULT_EPS) is False

    def test_short_down_hits(self):
        assert direction_hit("short", -0.01, eps=DEFAULT_EPS) is True

    def test_short_up_misses(self):
        assert direction_hit("short", 0.01, eps=DEFAULT_EPS) is False

    def test_neutral_inside_band_hits(self):
        assert direction_hit("neutral", 0.003, eps=DEFAULT_EPS) is True
        assert direction_hit("neutral", -0.004, eps=DEFAULT_EPS) is True

    def test_neutral_outside_band_misses(self):
        assert direction_hit("neutral", 0.006, eps=DEFAULT_EPS) is False
        assert direction_hit("neutral", -0.01, eps=DEFAULT_EPS) is False


class TestReturnAtHorizon:
    def test_exact_horizon(self):
        closes = [100.0, 101.0, 102.0, 105.0]
        assert return_at_horizon(closes, 1) == pytest.approx(0.01)
        assert return_at_horizon(closes, 3) == pytest.approx(0.05)

    def test_insufficient_bars(self):
        assert return_at_horizon([100.0, 101.0], 5) is None

    def test_anchor_zero_guard(self):
        assert return_at_horizon([0.0, 1.0], 1) is None


class TestConfigFingerprint:
    def test_stable_and_sensitive(self):
        a = config_fingerprint(
            {
                "llm_provider": "deepseek",
                "deep_think_llm": "deepseek-v4-pro",
                "quick_think_llm": "deepseek-v4-flash",
                "max_debate_rounds": 1,
                "max_risk_discuss_rounds": 1,
            }
        )
        b = config_fingerprint(
            {
                "llm_provider": "deepseek",
                "deep_think_llm": "deepseek-v4-pro",
                "quick_think_llm": "deepseek-v4-flash",
                "max_debate_rounds": 1,
                "max_risk_discuss_rounds": 1,
            }
        )
        c = config_fingerprint(
            {
                "llm_provider": "deepseek",
                "deep_think_llm": "deepseek-v4-pro",
                "quick_think_llm": "deepseek-v4-flash",
                "max_debate_rounds": 2,
                "max_risk_discuss_rounds": 1,
            }
        )
        assert a == b
        assert a != c
        assert len(a) == 12


# ---------------------------------------------------------------------------
# Ledger enroll + settle
# ---------------------------------------------------------------------------


def _ledger(tmp_path: Path) -> SignalAccuracyLedger:
    return SignalAccuracyLedger(tmp_path / "signal_accuracy.json")


def _closes_fetcher(series_map: dict[str, list[float]]):
    def fetch(ticker: str, trade_date: str, need_bars: int):
        del trade_date, need_bars
        closes = series_map.get(ticker)
        return None if closes is None else list(closes)

    return fetch


class TestLedgerEnroll:
    def test_enroll_creates_pending_horizons(self, tmp_path):
        led = _ledger(tmp_path)
        rec = led.enroll(
            ticker="600519",
            trade_date="2026-01-10",
            rating="Buy",
            config_fp="cfg1",
        )
        assert rec["direction"] == "long"
        assert set(rec["horizons"]) == {str(h) for h in DEFAULT_HORIZONS}
        for h in DEFAULT_HORIZONS:
            assert rec["horizons"][str(h)]["status"] == "pending"

    def test_enroll_dedup(self, tmp_path):
        led = _ledger(tmp_path)
        a = led.enroll("600519", "2026-01-10", "Buy", config_fp="cfg1")
        b = led.enroll("600519", "2026-01-10", "Overweight", config_fp="cfg1")
        assert a["id"] == b["id"]
        assert len(led.records()) == 1

    def test_enroll_different_config_is_new_sample(self, tmp_path):
        led = _ledger(tmp_path)
        led.enroll("600519", "2026-01-10", "Buy", config_fp="cfg1")
        led.enroll("600519", "2026-01-10", "Buy", config_fp="cfg2")
        assert len(led.records()) == 2


class TestSettleDue:
    def test_settle_long_hit_and_miss(self, tmp_path):
        led = _ledger(tmp_path)
        led.enroll("AAA", "2026-01-02", "Buy", config_fp="c")
        # 1d +1%, 5d flat, 20d not enough bars yet
        fetcher = _closes_fetcher(
            {"AAA": [100.0, 101.0, 100.5, 100.2, 100.1, 100.0]}
        )
        events = settle_due(led, fetcher, as_of=date(2026, 2, 1))
        rec = led.records()[0]
        assert rec["horizons"]["1"]["status"] == "settled"
        assert rec["horizons"]["1"]["hit"] is True
        assert rec["horizons"]["1"]["return"] == pytest.approx(0.01)
        assert rec["horizons"]["5"]["status"] == "settled"
        assert rec["horizons"]["5"]["hit"] is False  # 0% <= eps for long → miss
        assert rec["horizons"]["20"]["status"] == "pending"
        assert any(e["horizon"] == 1 and e["hit"] is True for e in events)
        assert any(e["horizon"] == 5 and e["hit"] is False for e in events)

    def test_settle_short_hit(self, tmp_path):
        led = _ledger(tmp_path)
        led.enroll("BBB", "2026-01-02", "Sell", config_fp="c")
        fetcher = _closes_fetcher({"BBB": [100.0, 98.0]})
        settle_due(led, fetcher)
        assert led.records()[0]["horizons"]["1"]["hit"] is True

    def test_settle_hold_band(self, tmp_path):
        led = _ledger(tmp_path)
        led.enroll("CCC", "2026-01-02", "Hold", config_fp="c")
        fetcher = _closes_fetcher({"CCC": [100.0, 100.3]})
        settle_due(led, fetcher)
        assert led.records()[0]["horizons"]["1"]["hit"] is True

    def test_settle_is_idempotent(self, tmp_path):
        led = _ledger(tmp_path)
        led.enroll("DDD", "2026-01-02", "Buy", config_fp="c")
        fetcher = _closes_fetcher({"DDD": [100.0, 102.0]})
        e1 = settle_due(led, fetcher)
        e2 = settle_due(led, fetcher)
        assert len(e1) >= 1
        assert e2 == []

    def test_missing_prices_leave_pending(self, tmp_path):
        led = _ledger(tmp_path)
        led.enroll("EEE", "2026-01-02", "Buy", config_fp="c")
        settle_due(led, lambda *a, **k: None)
        assert led.records()[0]["horizons"]["1"]["status"] == "pending"


class TestSummary:
    def test_summary_by_horizon_and_direction(self, tmp_path):
        led = _ledger(tmp_path)
        led.enroll("X1", "2026-01-02", "Buy", config_fp="c")
        led.enroll("X2", "2026-01-02", "Sell", config_fp="c")
        fetcher = _closes_fetcher(
            {
                "X1": [100.0, 103.0],  # long hit
                "X2": [100.0, 97.0],  # short hit
            }
        )
        settle_due(led, fetcher)
        s = led.summary()
        assert s["by_horizon"]["1"]["settled"] == 2
        assert s["by_horizon"]["1"]["hits"] == 2
        assert s["by_horizon"]["1"]["hit_rate"] == pytest.approx(1.0)
        assert s["by_direction"]["long"]["1"]["hits"] == 1
        assert s["by_direction"]["short"]["1"]["hits"] == 1


class TestMigrate:
    def test_migrate_from_memory_log(self, tmp_path):
        from tradingagents.agents.utils.memory import TradingMemoryLog

        mem_path = tmp_path / "trading_memory.md"
        log = TradingMemoryLog({"memory_log_path": str(mem_path)})
        log.store_decision("600519", "2026-01-10", "**Rating**: Buy\nLong Moutai.")
        log.store_decision("000001", "2026-01-11", "**Rating**: Sell\nShort bank.")

        led = _ledger(tmp_path)
        n = led.migrate_from_memory_log(log, config_fp="migrated")
        assert n == 2
        assert len(led.records()) == 2
        # re-migrate is no-op for same keys
        assert led.migrate_from_memory_log(log, config_fp="migrated") == 0

    def test_migrate_from_results_dir(self, tmp_path):
        import json

        root = tmp_path / "logs"
        log_dir = root / "300750" / "TradingAgentsStrategy_logs"
        log_dir.mkdir(parents=True)
        state = {
            "final_trade_decision": "**Rating**: Overweight\nBullish.",
            "action_plan": {"rating": "Overweight"},
        }
        (log_dir / "full_states_log_2026-03-01.json").write_text(
            json.dumps(state), encoding="utf-8"
        )
        led = _ledger(tmp_path)
        n = led.migrate_from_results_dir(root, config_fp="fromlogs")
        assert n == 1
        rec = led.records()[0]
        assert rec["ticker"] == "300750"
        assert rec["trade_date"] == "2026-03-01"
        assert rec["direction"] == "long"


class TestRunAccuracyMaintenance:
    def test_backfill_and_settle(self, tmp_path):
        from tradingagents.agents.utils.memory import TradingMemoryLog
        from tradingagents.agents.utils.signal_accuracy import run_accuracy_maintenance

        mem = tmp_path / "trading_memory.md"
        acc = tmp_path / "signal_accuracy.json"
        log = TradingMemoryLog({"memory_log_path": str(mem)})
        log.store_decision("AAA", "2026-01-02", "**Rating**: Buy\nGo long.")
        cfg = {
            "memory_log_path": str(mem),
            "signal_accuracy_path": str(acc),
            "results_dir": str(tmp_path / "logs"),
        }

        def fetcher(ticker, trade_date, need_bars):
            return [100.0, 103.0, 104.0, 105.0, 106.0, 107.0]

        result = run_accuracy_maintenance(cfg, price_fetcher=fetcher)
        assert result["migrated"]["from_memory"] == 1
        assert any(e["horizon"] == 1 and e["hit"] is True for e in result["events"])
        # second call skips migrate
        result2 = run_accuracy_maintenance(cfg, price_fetcher=fetcher)
        assert result2["migrated"]["skipped"] is True
        assert result2["events"] == []
