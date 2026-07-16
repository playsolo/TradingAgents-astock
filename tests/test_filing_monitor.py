"""Tests for filing monitor: dedupe, enqueue, preview."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

from tradingagents.filings.monitor import (
    enqueue_us_filing_analysis,
    poll_ticker_filings,
    run_filing_poll_once,
)
from tradingagents.filings.store import FilingSeenStore
from web.analysis_queue import AnalysisJob


def test_poll_ticker_filings_dedupes(tmp_path: Path):
    store = FilingSeenStore(tmp_path / "seen.json")
    filings = [
        {
            "form": "8-K",
            "accession": "0000320193-25-000001",
            "filing_date": "2026-07-15",
            "primary_document": "a.htm",
            "description": "",
            "cik": "0000320193",
        }
    ]

    def list_fn(ticker, forms=None, limit=5):
        return filings

    first = poll_ticker_filings(
        "AAPL", store=store, list_fn=list_fn, today=date(2026, 7, 16)
    )
    second = poll_ticker_filings(
        "AAPL", store=store, list_fn=list_fn, today=date(2026, 7, 16)
    )
    assert len(first) == 1
    assert second == []


def test_poll_ticker_filings_skips_stale(tmp_path: Path):
    store = FilingSeenStore(tmp_path / "seen.json")
    filings = [
        {
            "form": "10-K",
            "accession": "0000320193-24-000099",
            "filing_date": "2024-11-01",
            "primary_document": "k.htm",
            "description": "",
            "cik": "0000320193",
        }
    ]

    def list_fn(ticker, forms=None, limit=5):
        return filings

    out = poll_ticker_filings(
        "AAPL", store=store, list_fn=list_fn, today=date(2026, 7, 16), max_age_days=5
    )
    assert out == []
    assert store.has_accession("0000320193-24-000099")


def test_enqueue_us_filing_analysis_uses_filing_source():
    captured = {}

    def append_job(job: AnalysisJob):
        captured["job"] = job
        return job

    enqueue_us_filing_analysis(
        "AAPL",
        {"form": "10-Q", "accession": "x"},
        append_job=append_job,
    )
    job = captured["job"]
    assert job.market == "US"
    assert job.source == "filing"
    assert job.force_full_reeval is True
    assert job.analysis_mode == "full_reeval"


def test_run_filing_poll_once_preview_and_filing(tmp_path: Path):
    store = FilingSeenStore(tmp_path / "seen.json")
    filings = [
        {
            "form": "10-Q",
            "accession": "0001-25-000001",
            "filing_date": "2026-07-16",
            "primary_document": "q.htm",
            "description": "10-Q",
            "cik": "0000000001",
        }
    ]

    with (
        patch(
            "tradingagents.filings.monitor.earnings_window_status",
            return_value={
                "next_date": "2026-07-20",
                "days_until": 4,
                "phase": "preview",
            },
        ),
        patch(
            "tradingagents.filings.monitor.poll_ticker_filings",
            return_value=filings,
        ),
        patch("tradingagents.filings.monitor.emit_earnings_preview") as prev,
        patch("tradingagents.filings.monitor.emit_filing_inbox") as finbox,
        patch("tradingagents.filings.monitor.enqueue_us_filing_analysis") as enq,
    ):
        summary = run_filing_poll_once(
            seen_store=store,
            tickers=["AAPL"],
            enqueue=True,
            notify=True,
            today=date(2026, 7, 16),
        )

    assert len(summary["previews"]) == 1
    assert len(summary["new_filings"]) == 1
    assert summary["enqueued"] == ["AAPL"]
    prev.assert_called_once()
    finbox.assert_called_once()
    enq.assert_called_once()


def test_analysis_job_accepts_filing_source():
    job = AnalysisJob.from_mapping(
        {
            "ticker": "AAPL",
            "trade_date": "2026-07-16",
            "market": "US",
            "source": "filing",
            "force_full_reeval": True,
        }
    )
    assert job.source == "filing"
