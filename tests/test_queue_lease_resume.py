"""Queue lease (C) + graceful requeue (B) + startup auto-resume max 2 (A)."""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from tradingagents.analyze_worker.daemon import (
    AnalyzeWorker,
    recover_interrupted_runs,
)
from web import history
from web.analysis_queue import AnalysisJob, AnalysisQueueStore


@pytest.fixture
def store(tmp_path: Path) -> AnalysisQueueStore:
    return AnalysisQueueStore(tmp_path / "analysis_queue.json")


@pytest.fixture
def incomplete_index(tmp_path: Path, monkeypatch):
    index = tmp_path / "incomplete_tasks.json"
    monkeypatch.setattr(history, "_INCOMPLETE_TASKS_FILE", index)
    return index


def _job(
    ticker: str,
    *,
    fresh: bool = True,
    resume_count: int = 0,
    market: str = "CN",
    date: str = "2026-07-15",
) -> AnalysisJob:
    return AnalysisJob(
        ticker=ticker,
        trade_date=date,
        market=market,
        fresh=fresh,
        resume_count=resume_count,
    )


# ── C: lease claim / heartbeat / reclaim ─────────────────────────────────────


def test_claim_next_moves_job_to_lease_not_lost(store: AnalysisQueueStore):
    store.save([_job("300253"), _job("300785")])

    claimed = store.claim_next()
    assert claimed is not None and claimed.ticker == "300253"
    assert [j.ticker for j in store.load()] == ["300785"]
    leases = store.load_leases()
    assert len(leases) == 1
    assert leases[0].job.ticker == "300253"


def test_complete_removes_lease(store: AnalysisQueueStore):
    store.save([_job("300253")])
    job = store.claim_next()
    assert job is not None
    assert store.complete(job.identity()) is True
    assert store.load_leases() == []
    assert store.load() == []


def test_heartbeat_extends_lease(store: AnalysisQueueStore):
    store.save([_job("300253")])
    job = store.claim_next()
    assert job is not None
    before = store.load_leases()[0].heartbeat_at
    time.sleep(0.02)
    assert store.heartbeat(job.identity()) is True
    after = store.load_leases()[0].heartbeat_at
    assert after >= before


def test_reclaim_expired_bumps_resume_and_returns_to_front(store: AnalysisQueueStore):
    store.save([_job("300253", resume_count=0), _job("300785")])
    claimed = store.claim_next()
    assert claimed is not None

    # Force stale heartbeat
    with store.exclusive():
        jobs, leases = store._load_state()
        leases[0] = leases[0].__class__(
            job=leases[0].job,
            lease_id=leases[0].lease_id,
            claimed_at=leases[0].claimed_at,
            heartbeat_at=time.time() - 999,
            owner_pid=leases[0].owner_pid,
        )
        store._save_state(jobs, leases)

    reclaimed, exhausted = store.reclaim_expired(ttl_seconds=60, max_auto_resume=2)
    assert [j.ticker for j in reclaimed] == ["300253"]
    assert reclaimed[0].resume_count == 1
    assert reclaimed[0].fresh is False
    assert exhausted == []
    assert [j.ticker for j in store.load()] == ["300253", "300785"]
    assert store.load_leases() == []


def test_reclaim_exhausted_when_resume_count_at_max(
    store: AnalysisQueueStore, incomplete_index
):
    store.save([_job("300253", resume_count=2)])
    store.claim_next()
    with store.exclusive():
        jobs, leases = store._load_state()
        leases[0] = leases[0].__class__(
            job=leases[0].job,
            lease_id=leases[0].lease_id,
            claimed_at=leases[0].claimed_at,
            heartbeat_at=time.time() - 999,
            owner_pid=leases[0].owner_pid,
        )
        store._save_state(jobs, leases)

    reclaimed, exhausted = store.reclaim_expired(ttl_seconds=60, max_auto_resume=2)
    assert reclaimed == []
    assert [(j.ticker, j.resume_count) for j in exhausted] == [("300253", 2)]
    assert store.load() == []
    assert store.load_leases() == []


