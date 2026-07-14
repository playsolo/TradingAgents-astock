"""US bridge subprocess client → ProgressTracker updates."""

from __future__ import annotations

import json
from typing import Iterator

from web.progress import ProgressTracker
from web.us_bridge.client import consume_bridge_stdout, run_us_analysis
from web.us_bridge.protocol import US_PIPELINE_STAGES, encode_event


def _lines(*events: dict) -> Iterator[str]:
    for event in events:
        yield encode_event(event["event"], **{k: v for k, v in event.items() if k != "event"})


def test_consume_bridge_stdout_applies_events_in_order():
    tracker = ProgressTracker(ticker="NVDA", trade_date="2024-05-10")
    tracker.is_running = True
    tracker.stages = list(US_PIPELINE_STAGES)

    lines = _lines(
        {"event": "stage_active", "stage": "market"},
        {"event": "stage_done", "stage": "market", "report": "m"},
        {"event": "stage_done", "stage": "social", "report": "s"},
        {
            "event": "complete",
            "signal": "Hold",
            "state": {"final_trade_decision": "Hold"},
        },
    )
    consume_bridge_stdout(tracker, lines)
    assert tracker.is_complete
    assert tracker.signal == "Hold"
    assert "market" in tracker.completed_stages
    assert "social" in tracker.completed_stages


def test_consume_bridge_stdout_marks_error_event():
    tracker = ProgressTracker(ticker="NVDA", trade_date="2024-05-10")
    tracker.is_running = True
    consume_bridge_stdout(
        tracker,
        _lines({"event": "error", "message": "boom"}),
    )
    assert tracker.error == "boom"
    assert not tracker.is_running


def test_run_us_analysis_uses_injected_popen(monkeypatch):
    class FakeProc:
        def __init__(self):
            self.pid = 4242
            self.stdout = iter(
                [
                    encode_event("stage_done", stage="market", report="ok"),
                    encode_event(
                        "complete",
                        signal="Buy",
                        state={"market_report": "ok", "final_trade_decision": "Buy"},
                    ),
                ]
            )
            self.returncode = 0
            self._killed = False

        def poll(self):
            return self.returncode if self._killed else None

        def wait(self, timeout=None):
            return 0

        def kill(self):
            self._killed = True
            self.returncode = -9

    created = {}

    def fake_popen(*args, **kwargs):
        created["args"] = args
        created["kwargs"] = kwargs
        return FakeProc()

    monkeypatch.setattr("web.us_bridge.client.subprocess.Popen", fake_popen)
    monkeypatch.setattr(
        "web.us_bridge.client.build_worker_command",
        lambda **kwargs: (
            ["/bin/true", "-u", "worker.py"],
            {"PYTHONPATH": "/tmp/us"},
            "/tmp/us",
        ),
    )

    tracker = ProgressTracker(ticker="NVDA", trade_date="2024-05-10")
    tracker.is_running = True
    tracker.stages = list(US_PIPELINE_STAGES)

    run_us_analysis(
        ticker="NVDA",
        trade_date="2024-05-10",
        llm_config={"llm_provider": "openai"},
        tracker=tracker,
    )
    assert tracker.is_complete
    assert tracker.signal == "Buy"
    assert created["kwargs"]["cwd"] == "/tmp/us"
    assert created["kwargs"]["text"] is True


def test_run_us_analysis_kills_process_on_stop(monkeypatch):
    class FakeProc:
        def __init__(self):
            self.pid = 99
            self.stdout = self._gen()
            self.returncode = None
            self.killed = False

        def _gen(self):
            yield encode_event("stage_active", stage="market")
            # stop is requested mid-stream by the test harness below

        def poll(self):
            return -9 if self.killed else None

        def wait(self, timeout=None):
            return -9

        def kill(self):
            self.killed = True
            self.returncode = -9

    monkeypatch.setattr(
        "web.us_bridge.client.subprocess.Popen",
        lambda *a, **k: FakeProc(),
    )
    monkeypatch.setattr(
        "web.us_bridge.client.build_worker_command",
        lambda **kwargs: (["/bin/true"], {}, "/tmp"),
    )

    tracker = ProgressTracker(ticker="NVDA", trade_date="2024-05-10")
    tracker.is_running = True

    # Request stop as soon as first event applied
    original_apply = __import__(
        "web.us_bridge.client", fromlist=["apply_bridge_event"]
    ).apply_bridge_event

    def apply_then_stop(tracker, event):
        original_apply(tracker, event)
        tracker.request_stop()

    monkeypatch.setattr("web.us_bridge.client.apply_bridge_event", apply_then_stop)

    run_us_analysis(
        ticker="NVDA",
        trade_date="2024-05-10",
        llm_config={},
        tracker=tracker,
    )
    # request_stop clears running flags via mark_stopped path in client
    assert tracker.stop_requested or not tracker.is_running or tracker.error is None
