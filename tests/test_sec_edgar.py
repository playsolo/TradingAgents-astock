"""Unit tests for SEC EDGAR client (mocked HTTP)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from tradingagents.filings.sec_edgar import (
    TARGET_FORMS,
    extract_recent_filings,
    fetch_filing_excerpt,
    format_filings_report,
    lookup_cik,
    pad_cik,
)


def test_pad_cik():
    assert pad_cik("320193") == "0000320193"
    assert pad_cik(320193) == "0000320193"
    assert pad_cik("0000320193") == "0000320193"


def test_extract_recent_filings_filters_forms_and_limit():
    recent = {
        "accessionNumber": ["0000320193-25-000001", "0000320193-25-000002", "0000320193-24-000099"],
        "filingDate": ["2025-01-30", "2025-01-29", "2024-11-01"],
        "form": ["10-Q", "8-K", "4"],
        "primaryDocument": ["aapl-10q.htm", "aapl-8k.htm", "form4.xml"],
        "primaryDocDescription": ["10-Q", "Current report", "Form 4"],
    }
    out = extract_recent_filings(recent, forms=TARGET_FORMS, limit=5)
    assert len(out) == 2
    assert out[0]["form"] == "10-Q"
    assert out[0]["accession"] == "0000320193-25-000001"
    assert out[1]["form"] == "8-K"


def test_lookup_cik_from_ticker_map():
    payload = {
        "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
        "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft"},
    }
    with patch("tradingagents.filings.sec_edgar._http_get_json", return_value=payload):
        assert lookup_cik("AAPL") == "0000320193"
        assert lookup_cik("msft") == "0000789019"
        assert lookup_cik("NOPE") is None


def test_format_filings_report_includes_links():
    filings = [
        {
            "form": "10-Q",
            "accession": "0000320193-25-000001",
            "filing_date": "2025-01-30",
            "primary_document": "aapl-10q.htm",
            "description": "10-Q",
            "cik": "0000320193",
        }
    ]
    text = format_filings_report("AAPL", filings, excerpts={"0000320193-25-000001": "Revenue grew 5%."})
    assert "AAPL" in text
    assert "10-Q" in text
    assert "Revenue grew 5%" in text
    assert "sec.gov" in text


def test_fetch_filing_excerpt_strips_html():
    html = "<html><body><p>Net income was $10 billion.</p><script>x</script></body></html>"
    resp = MagicMock()
    resp.text = html
    resp.raise_for_status = MagicMock()
    with patch("tradingagents.filings.sec_edgar._http_get", return_value=resp):
        text = fetch_filing_excerpt(
            cik="0000320193",
            accession="0000320193-25-000001",
            primary_document="aapl-10q.htm",
            max_chars=500,
        )
    assert "Net income was $10 billion" in text
    assert "<script>" not in text
