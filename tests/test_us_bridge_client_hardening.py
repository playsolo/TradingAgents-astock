"""Hardening: stop mid-read, stderr drain, pause SIGSTOP, market gate helpers."""

from __future__ import annotations

from web.components.report_viewer import resolve_report_market
from web.progress import ProgressTracker
from web.us_bridge.client import run_us_analysis
from web.us_bridge.protocol import US_PIPELINE_STAGES, encode_event


def test_request_stop_kills_bridge_pid(monkeypatch):
    killed = []

    def fake_killpg(pid, sig):
        killed.append((pid, sig))

    monkeypatch.setattr("web.progress.os.killpg", fake_killpg, raising=False)
    monkeypatch.setattr("web.progress.os.kill", lambda pid, sig: killed.append((pid, sig)))

    tracker = ProgressTracker(ticker="NVDA", trade_date="2024-05-10")
    tracker.is_running = True
    tracker.bridge_pid = 12345
    tracker.market = "US"

    assert tracker.request_stop() is True
    assert killed  # best-effort kill attempted


def test_run_us_analysis_discards_complete_after_stop(monkeypatch):
    class FakeProc:
        def __init__(self):
            self.pid = 77
            self._lines = [
                encode_event("stage_active", stage="market"),
                encode_event(
                    "complete",
                    signal="Buy",
                    state={"final_trade_decision": "Buy"},
                ),
            ]
            self._i = 0
            self.stdout = self
            self.stderr = None
            self.returncode = None
            self.killed = False

        def readline(self):
            if self._i >= len(self._lines):
                self.returncode = 0
                return ""
            line = self._lines[self._i]
            self._i += 1
            return line

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode or 0

        def kill(self):
            self.killed = True
            self.returncode = -9

        def terminate(self):
            self.kill()

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
    tracker.stages = list(US_PIPELINE_STAGES)

    # First event applied; then stop so complete is discarded
    from web.us_bridge import client as client_mod

    orig = client_mod.apply_bridge_event

    def apply_and_stop(tracker, event):
        if event.get("event") == "stage_active":
            tracker.request_stop()
            return
        orig(tracker, event)

    monkeypatch.setattr(client_mod, "apply_bridge_event", apply_and_stop)

    run_us_analysis(
        ticker="NVDA",
        trade_date="2024-05-10",
        llm_config={},
        tracker=tracker,
    )
    assert not tracker.is_complete
    assert tracker.signal == ""


def test_resolve_report_market_prefers_ticker_not_sidebar():
    assert resolve_report_market("NVDA", sidebar_market="CN", tracker_market="US") == "US"
    assert resolve_report_market("600519", sidebar_market="US", tracker_market=None) == "CN"
    assert resolve_report_market("AAPL", sidebar_market="CN", tracker_market=None) == "US"
