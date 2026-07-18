"""analyze daemon：孤儿 running 回收 + 空槽填充（FIFO、并发上限）。"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from tradingagents.analyze_worker import daemon as daemon_mod
from tradingagents.analyze_worker.daemon import AnalyzeWorker, reconcile_orphan_runs
from web.analysis_queue import AnalysisJob, AnalysisQueueStore


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


@pytest.fixture
def store(tmp_path: Path) -> AnalysisQueueStore:
    return AnalysisQueueStore(tmp_path / "analysis_queue.json")


def _job(ticker: str) -> AnalysisJob:
    return AnalysisJob(ticker=ticker, trade_date="2026-07-15", market="CN")


def test_reconcile_marks_running_orphans_as_error():
    active = [
        {"ticker": "300244", "trade_date": "2026-07-15", "status": "running"},
        {"ticker": "300253", "trade_date": "2026-07-15", "status": "error"},
        {"ticker": "300785", "trade_date": "2026-07-15", "status": "paused"},
    ]
    marked: list[tuple] = []

    orphaned = reconcile_orphan_runs(
        list_active_fn=lambda: active,
        mark_error_fn=lambda t, d, msg: marked.append((t, d, msg)),
    )

    assert orphaned == [("300244", "2026-07-15")]
    assert marked and marked[0][0] == "300244"


def test_claim_batch_respects_free_slots_and_fifo(store: AnalysisQueueStore):
    store.save([_job("A"), _job("B"), _job("C")])
    worker = AnalyzeWorker(store=store, config={}, max_workers=3, run_fn=lambda *a: None)

    # _claim_batch ignores free_slots, claims up to per-market cap (3).
    with _env("CN_MAX_PARALLEL", "2"):
        batch = worker._claim_batch(99)
    assert [j.ticker for j in batch] == ["A", "B"]
    assert [j.ticker for j in store.load()] == ["C"]


def test_claim_batch_stops_when_queue_empty(store: AnalysisQueueStore):
    store.save([_job("A")])
    worker = AnalyzeWorker(store=store, config={}, max_workers=3, run_fn=lambda *a: None)
    batch = worker._claim_batch(5)
    assert [j.ticker for j in batch] == ["A"]


def test_run_pending_drains_queue_within_concurrency(store: AnalysisQueueStore):
    store.save([_job(t) for t in ["A", "B", "C", "D", "E"]])

    max_concurrent = 0
    lock = threading.Lock()
    live = 0
    started: list[str] = []
    release = threading.Event()

    def run_fn(job, config):
        nonlocal live, max_concurrent
        with lock:
            live += 1
            started.append(job.ticker)
            max_concurrent = max(max_concurrent, live)
        release.wait(2.0)
        with lock:
            live -= 1

    worker = AnalyzeWorker(store=store, config={}, max_workers=2, run_fn=run_fn)

    t = threading.Thread(target=worker.run_until_drained, kwargs={"poll_seconds": 0.02})
    t.start()
    # Let a couple of batches pick up jobs, then release them all.
    time.sleep(0.3)
    release.set()
    t.join(5.0)

    assert not t.is_alive()
    assert sorted(started) == ["A", "B", "C", "D", "E"]
    assert max_concurrent <= 2
    assert store.load() == []


def test_default_run_fn_is_executor_run_one_job():
    worker = AnalyzeWorker(store=AnalysisQueueStore(Path("/tmp/x.json")), config={})
    from tradingagents.analyze_worker.executor import run_one_job

    assert worker.run_fn is run_one_job


def test_daemon_worker_lock_path():
    assert daemon_mod.WORKER_LOCK_PATH.name == "analyze.worker.lock"


# ---------------------------------------------------------------------------
# Bug B regression: AnalyzeWorker must pick up admin-config changes
# (provider / model / fallback_chain) on every job, not freeze the config
# dict at startup. Previously ``main()`` passed ``config=build_worker_config()``
# once and re-used the same dict for the lifetime of the process, which
# meant an admin saving a new model_config.json while jobs were already in
# flight kept using the *old* model until the daemon restarted.
# ---------------------------------------------------------------------------


def test_worker_uses_fresh_config_per_job(store: AnalysisQueueStore):
    """Each job must see the config returned by ``config_provider`` at
    *run-time*, not the value captured at worker construction. We swap the
    provider between two jobs to prove the second job sees the new provider.
    """
    store.save([_job("A"), _job("B")])

    captured: list[dict] = []
    job_ticker_to_provider = {
        "A": "deepseek",  # what the provider returns when job A claims
        "B": "minimax",   # admin "saves" between A and B; provider now returns minimax
    }

    def provider():
        # Decide based on the most-recently-claimed job: the worker
        # signals ``current_job`` via an event. We keep it simple by
        # returning based on a mutable state set by run_fn.
        target = current_target["ticker"]
        p = job_ticker_to_provider[target]
        return {"llm_provider": p, "deep_think_llm": f"{p}-model"}

    current_target = {"ticker": "A"}

    def run_fn(job, config):
        # Simulate "admin saves between A and B": the run_fn for A records
        # what it got, then flips the target for the next claim.
        captured.append({"ticker": job.ticker, "provider": config["llm_provider"]})
        current_target["ticker"] = "B"

    worker = AnalyzeWorker(
        store=store,
        config_provider=provider,
        max_workers=1,
        run_fn=run_fn,
    )

    t = threading.Thread(target=worker.run_until_drained, kwargs={"poll_seconds": 0.02})
    t.start()
    t.join(5.0)

    assert not t.is_alive()
    assert captured == [
        {"ticker": "A", "provider": "deepseek"},
        {"ticker": "B", "provider": "minimax"},
    ], (
        "Worker must call config_provider for each job so admin-config "
        "changes between jobs take effect immediately."
    )


def test_worker_legacy_config_kwarg_still_supported(store: AnalysisQueueStore):
    """Existing callers that pass a plain ``config`` dict (tests, ad-hoc
    scripts) must keep working — they get a provider that always returns
    the same dict. This guards against a refactor breaking every test
    that constructs ``AnalyzeWorker(store=..., config={...})``."""
    store.save([_job("A")])

    captured: list[dict] = []

    def run_fn(job, config):
        captured.append(config)

    worker = AnalyzeWorker(
        store=store,
        config={"llm_provider": "deepseek"},
        max_workers=1,
        run_fn=run_fn,
    )
    t = threading.Thread(target=worker.run_until_drained, kwargs={"poll_seconds": 0.02})
    t.start()
    t.join(5.0)

    assert len(captured) == 1
    assert captured[0]["llm_provider"] == "deepseek"
