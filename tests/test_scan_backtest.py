"""Tests for scan archive backtest evaluation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tradingagents.evaluation.scan_backtest import (
    compare_scan_periods,
    evaluate_scan_archives,
    evaluate_scan_history,
    list_scan_archives,
    spearman_ic,
)


def _fake_fetcher(ticker: str, trade_date: str, need_bars: int) -> list[float] | None:
    base = {
        "600519": 100.0,
        "000001": 10.0,
        "000300": 50.0,
    }.get(ticker, 20.0)
    step = 0.01 if ticker == "600519" else -0.005
    return [base * (1 + step * i) for i in range(need_bars + 1)]


@pytest.fixture
def archive_dir(tmp_path: Path) -> Path:
    root = tmp_path / "archives"
    root.mkdir()
    record = {
        "status": "completed",
        "scan_id": "20260701_120000",
        "finished_at": "2026-07-01T12:00:00",
        "result": {
            "scan_date": "2026-07-01",
            "candidates": [
                {
                    "code": "600519",
                    "signal_score": 7,
                    "factor_hits": [
                        {"key": "fund_flow", "label": "竞价", "active": True, "hit": True},
                    ],
                },
                {
                    "code": "000001",
                    "signal_score": 3,
                    "factor_hits": [
                        {"key": "fund_flow", "label": "竞价", "active": True, "hit": False},
                    ],
                },
            ],
        },
    }
    (root / "20260701_120000.json").write_text(
        json.dumps(record, ensure_ascii=False), encoding="utf-8"
    )
    return root


def test_list_scan_archives(archive_dir: Path):
    rows = list_scan_archives("value_swing", archive_dir=archive_dir)
    assert len(rows) == 1
    assert rows[0]["scan_id"] == "20260701_120000"


def test_evaluate_scan_archives(archive_dir: Path):
    archives = list_scan_archives("value_swing", archive_dir=archive_dir)
    report = evaluate_scan_archives(archives, fetcher=_fake_fetcher)
    assert report["total_candidate_rows"] == 2
    assert report["aggregate"]["5"]["n"] == 2
    assert report["aggregate"]["20"]["median_excess"] is not None
    assert "fund_flow" in report["by_factor"]
    # Spearman IC needs ≥3 paired samples
    assert report["score_excess_ic_20d"] is None


def test_evaluate_scan_history(archive_dir: Path):
    report = evaluate_scan_history(
        "value_swing",
        archive_limit=10,
        archive_dir=archive_dir,
        fetcher=_fake_fetcher,
    )
    assert report["strategy"] == "value_swing"
    assert report["total_candidate_rows"] == 2


def test_compare_scan_periods(monkeypatch, archive_dir: Path):
    monkeypatch.setattr(
        "tradingagents.evaluation.scan_backtest.list_scan_archives",
        lambda *a, **k: list_scan_archives("value_swing", archive_dir=archive_dir),
    )
    out = compare_scan_periods(
        "value_swing",
        before_until="2026-08-23",
        after_since="2026-08-23",
        fetcher=_fake_fetcher,
    )
    assert "median_excess_20d_delta" in out


def test_spearman_ic_perfect():
    assert spearman_ic([1, 2, 3, 4], [1, 2, 3, 4]) == pytest.approx(1.0)
