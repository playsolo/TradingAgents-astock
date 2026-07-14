"""Queue display labels + disk persistence across Streamlit sessions."""

from __future__ import annotations

from pathlib import Path

import pytest

from web.analysis_queue import (
    QUEUE_SESSION_KEY,
    AnalysisJob,
    AnalysisQueueStore,
    advance_queue,
    append_jobs,
    clear_queue,
    format_queue_job_caption,
    hydrate_queue,
    prepend_job,
    queue_snapshot,
)


@pytest.fixture
def queue_store(tmp_path: Path) -> AnalysisQueueStore:
    return AnalysisQueueStore(tmp_path / "analysis_queue.json")


def test_format_queue_job_caption_includes_stock_name(monkeypatch):
    monkeypatch.setattr(
        "web.stock_display.resolve_stock_name",
        lambda ticker: "药明康德" if "603259" in str(ticker) else None,
    )
    job = AnalysisJob(ticker="603259", trade_date="2026-07-14", market="CN")
    caption = format_queue_job_caption(job, index=2)
    assert caption.startswith("2. ")
    assert "603259" in caption
    assert "药明康德" in caption
    assert "2026-07-14" in caption
    assert "A股" in caption


def test_format_queue_job_caption_falls_back_to_code_only(monkeypatch):
    monkeypatch.setattr("web.stock_display.resolve_stock_name", lambda ticker: None)
    job = AnalysisJob(ticker="AAPL", trade_date="2026-07-14", market="US")
    caption = format_queue_job_caption(job, index=1)
    assert caption == "1. AAPL  ·  2026-07-14  ·  美股"


def test_append_jobs_persists_to_disk(queue_store: AnalysisQueueStore):
    session: dict = {}
    append_jobs(
        session,
        [
            AnalysisJob(ticker="603259", trade_date="2026-07-14", market="CN"),
            AnalysisJob(ticker="603011", trade_date="2026-07-14", market="CN"),
        ],
        store=queue_store,
    )
    loaded = queue_store.load()
    assert [j.ticker for j in loaded] == ["603259", "603011"]


def test_hydrate_queue_restores_into_fresh_session(queue_store: AnalysisQueueStore):
    queue_store.save(
        [
            AnalysisJob(ticker="603259", trade_date="2026-07-14", market="CN"),
            AnalysisJob(ticker="603501", trade_date="2026-07-14", market="CN"),
        ]
    )
    fresh: dict = {}
    count = hydrate_queue(fresh, store=queue_store)
    assert count == 2
    assert [j.ticker for j in queue_snapshot(fresh)] == ["603259", "603501"]
    # Second hydrate in same session is a no-op
    assert hydrate_queue(fresh, store=queue_store) == 0


def test_advance_and_clear_update_persisted_file(queue_store: AnalysisQueueStore):
    session: dict = {}
    append_jobs(
        session,
        [
            AnalysisJob(ticker="AAPL", trade_date="2026-07-14", market="US"),
            AnalysisJob(ticker="NVDA", trade_date="2026-07-14", market="US"),
        ],
        store=queue_store,
    )
    assert advance_queue(session, store=queue_store).ticker == "AAPL"
    assert [j.ticker for j in queue_store.load()] == ["NVDA"]

    clear_queue(session, store=queue_store)
    assert queue_store.load() == []
    assert session[QUEUE_SESSION_KEY] == []


def test_prepend_job_persists(queue_store: AnalysisQueueStore):
    session: dict = {}
    append_jobs(
        session,
        [AnalysisJob(ticker="NVDA", trade_date="2026-07-14", market="US")],
        store=queue_store,
    )
    prepend_job(
        session,
        AnalysisJob(ticker="AAPL", trade_date="2026-07-14", market="US"),
        store=queue_store,
    )
    assert [j.ticker for j in queue_store.load()] == ["AAPL", "NVDA"]


def test_new_session_after_refresh_sees_persisted_queue(queue_store: AnalysisQueueStore):
    """Refreshing Streamlit creates a new session_state dict; disk must carry over."""
    first: dict = {}
    append_jobs(
        first,
        [AnalysisJob(ticker="600354", trade_date="2026-07-14", market="CN")],
        store=queue_store,
    )
    second: dict = {}
    hydrate_queue(second, store=queue_store)
    assert [j.ticker for j in queue_snapshot(second)] == ["600354"]


def test_hydrate_does_not_consume_jobs(queue_store: AnalysisQueueStore):
    """Restore is read-only for the queue length; auto-start is intentionally not here."""
    queue_store.save(
        [
            AnalysisJob(ticker="603259", trade_date="2026-07-14", market="CN"),
            AnalysisJob(ticker="603011", trade_date="2026-07-14", market="CN"),
        ]
    )
    session: dict = {}
    assert hydrate_queue(session, store=queue_store) == 2
    assert [j.ticker for j in queue_snapshot(session)] == ["603259", "603011"]
    assert [j.ticker for j in queue_store.load()] == ["603259", "603011"]
