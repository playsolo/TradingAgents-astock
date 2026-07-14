"""mootdx singleton reset must not race in-flight RPCs."""

from __future__ import annotations

import threading
import time


def test_reset_waits_for_in_flight_session(monkeypatch):
    from tradingagents.dataflows import a_stock

    events = {"in_session": threading.Event(), "reset_done": threading.Event()}
    order: list[str] = []

    class _FakeClient:
        def close(self):
            order.append("close")

    fake = _FakeClient()
    a_stock._mootdx_client = fake

    def _slow_rpc():
        with a_stock._mootdx_client_session() as client:
            assert client is fake
            order.append("rpc_start")
            events["in_session"].set()
            time.sleep(0.15)
            order.append("rpc_end")

    def _reset():
        events["in_session"].wait(timeout=2)
        a_stock._reset_mootdx_client()
        order.append("reset_return")
        events["reset_done"].set()

    t_rpc = threading.Thread(target=_slow_rpc)
    t_reset = threading.Thread(target=_reset)
    t_rpc.start()
    t_reset.start()
    t_rpc.join(timeout=2)
    t_reset.join(timeout=2)
    assert events["reset_done"].is_set()
    # Reset/close must happen only after the session releases the lock.
    assert order.index("rpc_end") < order.index("close")
    assert order.index("rpc_end") < order.index("reset_return")
