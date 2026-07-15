"""跨进程安全的队列 claim / append（flock 包裹读-改-写）。"""

from __future__ import annotations

from pathlib import Path

import pytest

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


def _job(ticker: str, date: str = "2026-07-15", market: str = "CN") -> AnalysisJob:
    return AnalysisJob(ticker=ticker, trade_date=date, market=market)


def test_claim_next_pops_fifo_and_persists(store: AnalysisQueueStore):
    store.save([_job("300253"), _job("300785"), _job("002648")])

    first = store.claim_next()
    assert first is not None and first.ticker == "300253"

    remaining = store.load()
    assert [j.ticker for j in remaining] == ["300785", "002648"]


def test_claim_next_returns_none_when_empty(store: AnalysisQueueStore):
    assert store.claim_next() is None
    store.save([])
    assert store.claim_next() is None


def test_claim_next_drains_all_then_none(store: AnalysisQueueStore):
    store.save([_job("300253"), _job("300785")])
    assert store.claim_next().ticker == "300253"
    assert store.claim_next().ticker == "300785"
    assert store.claim_next() is None


def test_append_atomic_reads_disk_and_dedupes(store: AnalysisQueueStore):
    store.save([_job("300253")])
    added = store.append_atomic([_job("300253"), _job("300785")])
    assert added == 1
    assert [j.ticker for j in store.load()] == ["300253", "300785"]


def test_append_atomic_clears_incomplete_for_queued_jobs(
    store: AnalysisQueueStore, incomplete_index
):
    history.record_incomplete_task(
        "300253", "2026-07-15", status="error", error="worker 重启"
    )
    history.record_incomplete_task(
        "300785", "2026-07-15", status="error", error="timeout"
    )
    history.record_incomplete_task(
        "002648", "2026-07-15", status="running", error=""
    )

    added = store.append_atomic([_job("300253"), _job("300785")])
    assert added == 2

    left = {(e["ticker"], e["status"]) for e in history.get_incomplete_history()}
    assert left == {("002648", "running")}


def test_append_atomic_clears_incomplete_when_already_queued(
    store: AnalysisQueueStore, incomplete_index
):
    """去重未新增条目时，只要身份已在队列，仍应去掉未完成残留。"""
    store.save([_job("300253")])
    history.record_incomplete_task(
        "300253", "2026-07-15", status="error", error="worker 重启"
    )

    added = store.append_atomic([_job("300253")])
    assert added == 0
    assert history.get_incomplete_history() == []


def test_append_atomic_exclude_does_not_clear_incomplete(
    store: AnalysisQueueStore, incomplete_index
):
    history.record_incomplete_task(
        "300253", "2026-07-15", status="running", error=""
    )
    added = store.append_atomic(
        [_job("300253"), _job("300785")],
        exclude={("CN", "300253", "2026-07-15")},
    )
    assert added == 1
    assert [j.ticker for j in store.load()] == ["300785"]
    left = history.get_incomplete_history()
    assert len(left) == 1
    assert left[0]["ticker"] == "300253"


def test_append_atomic_respects_exclude(store: AnalysisQueueStore):
    added = store.append_atomic(
        [_job("300253"), _job("300785")],
        exclude={("CN", "300253", "2026-07-15")},
    )
    assert added == 1
    assert [j.ticker for j in store.load()] == ["300785"]


def test_append_then_claim_does_not_lose_jobs(store: AnalysisQueueStore):
    """模拟 Web append 与 worker claim 交错：不丢任务、不重复。"""
    store.append_atomic([_job("A"), _job("B")])
    claimed = store.claim_next()
    store.append_atomic([_job("C")])
    remaining = [j.ticker for j in store.load()]
    assert claimed.ticker == "A"
    assert remaining == ["B", "C"]


def test_clear_atomic_empties_disk(store: AnalysisQueueStore):
    store.save([_job("300253"), _job("300785")])
    store.clear_atomic()
    assert store.load() == []


def test_exclusive_lock_is_reentrant_safe_within_process(store: AnalysisQueueStore):
    """同进程内嵌套 exclusive 不应死锁（append_atomic 内部会 load/save）。"""
    with store.exclusive():
        store.save([_job("300253")])
    assert [j.ticker for j in store.load()] == ["300253"]
