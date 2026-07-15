"""Progress reporting for the value-swing scan (stage / counts / percent / current stock)."""

from __future__ import annotations

import pytest

from tradingagents.strategies import value_swing
from tradingagents.strategies.value_swing import (
    StockInfo,
    run_l1b_filter,
    run_l2_filter,
    run_value_swing_scan,
)


def _fin(code: str) -> StockInfo:
    """A stock that clears L1b with pre-filled financials (no network)."""
    return StockInfo(
        code=code,
        name=f"股票{code}",
        pe_ttm=10,
        pb=1,
        revenue_growth=0.05,
        debt_ratio=0.4,
        amplitude_20d=0.04,
    )


class TestStageItemCallbacks:
    def test_l1b_reports_each_processed_stock(self):
        stocks = [_fin(f"00000{i}") for i in range(1, 4)]
        seen: list[tuple[str, str, int, int]] = []
        run_l1b_filter(stocks, on_item=lambda code, name, idx, total: seen.append((code, name, idx, total)))
        assert [s[0] for s in seen] == ["000001", "000002", "000003"]
        # index is 1-based, total equals processed sample size
        assert seen[0][2] == 1 and seen[0][3] == 3
        assert seen[-1][2] == 3

    def test_l2_reports_each_processed_stock(self, monkeypatch: pytest.MonkeyPatch):
        # Neutralize all network catalyst checks so L2 stays offline.
        monkeypatch.setattr(value_swing, "_check_northbound_3d", lambda code: None)
        monkeypatch.setattr(value_swing, "_load_hot_stocks", lambda: {})
        monkeypatch.setattr(value_swing, "_load_global_news", lambda: [])
        monkeypatch.setattr(value_swing, "_check_ma_support", lambda code: (False, False))
        monkeypatch.setattr(value_swing, "_check_news_catalyst", lambda code: False)

        # 000001 is the SSE index (excluded by _is_stock_code); use 000002.
        stocks = [
            StockInfo(code="600519", name="贵州茅台", volume_wan=9000),
            StockInfo(code="000002", name="万科A", volume_wan=8000),
        ]
        seen: list[str] = []
        run_l2_filter(stocks, on_item=lambda code, name, idx, total: seen.append(code))
        assert set(seen) == {"600519", "000002"}


class TestScanProgressSequence:
    def test_progress_cb_emits_stage_sequence_and_monotonic_percent(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Stub out the four heavy stages with offline fakes.
        monkeypatch.setattr(value_swing, "_get_all_cn_codes", lambda: ["000001", "000002"])
        monkeypatch.setattr(value_swing, "run_l0_filter", lambda: [_fin("000001"), _fin("000002")])
        monkeypatch.setattr(value_swing, "run_l1a_filter", lambda s: s)

        def fake_l1b(stocks, *, on_item=None):
            if on_item:
                for i, info in enumerate(stocks, 1):
                    on_item(info.code, info.name, i, len(stocks))
            return stocks

        def fake_l2(stocks, max_candidates=15, *, on_item=None):
            if on_item:
                for i, info in enumerate(stocks, 1):
                    on_item(info.code, info.name, i, len(stocks))
            return stocks[:max_candidates]

        monkeypatch.setattr(value_swing, "run_l1b_filter", fake_l1b)
        monkeypatch.setattr(value_swing, "run_l2_filter", fake_l2)

        events: list[dict] = []
        result = run_value_swing_scan(max_candidates=5, progress_cb=events.append)

        assert result.l2_passed == 2
        assert events, "progress_cb was never called"

        # Stages advance L0 -> L1a -> L1b -> L2 -> done.
        stages = [e["stage"] for e in events]
        assert stages[0] == "L0"
        assert "L1a" in stages and "L1b" in stages and "L2" in stages
        assert stages[-1] == "done"

        # Percent never decreases and ends at 100.
        percents = [e["percent"] for e in events]
        assert percents == sorted(percents)
        assert percents[-1] == 100

        # Payload carries funnel counts + current stock during item events.
        l1b_events = [e for e in events if e["stage"] == "L1b" and e["current_code"]]
        assert l1b_events
        assert l1b_events[0]["l0_passed"] == 2
        assert l1b_events[0]["current_code"] in {"000001", "000002"}
        assert l1b_events[0]["stage_total"] == 2

    def test_progress_cb_optional(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(value_swing, "_get_all_cn_codes", lambda: ["000001"])
        monkeypatch.setattr(value_swing, "run_l0_filter", lambda: [])
        monkeypatch.setattr(value_swing, "run_l1a_filter", lambda s: s)
        monkeypatch.setattr(value_swing, "run_l1b_filter", lambda s, *, on_item=None: s)
        monkeypatch.setattr(value_swing, "run_l2_filter", lambda s, max_candidates=15, *, on_item=None: s)
        # Must not raise without a callback.
        run_value_swing_scan(max_candidates=5)
