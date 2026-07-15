"""并行空闲槽位应自动从分析队列开跑，无需等上一只结束。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from web.analysis_queue import QUEUE_SESSION_KEY, AnalysisJob
from web.parallel_runs import (
    ACTIVE_RUNS_KEY,
    FORCE_FILL_KEY,
    can_fill_parallel_slots,
    pop_and_start_queued_jobs,
    request_fill_parallel_slots,
    try_fill_parallel_slots,
)
from web.progress import ProgressTracker


def _running(ticker: str, trade_date: str = "2026-07-15") -> SimpleNamespace:
    return SimpleNamespace(
        ticker=ticker,
        trade_date=trade_date,
        is_running=True,
        is_complete=False,
        error=None,
        market="CN",
    )


def _job(ticker: str, trade_date: str = "2026-07-15") -> AnalysisJob:
    return AnalysisJob(ticker=ticker, trade_date=trade_date, market="CN")


def test_can_fill_when_running_and_queue_has_jobs_with_free_slots():
    session = {
        ACTIVE_RUNS_KEY: [_running("300244")],
        QUEUE_SESSION_KEY: [_job("300253")],
    }
    assert can_fill_parallel_slots(session) is True


def test_cannot_fill_when_slots_full():
    session = {
        ACTIVE_RUNS_KEY: [_running(f"00000{i}") for i in range(3)],
        QUEUE_SESSION_KEY: [_job("300253")],
    }
    assert can_fill_parallel_slots(session) is False


def test_cannot_fill_when_queue_empty():
    session = {
        ACTIVE_RUNS_KEY: [_running("300244")],
        QUEUE_SESSION_KEY: [],
    }
    assert can_fill_parallel_slots(session) is False


def test_cold_session_blocks_fill_when_incomplete_running_on_disk():
    session = {QUEUE_SESSION_KEY: [_job("300253")]}
    incomplete = [{"ticker": "300244", "trade_date": "2026-07-15", "status": "running"}]
    assert can_fill_parallel_slots(session, incomplete_entries=incomplete) is False


def test_force_fill_overrides_incomplete_gate():
    session = {QUEUE_SESSION_KEY: [_job("300253")]}
    request_fill_parallel_slots(session, force=True)
    incomplete = [{"ticker": "300244", "trade_date": "2026-07-15", "status": "running"}]
    assert can_fill_parallel_slots(session, incomplete_entries=incomplete) is True


def test_pop_and_start_fills_multiple_free_slots():
    session = {
        ACTIVE_RUNS_KEY: [_running("300244")],
        QUEUE_SESSION_KEY: [_job("300253"), _job("300785"), _job("002648")],
    }
    started_tickers: list[str] = []

    def begin(job: AnalysisJob) -> ProgressTracker:
        started_tickers.append(job.ticker)
        tracker = ProgressTracker(ticker=job.ticker, trade_date=job.trade_date, market=job.market)
        tracker.is_running = True
        runs = session.setdefault(ACTIVE_RUNS_KEY, [])
        runs.append(tracker)
        return tracker

    started = pop_and_start_queued_jobs(session, begin)
    assert [t.ticker for t in started] == ["300253", "300785"]
    assert started_tickers == ["300253", "300785"]
    remaining = [AnalysisJob.from_mapping(j) for j in session[QUEUE_SESSION_KEY]]
    assert [j.ticker for j in remaining] == ["002648"]


def test_try_fill_clears_force_flag_and_starts():
    session = {QUEUE_SESSION_KEY: [_job("300253")]}
    request_fill_parallel_slots(session, force=True)
    assert session.get(FORCE_FILL_KEY) is True

    def begin(job: AnalysisJob) -> ProgressTracker:
        tracker = ProgressTracker(ticker=job.ticker, trade_date=job.trade_date)
        tracker.is_running = True
        session.setdefault(ACTIVE_RUNS_KEY, []).append(tracker)
        return tracker

    started = try_fill_parallel_slots(session, begin, incomplete_entries=[])
    assert len(started) == 1
    assert started[0].ticker == "300253"
    assert FORCE_FILL_KEY not in session


def test_app_lifecycle_fills_slots_not_only_on_finish():
    """有空槽+队列时应填槽，不能只在 tracker 完成时才 pop_and_start。"""
    app_src = Path("web/app.py").read_text(encoding="utf-8")
    lifecycle = app_src.split("# ── Multi-run lifecycle")[1].split("# ── State routing")[0]
    assert "try_fill_parallel_slots" in lifecycle
    # Old bug: fill was nested under `if need_rerun` (only after a finish).
    assert "if need_rerun:\n        from web.parallel_runs import pop_and_start" not in lifecycle
    assert "if need_rerun:\n        started = pop_and_start" not in lifecycle


def test_continue_queue_requests_force_fill_instead_of_single_advance():
    sidebar_src = Path("web/components/sidebar.py").read_text(encoding="utf-8")
    section = sidebar_src.split("def _render_analysis_queue")[1].split("def max_jobs_configured")[0]
    assert "request_fill_parallel_slots" in section
    assert "force=True" in section
    # Must not manually start only one job via advance_queue + start_analysis.
    assert "advance_queue(" not in section
    assert '["start_analysis"]' not in section
