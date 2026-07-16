"""Test that the background worker claims jobs per-market with separate limits.

CN_MAX_PARALLEL / US_MAX_PARALLEL control how many jobs of each market
the AnalyzeWorker dispatches concurrently.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from tradingagents.analyze_worker.daemon import AnalyzeWorker
from web.analysis_queue import AnalysisJob, AnalysisQueueStore


@pytest.fixture
def store(tmp_path: Path) -> AnalysisQueueStore:
    return AnalysisQueueStore(tmp_path / "analysis_queue.json")


def _cn(ticker: str, date: str = "2026-07-15") -> AnalysisJob:
    return AnalysisJob(ticker=ticker, trade_date=date, market="CN")


def _us(ticker: str, date: str = "2026-07-15") -> AnalysisJob:
    return AnalysisJob(ticker=ticker, trade_date=date, market="US")


# ── claim_next_by_market ────────────────────────────────────────────────────


def test_claim_next_by_market_returns_first_of_market(store: AnalysisQueueStore):
    store.save([_cn("300750"), _us("AAPL"), _cn("000001")])
    job = store.claim_next_by_market("US")
    assert job is not None and job.ticker == "AAPL"
    remaining = store.load()
    assert [j.ticker for j in remaining] == ["300750", "000001"]


def test_claim_next_by_market_none_when_no_unclaimed_of_market(store: AnalysisQueueStore):
    store.save([_cn("300750"), _cn("000001")])
    assert store.claim_next_by_market("US") is None
    assert [j.ticker for j in store.load()] == ["300750", "000001"]


def test_claim_next_by_market_skips_leased_jobs(store: AnalysisQueueStore):
    store.save([_cn("300750"), _us("AAPL"), _us("MSFT")])
    first = store.claim_next()  # claims 300750
    assert first is not None and first.ticker == "300750"

    # claim_next_by_market("US") should skip 300750 (already leased) and claim AAPL
    job = store.claim_next_by_market("US")
    assert job is not None and job.ticker == "AAPL"


def test_claim_next_by_market_with_none_falls_back_to_any(store: AnalysisQueueStore):
    store.save([_cn("300750"), _us("AAPL")])
    job = store.claim_next_by_market(None)
    assert job is not None and job.ticker == "300750"


# ── Worker _claim_batch market-aware ────────────────────────────────────────


def test_claim_batch_mixed_markets_respects_separate_pools(store: AnalysisQueueStore):
    """With CN_MAX_PARALLEL=2 and US_MAX_PARALLEL=1, claim up to 3 jobs.

    Queue: [CN_a, CN_b, US_a, CN_c, US_b]
    Should claim: CN_a, CN_b (2 CN slots), US_a (1 US slot) → only 3.
    """
    store.save([_cn("CN_a"), _cn("CN_b"), _us("US_a"), _cn("CN_c"), _us("US_b")])
    worker = AnalyzeWorker(store=store, config={}, max_workers=5, run_fn=lambda *a: None)

    with _env("CN_MAX_PARALLEL", "2"), _env("US_MAX_PARALLEL", "1"):
        batch = worker._claim_batch(5)

    assert [j.ticker for j in batch] == ["CN_a", "CN_b", "US_a"]
    remaining = [j.ticker for j in store.load()]
    # US_b should still be in queue (US pool full), CN_c remains too
    assert set(remaining) == {"CN_c", "US_b"}


def test_claim_batch_all_cn_fills_cn_only(store: AnalysisQueueStore):
    store.save([_cn("A"), _cn("B"), _cn("C"), _cn("D")])
    worker = AnalyzeWorker(store=store, config={}, max_workers=5, run_fn=lambda *a: None)

    with _env("CN_MAX_PARALLEL", "2"):
        batch = worker._claim_batch(5)

    assert [j.ticker for j in batch] == ["A", "B"]
    assert [j.ticker for j in store.load()] == ["C", "D"]


def test_claim_batch_all_us_fills_us_only(store: AnalysisQueueStore):
    store.save([_us("AAPL"), _us("MSFT"), _us("NVDA")])
    worker = AnalyzeWorker(store=store, config={}, max_workers=5, run_fn=lambda *a: None)

    with _env("US_MAX_PARALLEL", "2"):
        batch = worker._claim_batch(5)

    assert [j.ticker for j in batch] == ["AAPL", "MSFT"]
    assert [j.ticker for j in store.load()] == ["NVDA"]


def test_claim_batch_respects_fifo_within_each_market(store: AnalysisQueueStore):
    """FIFO per market: CN jobs maintain relative order, US jobs maintain
    relative order, but they interleave."""
    store.save([_cn("CN1"), _us("US1"), _cn("CN2"), _us("US2")])
    worker = AnalyzeWorker(store=store, config={}, max_workers=5, run_fn=lambda *a: None)

    with _env("CN_MAX_PARALLEL", "2"), _env("US_MAX_PARALLEL", "2"):
        batch = worker._claim_batch(5)

    # One claim pass: CN has free → claim CN1. CN still free → claim CN2.
    # Then US has free → claim US1. Then US still free → claim US2.
    assert [j.ticker for j in batch] == ["CN1", "CN2", "US1", "US2"]
    assert store.load() == []


def test_claim_batch_does_not_exceed_in_flight_per_market(store: AnalysisQueueStore):
    """If some CN jobs are already in-flight, don't exceed CN_MAX_PARALLEL."""
    store.save([_cn("CN3"), _us("US1"), _us("US2")])
    worker = AnalyzeWorker(store=store, config={}, max_workers=5, run_fn=lambda *a: None)

    # Simulate 2 CN jobs already in flight (e.g., CN1, CN2).
    class FakeFuture:
        def done(self): return False
    worker._in_flight = {
        FakeFuture(): _cn("CN1"),
        FakeFuture(): _cn("CN2"),
    }

    with _env("CN_MAX_PARALLEL", "2"), _env("US_MAX_PARALLEL", "2"):
        batch = worker._claim_batch(5)

    # CN slots full, US slots 2 free → claim US1, US2
    assert [j.ticker for j in batch] == ["US1", "US2"]
    assert [j.ticker for j in store.load()] == ["CN3"]