def test_release_to_queue_without_bump_for_graceful_stop(store: AnalysisQueueStore):
    store.save([_job("A", resume_count=1), _job("B")])
    a = store.claim_next()
    assert a is not None
    store.release_to_queue([a.identity()], bump_resume=False)
    waiting = store.load()
    assert waiting[0].ticker == "A"
    assert waiting[0].resume_count == 1
    assert waiting[0].fresh is False
    assert [j.ticker for j in waiting] == ["A", "B"]
    assert store.load_leases() == []


# ── A: startup recover orphans ───────────────────────────────────────────────


def test_recover_requeues_running_orphan_under_max(
    store: AnalysisQueueStore, incomplete_index, monkeypatch
):
    history.record_incomplete_task(
        "300253",
        "2026-07-15",
        status="running",
        resume_count=0,
        completed_stages=["market"],
    )

    stats = recover_interrupted_runs(
        store,
        max_auto_resume=2,
        has_checkpoint_fn=lambda ticker, trade_date: True,
    )
    assert stats.requeued == [("300253", "2026-07-15", 1)]
    assert stats.exhausted == []
    jobs = store.load()
    assert len(jobs) == 1
    assert jobs[0].fresh is False
    assert jobs[0].resume_count == 1


def test_recover_marks_exhausted_at_max_resume(
    store: AnalysisQueueStore, incomplete_index, monkeypatch
):
    history.record_incomplete_task(
        "300253",
        "2026-07-15",
        status="running",
        resume_count=2,
        completed_stages=["market"],
    )

    stats = recover_interrupted_runs(
        store,
        max_auto_resume=2,
        has_checkpoint_fn=lambda ticker, trade_date: True,
    )
    assert stats.requeued == []
    assert stats.exhausted == [("300253", "2026-07-15")]
    left = history.get_incomplete_history()
    assert len(left) == 1
    assert left[0]["status"] == "error"
    assert "已达自动续跑上限" in left[0]["error"]
    assert store.load() == []


def test_recover_skips_without_checkpoint(
    store: AnalysisQueueStore, incomplete_index, monkeypatch
):
    history.record_incomplete_task(
        "300253", "2026-07-15", status="running", resume_count=0
    )

    stats = recover_interrupted_runs(
        store,
        max_auto_resume=2,
        has_checkpoint_fn=lambda ticker, trade_date: False,
    )
    assert stats.requeued == []
    assert stats.marked_error == [("300253", "2026-07-15")]
    left = history.get_incomplete_history()
    assert left[0]["status"] == "error"


# ── B: graceful stop requeues in-flight ──────────────────────────────────────


def test_worker_stop_requeues_in_flight_without_bump(store: AnalysisQueueStore):
    store.save([_job("A", resume_count=1), _job("B")])
    started = []
    gate = __import__("threading").Event()

    def run_fn(job, config):
        started.append(job.ticker)
        gate.wait(2.0)

    worker = AnalyzeWorker(
        store=store,
        config={},
        max_workers=1,
        run_fn=run_fn,
        lease_ttl_seconds=600,
        max_auto_resume=2,
    )

    import threading

    t = threading.Thread(
        target=worker.run_forever, kwargs={"poll_seconds": 0.02}, daemon=True
    )
    t.start()
    deadline = time.time() + 2.0
    while not started and time.time() < deadline:
        time.sleep(0.02)
    assert started == ["A"]

    worker.request_stop()
    t.join(3.0)
    assert not t.is_alive()
    gate.set()

    waiting = store.load()
    assert waiting[0].ticker == "A"
    assert waiting[0].resume_count == 1  # no bump on graceful stop
    assert waiting[0].fresh is False
    assert store.load_leases() == []


def test_job_roundtrip_preserves_resume_count():
    raw = _job("600519", fresh=False, resume_count=2).to_dict()
    back = AnalysisJob.from_mapping(raw)
    assert back.resume_count == 2
    assert back.fresh is False


def test_max_auto_resume_env_default():
    from tradingagents.analyze_worker import daemon as d

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("TRADINGAGENTS_MAX_AUTO_RESUME", None)
        assert d._max_auto_resume() == 2
    with patch.dict(os.environ, {"TRADINGAGENTS_MAX_AUTO_RESUME": "1"}):
        assert d._max_auto_resume() == 1
