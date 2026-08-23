"""Detached value-swing scan job runner (survives Web exit)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tradingagents.strategies.scan_runner import (
    _make_progress_writer,
    candidates_to_analysis_jobs,
    enqueue_scan_candidates,
    is_process_alive,
    result_dict_from_scan,
    run_scan_job,
    start_detached_scan,
)
from tradingagents.strategies.scan_store import (
    SCAN_STATUS_COMPLETED,
    SCAN_STATUS_FAILED,
    SCAN_STATUS_RUNNING,
    STRATEGY_BOTH,
    STRATEGY_GROWTH_ACCEL,
    STRATEGY_VALUE_SWING,
    ValueSwingScanStore,
    default_store,
)
from tradingagents.strategies.value_swing import ScanResult, StockInfo
from web.analysis_queue import AnalysisQueueStore


@pytest.fixture
def scan_store(tmp_path: Path) -> ValueSwingScanStore:
    return ValueSwingScanStore(
        path=tmp_path / "value_swing_scan.json",
        archive_dir=tmp_path / "value_swing_scans",
    )


@pytest.fixture
def queue_store(tmp_path: Path) -> AnalysisQueueStore:
    return AnalysisQueueStore(tmp_path / "analysis_queue.json")


def _fake_scan(*, max_candidates: int = 15) -> ScanResult:
    return ScanResult(
        scan_date="2026-07-15",
        total_stocks=100,
        l0_passed=50,
        l1a_passed=20,
        l1b_passed=10,
        l2_passed=2,
        candidates=[
            StockInfo(code="000001", name="平安银行", price=12, pe_ttm=6, pb=0.6, signal_score=5),
            StockInfo(code="600519", name="贵州茅台", price=1600, pe_ttm=20, pb=8, signal_score=3),
        ][:max_candidates],
        duration_seconds=1.5,
    )


def test_result_dict_from_scan_matches_ui_shape():
    d = result_dict_from_scan(_fake_scan())
    assert d["ok"] is True
    assert d["l2_passed"] == 2
    assert d["candidates"][0]["code"] == "000001"
    assert "debt_ratio" in d["candidates"][0]
    assert "rules" in d
    assert d["score_max"] == 9
    assert "why" in d["candidates"][0]
    assert "factor_hits" in d["candidates"][0]
    assert "exp_score_delta" in d["candidates"][0]


def test_run_scan_job_persists_completed_without_enqueue(
    scan_store: ValueSwingScanStore,
    queue_store: AnalysisQueueStore,
):
    record = run_scan_job(
        max_candidates=15,
        enqueue_on_success=False,
        scan_store=scan_store,
        queue_store=queue_store,
        scan_fn=_fake_scan,
        pid=999,
    )
    assert record["status"] == SCAN_STATUS_COMPLETED
    assert record["result"]["candidates"][0]["code"] == "000001"
    assert queue_store.load() == []
    assert scan_store.load()["status"] == SCAN_STATUS_COMPLETED


def test_run_scan_job_enqueues_candidates_on_success(
    scan_store: ValueSwingScanStore,
    queue_store: AnalysisQueueStore,
    monkeypatch: pytest.MonkeyPatch,
):
    # Bypass calibration skip so this test only covers enqueue wiring.
    monkeypatch.setattr(
        "web.analysis_queue.partition_scan_jobs_for_enqueue",
        lambda jobs, as_of=None: (list(jobs), []),
    )
    run_scan_job(
        max_candidates=15,
        enqueue_on_success=True,
        scan_store=scan_store,
        queue_store=queue_store,
        scan_fn=_fake_scan,
        pid=1001,
    )
    jobs = queue_store.load()
    assert [j.ticker for j in jobs] == ["000001", "600519"]
    assert all(j.market == "CN" for j in jobs)
    assert scan_store.load()["enqueued"] == 2


def test_run_scan_job_keeps_result_when_enqueue_fails(
    scan_store: ValueSwingScanStore,
    queue_store: AnalysisQueueStore,
    monkeypatch: pytest.MonkeyPatch,
):
    # A successful scan whose enqueue step blows up must still persist as
    # completed (critical for overnight auto-enqueue).
    def boom_enqueue(*args, **kwargs):
        raise RuntimeError("queue disk full")

    monkeypatch.setattr(
        "tradingagents.strategies.scan_runner.enqueue_scan_candidates",
        boom_enqueue,
    )
    record = run_scan_job(
        max_candidates=15,
        enqueue_on_success=True,
        scan_store=scan_store,
        queue_store=queue_store,
        scan_fn=_fake_scan,
        pid=555,
    )
    assert record["status"] == SCAN_STATUS_COMPLETED
    assert record["result"]["candidates"][0]["code"] == "000001"
    assert record["enqueued"] == 0


def test_run_scan_job_marks_failed_on_exception(
    scan_store: ValueSwingScanStore,
    queue_store: AnalysisQueueStore,
):
    def boom(**kwargs):
        raise RuntimeError("network down")

    record = run_scan_job(
        max_candidates=10,
        enqueue_on_success=True,
        scan_store=scan_store,
        queue_store=queue_store,
        scan_fn=boom,
        pid=7,
    )
    assert record["status"] == SCAN_STATUS_FAILED
    assert "network down" in record["error"]
    assert queue_store.load() == []


def test_run_scan_job_refuses_when_already_running(
    scan_store: ValueSwingScanStore,
    queue_store: AnalysisQueueStore,
    monkeypatch: pytest.MonkeyPatch,
):
    scan_store.mark_running(max_candidates=15, enqueue_on_success=False, pid=1)
    monkeypatch.setattr(
        "tradingagents.strategies.scan_runner.is_process_alive",
        lambda pid: True,
    )
    with pytest.raises(RuntimeError, match="已有扫描"):
        run_scan_job(
            max_candidates=15,
            enqueue_on_success=False,
            scan_store=scan_store,
            queue_store=queue_store,
            scan_fn=_fake_scan,
            pid=2,
        )


def test_run_scan_job_forwards_progress_to_store(
    scan_store: ValueSwingScanStore,
    queue_store: AnalysisQueueStore,
):
    def scan_with_progress(*, max_candidates=15, progress_cb=None):
        if progress_cb is not None:
            progress_cb({"stage": "L1b", "percent": 40, "current_code": "000001"})
        return _fake_scan(max_candidates=max_candidates)

    run_scan_job(
        max_candidates=15,
        enqueue_on_success=False,
        scan_store=scan_store,
        queue_store=queue_store,
        scan_fn=scan_with_progress,
        pid=321,
    )
    record = scan_store.load()
    # Completed record retains the last progress snapshot written mid-run.
    assert record["status"] == SCAN_STATUS_COMPLETED
    assert record["progress"]["stage"] == "L1b"
    assert record["progress"]["current_code"] == "000001"


def test_run_scan_job_tolerates_scan_fn_without_progress_cb(
    scan_store: ValueSwingScanStore,
    queue_store: AnalysisQueueStore,
):
    # Legacy scan_fn (no progress_cb kwarg) must still work.
    record = run_scan_job(
        max_candidates=15,
        enqueue_on_success=False,
        scan_store=scan_store,
        queue_store=queue_store,
        scan_fn=_fake_scan,
        pid=322,
    )
    assert record["status"] == SCAN_STATUS_COMPLETED


def test_candidates_to_analysis_jobs_uses_trade_date():
    jobs = candidates_to_analysis_jobs(
        [{"code": "000001"}, {"code": "600519"}],
        trade_date="2026-07-15",
    )
    assert jobs[0].ticker == "000001"
    assert jobs[0].trade_date == "2026-07-15"
    assert jobs[0].fresh is True


def test_candidates_to_analysis_jobs_skips_watch_lane():
    jobs = candidates_to_analysis_jobs(
        [
            {"code": "000001", "lane": "analyze"},
            {"code": "600519", "lane": "watch"},
            {"code": "300750"},  # 无 lane → 兼容旧载荷，仍入队
        ],
        trade_date="2026-07-16",
    )
    assert [j.ticker for j in jobs] == ["000001", "300750"]


def test_enqueue_scan_candidates_dedupes(
    queue_store: AnalysisQueueStore,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        "web.analysis_queue.partition_scan_jobs_for_enqueue",
        lambda jobs, as_of=None: (list(jobs), []),
    )
    first = enqueue_scan_candidates(
        [{"code": "000001"}, {"code": "600519"}],
        trade_date="2026-07-15",
        queue_store=queue_store,
    )
    second = enqueue_scan_candidates(
        [{"code": "000001"}, {"code": "300750"}],
        trade_date="2026-07-15",
        queue_store=queue_store,
    )
    assert first == 2
    assert second == 1
    assert [j.ticker for j in queue_store.load()] == ["000001", "600519", "300750"]


def test_is_process_alive_current_pid():
    import os

    assert is_process_alive(os.getpid()) is True
    assert is_process_alive(999_999_999) is False


def test_run_scan_job_allows_when_running_pid_is_self(
    scan_store: ValueSwingScanStore,
    queue_store: AnalysisQueueStore,
):
    # A parent launcher pre-reserved the slot with our pid; we must proceed.
    scan_store.mark_running(max_candidates=15, enqueue_on_success=False, pid=4444)
    record = run_scan_job(
        max_candidates=15,
        enqueue_on_success=False,
        scan_store=scan_store,
        queue_store=queue_store,
        scan_fn=_fake_scan,
        pid=4444,
    )
    assert record["status"] == SCAN_STATUS_COMPLETED


def test_start_detached_scan_reserves_running_slot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    class FakePopen:
        def __init__(self, *args, **kwargs):
            self.pid = 6161

    monkeypatch.setattr(
        "tradingagents.strategies.scan_runner.subprocess.Popen",
        FakePopen,
    )
    status_path = tmp_path / "value_swing_scan.json"
    archive_dir = tmp_path / "scans"
    start_detached_scan(
        max_candidates=10,
        status_path=status_path,
        archive_dir=archive_dir,
        log_path=tmp_path / "scan.log",
        strategy=STRATEGY_VALUE_SWING,
    )
    store = ValueSwingScanStore(path=status_path, archive_dir=archive_dir)
    record = store.load()
    assert record["status"] == SCAN_STATUS_RUNNING
    assert record["pid"] == 6161


def test_progress_writer_flushes_on_count_change(scan_store: ValueSwingScanStore):
    scan_store.mark_running(max_candidates=15, enqueue_on_success=False, pid=1)
    write = _make_progress_writer(scan_store)

    write({"stage": "L1b", "l0_passed": 10, "l1a_passed": 5, "l1b_passed": 0,
           "l2_passed": 0, "percent": 20, "current_code": "A"})
    # A funnel count changed -> must flush immediately despite the throttle.
    write({"stage": "L1b", "l0_passed": 10, "l1a_passed": 5, "l1b_passed": 3,
           "l2_passed": 0, "percent": 21, "current_code": "B"})
    assert scan_store.load()["progress"]["l1b_passed"] == 3

    # Pure percent/current-stock churn (no milestone change) is throttled.
    write({"stage": "L1b", "l0_passed": 10, "l1a_passed": 5, "l1b_passed": 3,
           "l2_passed": 0, "percent": 22, "current_code": "C"})
    assert scan_store.load()["progress"]["current_code"] == "B"


def test_start_detached_scan_refuses_second_concurrent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    import os

    class FakePopen:
        def __init__(self, *args, **kwargs):
            self.pid = os.getpid()  # a definitely-alive pid

    monkeypatch.setattr(
        "tradingagents.strategies.scan_runner.subprocess.Popen",
        FakePopen,
    )
    status_path = tmp_path / "value_swing_scan.json"
    archive_dir = tmp_path / "scans"
    log_path = tmp_path / "scan.log"
    start_detached_scan(
        max_candidates=10,
        status_path=status_path,
        archive_dir=archive_dir,
        log_path=log_path,
        strategy=STRATEGY_VALUE_SWING,
    )
    with pytest.raises(RuntimeError, match="已有扫描"):
        start_detached_scan(
            max_candidates=10,
            status_path=status_path,
            archive_dir=archive_dir,
            log_path=log_path,
            strategy=STRATEGY_VALUE_SWING,
        )


def test_start_detached_scan_uses_new_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    calls: list[dict] = []

    class FakePopen:
        def __init__(self, *args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
            self.pid = 5555

    monkeypatch.setattr(
        "tradingagents.strategies.scan_runner.subprocess.Popen",
        FakePopen,
    )
    status_path = tmp_path / "value_swing_scan.json"
    archive_dir = tmp_path / "scans"
    pid = start_detached_scan(
        max_candidates=12,
        enqueue_on_success=True,
        status_path=status_path,
        archive_dir=archive_dir,
        strategy=STRATEGY_VALUE_SWING,
    )
    assert pid == 5555
    assert calls
    kwargs = calls[0]["kwargs"]
    assert kwargs.get("start_new_session") is True
    argv = calls[0]["args"][0]
    assert "--max-candidates" in argv
    assert "12" in argv
    assert "--enqueue" in argv
    assert STRATEGY_VALUE_SWING in argv


def test_start_detached_scan_both_reserves_two_stores(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    class FakePopen:
        def __init__(self, *args, **kwargs):
            self.argv = args[0]
            self.pid = 7777

    monkeypatch.setattr(
        "tradingagents.strategies.scan_runner.subprocess.Popen",
        FakePopen,
    )
    monkeypatch.setenv(
        "TRADINGAGENTS_VALUE_SWING_SCAN_PATH",
        str(tmp_path / "value_swing_scan.json"),
    )
    monkeypatch.setenv(
        "TRADINGAGENTS_VALUE_SWING_SCANS_DIR",
        str(tmp_path / "value_scans"),
    )
    monkeypatch.setenv(
        "TRADINGAGENTS_GROWTH_ACCEL_SCAN_PATH",
        str(tmp_path / "growth_accel_scan.json"),
    )
    monkeypatch.setenv(
        "TRADINGAGENTS_GROWTH_ACCEL_SCANS_DIR",
        str(tmp_path / "growth_scans"),
    )
    pid = start_detached_scan(
        max_candidates=15,
        log_path=tmp_path / "both.log",
        strategy=STRATEGY_BOTH,
    )
    assert pid == 7777
    v = default_store(STRATEGY_VALUE_SWING).load()
    g = default_store(STRATEGY_GROWTH_ACCEL).load()
    assert v["status"] == SCAN_STATUS_RUNNING and v["pid"] == 7777
    assert g["status"] == SCAN_STATUS_RUNNING and g["pid"] == 7777
