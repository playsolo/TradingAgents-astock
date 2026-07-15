"""US bridge worker: completion signal + action_plan serialization.

The worker runs under the US checkout's PYTHONPATH, whose ``TradingAgentsGraph``
may predate the A-stock ``finalize_graph_run`` refactor. Completion must never
crash the whole run because of a missing method.
"""

from __future__ import annotations

import json

from web.us_bridge import worker


class _GraphNoFinalize:
    """Mimics an older US graph exposing only process_signal."""

    def process_signal(self, text: str) -> str:
        return "Sell"


class _GraphWithFinalize:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def finalize_graph_run(self, ticker, trade_date, state):
        self.calls.append((ticker, trade_date))
        state["action_plan"] = {"rating": "Buy"}
        return "Buy"

    def process_signal(self, text: str) -> str:
        return "Hold"


def test_finalize_or_signal_falls_back_when_method_missing():
    signal = worker._finalize_or_signal(
        _GraphNoFinalize(), "NVDA", "2024-05-10", {"final_trade_decision": "减持"}
    )
    assert signal == "Sell"


def test_finalize_or_signal_uses_finalize_when_available():
    graph = _GraphWithFinalize()
    merged = {"final_trade_decision": "Buy NVDA"}
    signal = worker._finalize_or_signal(graph, "NVDA", "2024-05-10", merged)
    assert signal == "Buy"
    assert graph.calls == [("NVDA", "2024-05-10")]
    assert merged["action_plan"] == {"rating": "Buy"}


def test_finalize_or_signal_prefers_action_plan_rating_on_fallback():
    """If finalize populated action_plan before raising, the fallback signal
    must agree with that rating, not a heuristic scan of the full memo."""

    class _BoomAfterPlan:
        def finalize_graph_run(self, ticker, trade_date, state):
            state["action_plan"] = {"rating": "Underweight"}
            raise RuntimeError("boom after plan")

        def process_signal(self, text: str) -> str:
            return "Buy"  # deliberately wrong; must not win

    merged = {"final_trade_decision": "牛方主张买入"}
    signal = worker._finalize_or_signal(_BoomAfterPlan(), "NVDA", "2024-05-10", merged)
    assert signal == "Sell"


def test_finalize_or_signal_collapses_five_tier_process_signal():
    """process_signal can return 5-tier (Overweight/Underweight); the emitted
    completion signal must be collapsed to the 3-tier the sidebar understands."""

    class _FiveTier:
        def process_signal(self, text: str) -> str:
            return "Overweight"

    signal = worker._finalize_or_signal(
        _FiveTier(), "NVDA", "2024-05-10", {"final_trade_decision": "看多"}
    )
    assert signal == "Buy"


def test_finalize_or_signal_falls_back_when_finalize_raises():
    class _Boom:
        def finalize_graph_run(self, *_):
            raise RuntimeError("US graph mismatch")

        def process_signal(self, text: str) -> str:
            return "Hold"

    signal = worker._finalize_or_signal(
        _Boom(), "NVDA", "2024-05-10", {"final_trade_decision": "持有"}
    )
    assert signal == "Hold"


def test_worker_serialize_keeps_action_plan_dict():
    merged = {
        "final_trade_decision": "**最终评级**：减持",
        "action_plan": {"rating": "Underweight", "levels": {"watch_support": 96.0}},
    }
    out = worker._serialize(merged)
    assert out["action_plan"]["rating"] == "Underweight"
    assert out["action_plan"]["levels"]["watch_support"] == 96.0
    json.dumps(out)
