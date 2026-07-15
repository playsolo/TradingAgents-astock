"""Value-swing scan persistence (beside ~/.tradingagents history)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tradingagents.strategies.scan_store import (
    SCAN_STATUS_COMPLETED,
    SCAN_STATUS_FAILED,
    SCAN_STATUS_IDLE,
    SCAN_STATUS_RUNNING,
    ValueSwingScanStore,
)


@pytest.fixture
def store(tmp_path: Path) -> ValueSwingScanStore:
    return ValueSwingScanStore(
        path=tmp_path / "value_swing_scan.json",
        archive_dir=tmp_path / "value_swing_scans",
    )


def test_default_status_is_idle(store: ValueSwingScanStore):
    record = store.load()
    assert record["status"] == SCAN_STATUS_IDLE
    assert record["result"] is None


def test_mark_running_and_complete_persists_result(store: ValueSwingScanStore):
    store.mark_running(max_candidates=15, enqueue_on_success=False, pid=4242)
    running = store.load()
    assert running["status"] == SCAN_STATUS_RUNNING
    assert running["pid"] == 4242
    assert running["max_candidates"] == 15

    result = {
        "ok": True,
        "scan_date": "2026-07-15",
        "total_stocks": 5000,
        "l0_passed": 100,
        "l1a_passed": 40,
        "l1b_passed": 20,
        "l2_passed": 2,
        "candidates": [
            {"code": "000001", "name": "平安银行", "signal_score": 5, "pe_ttm": 6.0, "pb": 0.6, "price": 12.0}
        ],
        "duration_seconds": 12.5,
    }
    store.mark_completed(result, enqueued=0)
    done = store.load()
    assert done["status"] == SCAN_STATUS_COMPLETED
    assert done["result"]["l2_passed"] == 2
    assert done["result"]["candidates"][0]["code"] == "000001"
    assert done["pid"] is None
    assert done["finished_at"]

    archives = list(store.archive_dir.glob("*.json"))
    assert len(archives) == 1


def test_mark_failed_keeps_previous_result_optional(store: ValueSwingScanStore):
    store.mark_running(max_candidates=10, enqueue_on_success=True, pid=1)
    store.mark_failed("boom")
    failed = store.load()
    assert failed["status"] == SCAN_STATUS_FAILED
    assert failed["error"] == "boom"
    assert failed["result"] is None


def test_load_survives_corrupt_file(store: ValueSwingScanStore):
    store.path.write_text("{not-json", encoding="utf-8")
    record = store.load()
    assert record["status"] == SCAN_STATUS_IDLE


def test_update_progress_persists_while_running(store: ValueSwingScanStore):
    store.mark_running(max_candidates=15, enqueue_on_success=False, pid=42)
    store.update_progress(
        {"stage": "L1b", "percent": 40, "l0_passed": 100, "current_code": "000001"}
    )
    record = store.load()
    assert record["status"] == SCAN_STATUS_RUNNING
    assert record["progress"]["stage"] == "L1b"
    assert record["progress"]["percent"] == 40
    assert record["progress"]["current_code"] == "000001"


def test_update_progress_ignored_when_not_running(store: ValueSwingScanStore):
    # Idle store: progress writes must not resurrect a running-looking record.
    store.update_progress({"stage": "L2", "percent": 90})
    record = store.load()
    assert record["status"] == SCAN_STATUS_IDLE
    assert record["progress"] is None


def test_default_record_has_progress_key(store: ValueSwingScanStore):
    assert "progress" in store.load()
    assert store.load()["progress"] is None


def test_mark_running_preserves_prior_result(store: ValueSwingScanStore):
    store.mark_running(max_candidates=15, enqueue_on_success=False, pid=1)
    store.mark_completed({"ok": True, "l2_passed": 3, "candidates": []}, enqueued=0)

    # A new run starts; the prior result must survive so a failed re-run still
    # shows yesterday's candidates.
    store.mark_running(max_candidates=15, enqueue_on_success=False, pid=2)
    running = store.load()
    assert running["status"] == SCAN_STATUS_RUNNING
    assert running["result"]["l2_passed"] == 3

    store.mark_failed("boom")
    failed = store.load()
    assert failed["status"] == SCAN_STATUS_FAILED
    assert failed["result"]["l2_passed"] == 3
