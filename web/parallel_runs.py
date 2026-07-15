"""Parallel analysis run manager — replaces the single-tracker model.

Stores a list of active trackers + the focused tracker ID in Streamlit session_state.
Thread-safe access methods so the runner threads and UI don't race.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, MutableMapping, Optional

from web.analysis_queue import AnalysisJob, advance_queue, queue_snapshot
from web.progress import ProgressTracker

# ── session_state keys ───────────────────────────────────────────────────────

ACTIVE_RUNS_KEY = "_parallel_active_runs"
FOCUSED_RUN_KEY = "_parallel_focused_ticker"
FORCE_FILL_KEY = "_force_fill_parallel_slots"

# ── Config (env override) ────────────────────────────────────────────────────

# Max concurrent analysis runs across the entire process (CN + US combined).
# Can be overridden via environment variable.
_DEFAULT_MAX_RUNS = 3


def max_runs() -> int:
    """Return the global slot cap. Only used for enqueue gating."""
    return int(os.environ.get("TRADINGAGENTS_MAX_PARALLEL", str(_DEFAULT_MAX_RUNS)))


# ── Active trackers snapshot ─────────────────────────────────────────────────


@dataclass
class RunSnapshot:
    """Immutable snapshot of one run for UI rendering."""

    ticker: str
    trade_date: str
    market: str
    is_running: bool
    is_complete: bool
    is_paused: bool
    error: str | None
    completed_stages: list[str]
    total_stages: int
    llm_calls: int
    tool_calls: int
    tokens_in: int
    tokens_out: int
    elapsed: float
    final_signal: str
    current_stage: str
    stage_reports: dict[str, str]


def _lock_for(session: MutableMapping[str, Any]) -> threading.RLock:
    """Return a per-session lock for modifying active runs."""
    if "_parallel_runs_lock" not in session:
        session["_parallel_runs_lock"] = threading.RLock()
    return session["_parallel_runs_lock"]


def active_runs(session: MutableMapping[str, Any]) -> list[ProgressTracker]:
    """Return the list of active ProgressTracker objects."""
    return list(session.get(ACTIVE_RUNS_KEY, []))


def running_count(session: MutableMapping[str, Any]) -> int:
    """Count trackers that are still running."""
    return sum(1 for t in active_runs(session) if t.is_running and not t.is_complete and not t.error)


def slots_available(session: MutableMapping[str, Any]) -> int:
    """How many more slots are free to start new jobs."""
    return max(0, max_runs() - running_count(session))


def has_running(session: MutableMapping[str, Any]) -> bool:
    """True when at least one tracker is still running."""
    return running_count(session) > 0


def focused_ticker(session: MutableMapping[str, Any]) -> str | None:
    """Return the ticker of the focused/displayed run, if any."""
    return session.get(FOCUSED_RUN_KEY)


def set_focused_ticker(session: MutableMapping[str, Any], ticker: str) -> None:
    session[FOCUSED_RUN_KEY] = ticker


def add_tracker(session: MutableMapping[str, Any], tracker: ProgressTracker) -> None:
    """Add a tracker to the active list under lock."""
    with _lock_for(session):
        runs = session.get(ACTIVE_RUNS_KEY)
        if runs is None:
            runs = []
            session[ACTIVE_RUNS_KEY] = runs
        # Remove any stale entry for the same ticker+date (race recovery).
        runs[:] = [t for t in runs if not (t.ticker == tracker.ticker and t.trade_date == tracker.trade_date)]
        runs.append(tracker)
        # Auto-focus the newest run.
        session[FOCUSED_RUN_KEY] = tracker.ticker


def remove_finished_tracker(session: MutableMapping[str, Any], ticker: str, trade_date: str) -> int:
    """Remove a finished tracker and return how many still remain in the list."""
    with _lock_for(session):
        runs = session.get(ACTIVE_RUNS_KEY, [])
        runs[:] = [
            t for t in runs
            if not (t.ticker == ticker and t.trade_date == trade_date)
        ]
        # Keep the list even if empty.
        session[ACTIVE_RUNS_KEY] = runs
        return len(runs)


def take_snapshots(session: MutableMapping[str, Any]) -> list[RunSnapshot]:
    """Take a consistent snapshot of all active runs (thread-safe)."""
    result: list[RunSnapshot] = []
    with _lock_for(session):
        runs = list(session.get(ACTIVE_RUNS_KEY, []))
        for t in runs:
            result.append(
                RunSnapshot(
                    ticker=t.ticker,
                    trade_date=t.trade_date,
                    market=t.market,
                    is_running=t.is_running,
                    is_complete=t.is_complete,
                    is_paused=t.is_paused,
                    error=t.error,
                    completed_stages=list(t.completed_stages),
                    total_stages=len(t.stages) if t.stages else 0,
                    llm_calls=t.llm_calls,
                    tool_calls=t.tool_calls,
                    tokens_in=t.tokens_in,
                    tokens_out=t.tokens_out,
                    elapsed=t.elapsed,
                    final_signal=t.signal,
                    current_stage=t.current_stage,
                    stage_reports=dict(t.stage_reports),
                )
            )
    return result


def stop_run_by_ticker(
    session: MutableMapping[str, Any],
    ticker: str,
) -> bool:
    """Request stop on a specific run. Returns True if the run was found and stopped."""
    with _lock_for(session):
        for t in session.get(ACTIVE_RUNS_KEY, []):
            if t.ticker == ticker and t.is_running:
                t.request_stop()
                return True
    return False


def pause_run_by_ticker(
    session: MutableMapping[str, Any],
    ticker: str,
) -> bool:
    """Pause a specific run. Returns True on success."""
    with _lock_for(session):
        for t in session.get(ACTIVE_RUNS_KEY, []):
            if t.ticker == ticker and t.is_running and not t.is_paused:
                return t.pause()
    return False


def resume_run_by_ticker(
    session: MutableMapping[str, Any],
    ticker: str,
) -> bool:
    """Resume a specific run. Returns True on success."""
    with _lock_for(session):
        for t in session.get(ACTIVE_RUNS_KEY, []):
            if t.ticker == ticker and t.is_paused:
                return t.resume()
    return False


# ── Queue / slot helpers ──────────────────────────────────────────────────────


def pop_and_start_queued_jobs(
    session: MutableMapping[str, Any],
    begin_analysis_fn: Callable[[AnalysisJob], ProgressTracker],
) -> list[ProgressTracker]:
    """Pop as many jobs as slots are free and start them.

    Returns the list of newly started trackers (possibly empty).
    """
    started: list[ProgressTracker] = []
    while slots_available(session) > 0:
        job = advance_queue(session)
        if job is None:
            break
        try:
            tracker = begin_analysis_fn(job)
            started.append(tracker)
        except Exception:
            # Put the job back to front of queue on failure.
            from web.analysis_queue import prepend_job
            prepend_job(session, job)
            break
    return started


def request_fill_parallel_slots(
    session: MutableMapping[str, Any],
    *,
    force: bool = False,
) -> None:
    """Ask the next lifecycle pass to fill free slots from the queue.

    ``force=True`` overrides the disk incomplete-run gate (侧栏「继续队列」).
    """
    if force:
        session[FORCE_FILL_KEY] = True


def can_fill_parallel_slots(
    session: MutableMapping[str, Any],
    *,
    incomplete_entries: list[Any] | None = None,
) -> bool:
    """True when free slots + queued jobs should be started now."""
    if slots_available(session) <= 0:
        return False
    if not queue_snapshot(session):
        return False
    if has_running(session):
        return True
    if session.get(FORCE_FILL_KEY):
        return True
    from web.analysis_queue import has_blocking_incomplete_run

    return not has_blocking_incomplete_run(incomplete_entries)


def try_fill_parallel_slots(
    session: MutableMapping[str, Any],
    begin_analysis_fn: Callable[[AnalysisJob], ProgressTracker],
    *,
    incomplete_entries: list[Any] | None = None,
) -> list[ProgressTracker]:
    """Fill free parallel slots from the queue when allowed; clear force flag."""
    if not can_fill_parallel_slots(session, incomplete_entries=incomplete_entries):
        return []
    session.pop(FORCE_FILL_KEY, None)
    return pop_and_start_queued_jobs(session, begin_analysis_fn)


def stop_all_runs(session: MutableMapping[str, Any]) -> None:
    """Request stop on every active run."""
    with _lock_for(session):
        for t in session.get(ACTIVE_RUNS_KEY, []):
            if t.is_running:
                t.request_stop()


def pause_all_runs(session: MutableMapping[str, Any]) -> None:
    """Pause every running run."""
    with _lock_for(session):
        for t in session.get(ACTIVE_RUNS_KEY, []):
            if t.is_running and not t.is_paused:
                t.pause()
