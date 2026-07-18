"""Spawn the US TradingAgents worker and mirror NDJSON progress into ProgressTracker."""

from __future__ import annotations

import logging
import os
import selectors
import signal
import subprocess
import time
from typing import Any, Iterable, TextIO

logger = logging.getLogger(__name__)

from web.history import clear_incomplete_task, record_incomplete_task
from web.progress import ProgressTracker
from web.us_bridge.config import build_worker_command
from web.us_bridge.protocol import (
    US_PIPELINE_STAGES,
    apply_bridge_event,
    parse_event_line,
)

_READ_TIMEOUT_S = 0.25


def consume_bridge_stdout(
    tracker: ProgressTracker,
    lines: Iterable[str],
) -> None:
    for line in lines:
        tracker.wait_if_paused()
        if tracker.stop_requested:
            return
        event = parse_event_line(line)
        if event is None:
            continue
        apply_bridge_event(tracker, event)
        if event.get("event") in {"complete", "error"}:
            return
        record_incomplete_task(
            tracker.ticker,
            tracker.trade_date,
            status="paused" if tracker.is_paused else "running",
            completed_stages=tracker.completed_stages,
        )


def _kill_process_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name != "nt" and proc.pid:
            try:
                os.killpg(proc.pid, signal.SIGCONT)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            if os.name != "nt" and proc.pid:
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def _handle_event(tracker: ProgressTracker, event: dict[str, Any]) -> str | None:
    """Apply event; return 'complete' | 'error' | None."""
    if tracker.stop_requested:
        return "stopped"
    apply_bridge_event(tracker, event)
    name = event.get("event")
    if name == "complete":
        if tracker.stop_requested:
            return "stopped"
        clear_incomplete_task(tracker.ticker, tracker.trade_date)
        return "complete"
    if name == "error":
        record_incomplete_task(
            tracker.ticker,
            tracker.trade_date,
            status="error",
            error=str(event.get("message") or ""),
            completed_stages=tracker.completed_stages,
        )
        return "error"
    record_incomplete_task(
        tracker.ticker,
        tracker.trade_date,
        status="paused" if tracker.is_paused else "running",
        completed_stages=tracker.completed_stages,
    )
    return None


def _readline(stream: TextIO | Any) -> str:
    if hasattr(stream, "readline"):
        return stream.readline() or ""
    try:
        return next(stream)
    except StopIteration:
        return ""


def _stdout_ready(proc: subprocess.Popen, timeout: float) -> bool:
    """Return True if stdout has data, or if we should fall back to blocking read."""
    if proc.stdout is None:
        return False
    # Fake/test streams without fileno → treat as ready so readline/iterator runs.
    try:
        fd = proc.stdout.fileno()
    except (AttributeError, OSError, ValueError):
        return True
    try:
        sel = selectors.DefaultSelector()
        sel.register(fd, selectors.EVENT_READ)
        try:
            events = sel.select(timeout)
        finally:
            sel.close()
        return bool(events)
    except Exception:
        return True


