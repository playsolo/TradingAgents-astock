"""Test that CN and US stocks use separate parallel pools.

CN_MAX_PARALLEL=3 + US_MAX_PARALLEL=3 means up to 6 concurrent runs,
not competing across markets.
"""

from __future__ import annotations

from types import SimpleNamespace

from web.analysis_queue import QUEUE_SESSION_KEY, AnalysisJob
from web.parallel_runs import (
    ACTIVE_RUNS_KEY,
    cn_max_runs,
    us_max_runs,
    running_count,
    slots_available,
    pop_and_start_queued_jobs,
)
from web.progress import ProgressTracker


def _run(ticker: str, trade_date: str = "2026-07-15", market: str = "CN") -> SimpleNamespace:
    return SimpleNamespace(
        ticker=ticker,
        trade_date=trade_date,
        is_running=True,
        is_complete=False,
        error=None,
        market=market,
    )


def _job(ticker: str, trade_date: str = "2026-07-15", market: str = "CN") -> AnalysisJob:
    return AnalysisJob(ticker=ticker, trade_date=trade_date, market=market)


# ── Default config tests ─────────────────────────────────────────────────────

def test_cn_and_us_each_default_to_3():
    assert cn_max_runs() == 3
    assert us_max_runs() == 3


def test_env_overrides_cn_and_us_independently(monkeypatch):
    monkeypatch.setenv("CN_MAX_PARALLEL", "5")
    monkeypatch.setenv("US_MAX_PARALLEL", "2")
    assert cn_max_runs() == 5
    assert us_max_runs() == 2


# ── running_count / slots_available per market ──────────────────────────────

def test_running_count_by_market():
    session = {
        ACTIVE_RUNS_KEY: [
            _run("AAPL", market="US"),
            _run("NVDA", market="US"),
            _run("300750", market="CN"),
            _run(_job("000001", market="CN").ticker, market="CN"),  # will be marked complete
        ],
    }
    # Mark the last as complete so it doesn't count
    session[ACTIVE_RUNS_KEY][3].is_complete = True
    session[ACTIVE_RUNS_KEY][3].is_running = False

    assert running_count(session, market="CN") == 1
    assert running_count(session, market="US") == 2
    assert running_count(session) == 3  # total


def test_slots_available_by_market():
    session = {
        ACTIVE_RUNS_KEY: [
            _run("AAPL", market="US"),
            _run("NVDA", market="US"),
            _run("300750", market="CN"),
        ],
    }
    assert slots_available(session, market="CN") == 2  # 3 - 1
    assert slots_available(session, market="US") == 1   # 3 - 2
    # Total: the global cap is still 3 for backward compat
    assert slots_available(session) == 0  # 3 - 3


def test_slots_available_full_cn_only():
    """3 CN running, 0 US — CN slots full, US slots free."""
    session = {
        ACTIVE_RUNS_KEY: [
            _run("300750", market="CN"),
            _run("000001", market="CN"),
            _run("002648", market="CN"),
        ],
    }
    assert slots_available(session, market="CN") == 0
    assert slots_available(session, market="US") == 3
    assert slots_available(session) == 0  # still gated by old total cap


def test_slots_available_empty():
    session: dict = {}
    assert slots_available(session, market="CN") == 3
    assert slots_available(session, market="US") == 3


# ── pop_and_start_queued_jobs market-aware ──────────────────────────────────

def test_pop_and_start_respects_cn_and_us_pools():
    """CN jobs fill CN slots; US jobs fill US slots."""
    session = {
        ACTIVE_RUNS_KEY: [
            _run("AAPL", market="US"),
            _run("NVDA", market="US"),
            _run("300750", market="CN"),
        ],
        QUEUE_SESSION_KEY: [
            _job("MSFT", market="US"),
            _job("002648", market="CN"),
            _job("AMZN", market="US"),
            _job("000001", market="CN"),
            _job("300919", market="CN"),
        ],
    }
    # 1 US slot free, 2 CN slots free → should start 3 jobs total
    started_tickers: list[str] = []

    def begin(job: AnalysisJob) -> ProgressTracker:
        started_tickers.append(job.ticker)
        tracker = ProgressTracker(ticker=job.ticker, trade_date=job.trade_date, market=job.market)
        tracker.is_running = True
        session.setdefault(ACTIVE_RUNS_KEY, []).append(tracker)
        return tracker

    started = pop_and_start_queued_jobs(session, begin)
    # MSFT (US) fills the 1 US slot.  Then 002648 (CN) fills 1 of 2 CN slots.
    # Next: AMZN (US) has 0 US slots → skip.  000001 (CN) fills the last CN slot.
    # Then 300919 (CN) has 0 CN slots → skip.  AMZN (waiting for US slot) and 300919 remain.
    assert [t.ticker for t in started] == ["MSFT", "002648", "000001"]
    assert started_tickers == ["MSFT", "002648", "000001"]
    remaining = [AnalysisJob.from_mapping(j) for j in session[QUEUE_SESSION_KEY]]
    assert [j.ticker for j in remaining] == ["AMZN", "300919"]


def test_pop_and_start_skips_market_with_no_free_slots():
    """All US slots full; the first US job stays in queue, CN jobs start."""
    session = {
        ACTIVE_RUNS_KEY: [
            _run("AAPL", market="US"),
            _run("NVDA", market="US"),
            _run("AMZN", market="US"),
            _run("300750", market="CN"),
        ],
        QUEUE_SESSION_KEY: [
            _job("MSFT", market="US"),
            _job("002648", market="CN"),
            _job("000001", market="CN"),
        ],
    }
    # US 0 slots, CN 2 slots — should start 002648 and 000001 only
    started_tickers: list[str] = []

    def begin(job: AnalysisJob) -> ProgressTracker:
        started_tickers.append(job.ticker)
        tracker = ProgressTracker(ticker=job.ticker, trade_date=job.trade_date, market=job.market)
        tracker.is_running = True
        session.setdefault(ACTIVE_RUNS_KEY, []).append(tracker)
        return tracker

    started = pop_and_start_queued_jobs(session, begin)
    assert [t.ticker for t in started] == ["002648", "000001"]
    assert started_tickers == ["002648", "000001"]
    remaining = [AnalysisJob.from_mapping(j) for j in session[QUEUE_SESSION_KEY]]
    assert [j.ticker for j in remaining] == ["MSFT"]


def test_pop_and_start_with_failure_still_market_aware():
    """When begin_analysis_fn raises, the job goes back to queue front."""
    session = {
        ACTIVE_RUNS_KEY: [
            _run("AAPL", market="US"),
            _run("NVDA", market="US"),
        ],
        QUEUE_SESSION_KEY: [
            _job("MSFT", market="US"),
            _job("002648", market="CN"),
        ],
    }
    fail_once = True

    def begin(job: AnalysisJob) -> ProgressTracker:
        nonlocal fail_once
        if fail_once and job.market == "CN":
            fail_once = False
            raise RuntimeError("simulated failure")
        started_tickers.append(job.ticker)
        tracker = ProgressTracker(ticker=job.ticker, trade_date=job.trade_date, market=job.market)
        tracker.is_running = True
        session.setdefault(ACTIVE_RUNS_KEY, []).append(tracker)
        return tracker

    started_tickers: list[str] = []
    started = pop_and_start_queued_jobs(session, begin)
    # MSFT starts fine, 002648 fails (prepended back), loop breaks
    assert [t.ticker for t in started] == ["MSFT"]
    remaining = [AnalysisJob.from_mapping(j) for j in session[QUEUE_SESSION_KEY]]
    assert [j.ticker for j in remaining] == ["002648"]