def test_run_until_drained_with_mixed_markets(store: AnalysisQueueStore):
    """Integration: drain a mixed queue respecting per-market limits."""
    store.save([
        _cn("CN_A"), _us("US_A"), _us("US_B"), _cn("CN_B"),
        _cn("CN_C"), _us("US_C"),
    ])
    started: list[str] = []
    lock = threading.Lock()
    live = 0
    release = threading.Event()

    def run_fn(job, config):
        nonlocal live
        with lock:
            live += 1
            started.append(job.ticker)
        release.wait(2.0)
        with lock:
            live -= 1

    worker = AnalyzeWorker(store=store, config={}, max_workers=4, run_fn=run_fn)

    with _env("CN_MAX_PARALLEL", "2"), _env("US_MAX_PARALLEL", "2"):
        t = threading.Thread(target=worker.run_until_drained, kwargs={"poll_seconds": 0.02})
        t.start()
        time.sleep(0.3)
        release.set()
        t.join(5.0)

    assert not t.is_alive()
    # CN_MAX_PARALLEL=2 → at most 2 CN concurrently
    # US_MAX_PARALLEL=2 → at most 2 US concurrently
    # Total: max 4 concurrent
    assert sorted(started) == ["CN_A", "CN_B", "CN_C", "US_A", "US_B", "US_C"]
    assert store.load() == []


# ── Helpers ─────────────────────────────────────────────────────────────────


class _env:
    """Temporarily set an environment variable within a context."""

    def __init__(self, key: str, value: str):
        self.key = key
        self.value = value
        self._old: str | None = None

    def __enter__(self):
        self._old = os.environ.get(self.key)
        os.environ[self.key] = self.value
        return self

    def __exit__(self, *args):
        if self._old is None:
            os.environ.pop(self.key, None)
        else:
            os.environ[self.key] = self._old