def run_us_analysis(
    *,
    ticker: str,
    trade_date: str,
    llm_config: dict[str, Any],
    tracker: ProgressTracker,
) -> None:
    """Run the US pipeline in a subprocess; update *tracker* as events arrive."""
    tracker.ticker = ticker
    tracker.trade_date = trade_date
    tracker.market = "US"
    tracker.stages = list(US_PIPELINE_STAGES)
    tracker.is_running = True
    tracker.mark_stage_active(US_PIPELINE_STAGES[0]["id"])
    record_incomplete_task(
        ticker,
        trade_date,
        status="running",
        completed_stages=tracker.completed_stages,
    )

    # Bug D: pre-flight provider health check. The upstream TradingAgents
    # does not understand our ``fallback_chain``; if the operator's
    # primary provider is unhealthy (e.g. expired key) we'd otherwise
    # spawn a subprocess that immediately 401s. Probe here and swap the
    # primary to the first healthy fallback *before* we build the cmd.
    from web.us_bridge.health import choose_provider_for_us_bridge

    chosen = choose_provider_for_us_bridge(
        llm_provider=llm_config.get("llm_provider") or "",
        api_key=None,  # let health module read from the daemon's env
        base_url=llm_config.get("backend_url"),
        deep_think_llm=llm_config.get("deep_think_llm") or "",
        quick_think_llm=llm_config.get("quick_think_llm") or "",
        fallback_chain=llm_config.get("fallback_chain") or [],
    )
    if chosen["fell_back"]:
        logger.warning(
            "US bridge fell back: %s -> %s/%s for %s",
            llm_config.get("llm_provider"),
            chosen["llm_provider"],
            chosen["deep_think_llm"],
            ticker,
        )
    # Mutate a copy of llm_config so build_worker_command sees the chosen
    # provider; downstream report provenance (Bug C) reads from this same
    # dict via config passed in by the runner.
    effective_llm_config = dict(llm_config)
    effective_llm_config["llm_provider"] = chosen["llm_provider"]
    effective_llm_config["deep_think_llm"] = chosen["deep_think_llm"]
    effective_llm_config["quick_think_llm"] = chosen["quick_think_llm"]

    cmd, env, cwd = build_worker_command(
        ticker=ticker,
        trade_date=trade_date,
        llm_config=effective_llm_config,
    )

    popen_kwargs: dict[str, Any] = {
        "cwd": str(cwd),
        "env": env,
        "stdout": subprocess.PIPE,
        # Avoid stderr pipe deadlock — diagnostics still go to parent console.
        "stderr": subprocess.DEVNULL,
        "text": True,
        "bufsize": 1,
    }
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **popen_kwargs)
    tracker.bridge_pid = getattr(proc, "pid", None)

    try:
        while True:
            tracker.wait_if_paused()
            if tracker.stop_requested:
                _kill_process_tree(proc)
                clear_incomplete_task(ticker, trade_date)
                tracker.mark_stopped()
                return

            ready = _stdout_ready(proc, _READ_TIMEOUT_S)
            if not ready:
                code = proc.poll()
                if code is not None:
                    if tracker.stop_requested:
                        clear_incomplete_task(ticker, trade_date)
                        tracker.mark_stopped()
                        return
                    if not tracker.is_complete and not tracker.error:
                        msg = f"美股子进程异常退出 (code={code})"
                        tracker.mark_error(msg)
                        record_incomplete_task(
                            ticker,
                            trade_date,
                            status="error",
                            error=msg,
                            completed_stages=tracker.completed_stages,
                        )
                    return
                continue

            line = _readline(proc.stdout) if proc.stdout is not None else ""
            if line:
                if tracker.stop_requested:
                    _kill_process_tree(proc)
                    clear_incomplete_task(ticker, trade_date)
                    tracker.mark_stopped()
                    return
                event = parse_event_line(line)
                if event is None:
                    continue
                terminal = _handle_event(tracker, event)
                if terminal == "stopped":
                    _kill_process_tree(proc)
                    clear_incomplete_task(ticker, trade_date)
                    tracker.mark_stopped()
                    return
                if terminal in {"complete", "error"}:
                    return
                continue

            code = proc.poll()
            if code is not None:
                if tracker.stop_requested:
                    clear_incomplete_task(ticker, trade_date)
                    tracker.mark_stopped()
                    return
                if not tracker.is_complete and not tracker.error:
                    msg = f"美股子进程异常退出 (code={code})"
                    tracker.mark_error(msg)
                    record_incomplete_task(
                        ticker,
                        trade_date,
                        status="error",
                        error=msg,
                        completed_stages=tracker.completed_stages,
                    )
                return
            time.sleep(0.05)
    finally:
        tracker.bridge_pid = None
        if proc.poll() is None:
            _kill_process_tree(proc)
