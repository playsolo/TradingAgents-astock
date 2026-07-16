"""Tests for earnings calendar window classification."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

from tradingagents.filings.earnings_calendar import (
    earnings_window_status,
    next_earnings_date,
)


def test_next_earnings_date_from_earnings_dates_index():
    import pandas as pd

    idx = pd.DatetimeIndex(["2026-07-20", "2026-10-20"])
    ed = pd.DataFrame({"eps": [1.0, 1.1]}, index=idx)
    ticker = MagicMock()
    ticker.earnings_dates = ed
    ticker.calendar = {}

    with patch("yfinance.Ticker", return_value=ticker):
        assert next_earnings_date("AAPL", today=date(2026, 7, 16)) == date(2026, 7, 20)


def test_earnings_window_phases():
    with patch(
        "tradingagents.filings.earnings_calendar.next_earnings_date",
        return_value=date(2026, 7, 20),
    ):
        st = earnings_window_status("AAPL", today=date(2026, 7, 16), preview_days=7)
        assert st["phase"] == "preview"
        assert st["days_until"] == 4

    with patch(
        "tradingagents.filings.earnings_calendar.next_earnings_date",
        return_value=date(2026, 7, 16),
    ):
        st = earnings_window_status("AAPL", today=date(2026, 7, 16))
        assert st["phase"] == "day_of"

    with patch(
        "tradingagents.filings.earnings_calendar.next_earnings_date",
        return_value=None,
    ):
        st = earnings_window_status("AAPL", today=date(2026, 7, 16))
        assert st["phase"] == "none"
