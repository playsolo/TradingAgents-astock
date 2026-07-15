"""Sequential analysis queue: parse multi-ticker input and advance jobs."""

from __future__ import annotations

from pathlib import Path

import pytest

from web import history
from web.analysis_queue import (
    QUEUE_SESSION_KEY,
    SERIAL_QUEUE_SESSION_KEY,
    AnalysisJob,
    AnalysisQueueStore,
    advance_queue,
    append_jobs,
    clear_queue,
    format_restored_queue_blocked_notice,
    has_blocking_incomplete_run,
    hydrate_queue,
    maybe_autostart_restored_queue,
    parse_ticker_inputs,
    prepend_job,
    resolve_ticker_batch,
    take_next_job,
)


@pytest.fixture
def store(tmp_path: Path) -> AnalysisQueueStore:
    return AnalysisQueueStore(tmp_path / "analysis_queue.json")


@pytest.fixture
def incomplete_index(tmp_path: Path, monkeypatch):
    index = tmp_path / "incomplete_tasks.json"
    monkeypatch.setattr(history, "_INCOMPLETE_TASKS_FILE", index)
    return index


def test_parse_ticker_inputs_splits_common_separators():
    raw = "300750, 600519\n宁德时代；AAPL  NVDA，BRK.B"
    assert parse_ticker_inputs(raw) == [
        "300750",
        "600519",
        "宁德时代",
        "AAPL",
        "NVDA",
        "BRK.B",
    ]


def test_parse_ticker_inputs_ignores_empty_and_keeps_order_unique():
    assert parse_ticker_inputs("  AAPL ,, aapl\nNVDA; AAPL  ") == ["AAPL", "NVDA"]


def test_resolve_ticker_batch_cn_resolves_and_skips_failures(monkeypatch):
    def fake_resolve(raw: str) -> str:
        mapping = {"宁德时代": "300750", "600519": "600519"}
        if raw in mapping:
            return mapping[raw]
        raise ValueError(f"unknown:{raw}")

    jobs, errors = resolve_ticker_batch(
        ["宁德时代", "坏票", "600519"],
        market="CN",
        trade_date="2026-07-14",
        resolve_cn=fake_resolve,
    )
    assert [j.ticker for j in jobs] == ["300750", "600519"]
    assert all(j.market == "CN" for j in jobs)
    assert all(j.trade_date == "2026-07-14" for j in jobs)
    assert all(j.fresh for j in jobs)
    assert errors == ["坏票: unknown:坏票"]


def test_resolve_ticker_batch_us_normalizes_without_cn_resolver():
    jobs, errors = resolve_ticker_batch(
        [" aapl ", "BRK.B", ""],
        market="US",
        trade_date="2026-07-14",
        resolve_cn=lambda _raw: (_ for _ in ()).throw(AssertionError("CN")),
    )
    assert [j.ticker for j in jobs] == ["AAPL", "BRK.B"]
    assert all(j.market == "US" for j in jobs)
    assert errors == []


def test_resolve_ticker_batch_us_rejects_six_digit_a_share_codes():
    jobs, errors = resolve_ticker_batch(
        ["300750", "AAPL"],
        market="US",
        trade_date="2026-07-14",
        resolve_cn=lambda _raw: (_ for _ in ()).throw(AssertionError("CN")),
    )
    assert [j.ticker for j in jobs] == ["AAPL"]
    assert errors and "300750" in errors[0]
    assert "A股" in errors[0] or "美股" in errors[0]


def test_append_and_advance_queue_fifo(store):
    session: dict = {}
    first = AnalysisJob(ticker="AAPL", trade_date="2026-07-14", market="US")
    second = AnalysisJob(ticker="NVDA", trade_date="2026-07-14", market="US")
    append_jobs(session, [first, second], store=store)
    assert len(session[QUEUE_SESSION_KEY]) == 2
    assert session[SERIAL_QUEUE_SESSION_KEY] is True

    nxt = advance_queue(session, store=store)
    assert nxt == first
    assert [j.ticker for j in session[QUEUE_SESSION_KEY]] == ["NVDA"]

    nxt = advance_queue(session, store=store)
    assert nxt == second
    assert session[QUEUE_SESSION_KEY] == []

    assert advance_queue(session, store=store) is None


def test_clear_queue_empties_session(store):
    session = {
        QUEUE_SESSION_KEY: [
            AnalysisJob(ticker="AAPL", trade_date="2026-07-14", market="US"),
        ],
        SERIAL_QUEUE_SESSION_KEY: True,
    }
    clear_queue(session, store=store)
    assert session[QUEUE_SESSION_KEY] == []
    assert session[SERIAL_QUEUE_SESSION_KEY] is False


def test_append_jobs_dedupes_against_existing_queue(store):
    session: dict = {}
    append_jobs(
        session,
        [AnalysisJob(ticker="AAPL", trade_date="2026-07-14", market="US")],
        store=store,
    )
    append_jobs(
        session,
        [
            AnalysisJob(ticker="AAPL", trade_date="2026-07-14", market="US"),
            AnalysisJob(ticker="NVDA", trade_date="2026-07-14", market="US"),
        ],
        store=store,
    )
    assert [j.ticker for j in session[QUEUE_SESSION_KEY]] == ["AAPL", "NVDA"]


def test_append_jobs_clears_incomplete_for_queued_identities(store, incomplete_index):
    history.record_incomplete_task(
        "300253", "2026-07-15", status="error", error="worker 重启"
    )
    history.record_incomplete_task(
        "002648", "2026-07-15", status="running", error=""
    )
    session: dict = {}
    append_jobs(
        session,
        [AnalysisJob(ticker="300253", trade_date="2026-07-15", market="CN")],
        store=store,
    )
    left = history.get_incomplete_history()
    assert len(left) == 1
    assert left[0]["ticker"] == "002648"


def test_take_next_job_sets_notice_and_returns_job(store):
    session = {
        QUEUE_SESSION_KEY: [
            AnalysisJob(ticker="NVDA", trade_date="2026-07-14", market="US"),
        ]
    }
    nxt = take_next_job(session, finished_ticker="AAPL", store=store)
    assert nxt is not None
    assert nxt.ticker == "NVDA"
    assert nxt.market == "US"
    assert "AAPL 已完成" in session["queue_advance_notice"]
    assert session[QUEUE_SESSION_KEY] == []
    assert "start_analysis" not in session


def test_take_next_job_on_error_mentions_skip(store):
    session = {
        QUEUE_SESSION_KEY: [
            AnalysisJob(ticker="600519", trade_date="2026-07-14", market="CN"),
        ]
    }
    nxt = take_next_job(
        session, finished_ticker="300750", error="timeout", store=store
    )
    assert nxt is not None
    assert nxt.ticker == "600519"
    assert "失败（timeout）" in session["queue_advance_notice"]


def test_take_next_job_returns_none_when_empty(store):
    session: dict = {}
    assert take_next_job(session, finished_ticker="AAPL", store=store) is None


def test_append_jobs_can_exclude_active_identity(store):
    session: dict = {}
    active = AnalysisJob(ticker="AAPL", trade_date="2026-07-14", market="US")
    added = append_jobs(
        session,
        [active, AnalysisJob(ticker="NVDA", trade_date="2026-07-14", market="US")],
        exclude={active.identity()},
        store=store,
    )
    assert added == 1
    assert [j.ticker for j in session[QUEUE_SESSION_KEY]] == ["NVDA"]


def test_prepend_job_restores_front_and_dedupes(store):
    session = {
        QUEUE_SESSION_KEY: [
            AnalysisJob(ticker="NVDA", trade_date="2026-07-14", market="US"),
            AnalysisJob(ticker="AAPL", trade_date="2026-07-14", market="US"),
        ]
    }
    prepend_job(
        session,
        AnalysisJob(ticker="AAPL", trade_date="2026-07-14", market="US"),
        store=store,
    )
    assert [j.ticker for j in session[QUEUE_SESSION_KEY]] == ["AAPL", "NVDA"]


def test_take_next_job_drains_serial_session_and_clears_views(store):
    session = {
        QUEUE_SESSION_KEY: [],
        SERIAL_QUEUE_SESSION_KEY: True,
        "viewing_watchlist": True,
        "viewing_history": "/tmp/hist",
    }
    assert take_next_job(session, finished_ticker="AAPL", store=store) is None
    assert session[SERIAL_QUEUE_SESSION_KEY] is False
    assert session["viewing_watchlist"] is False
    assert session["viewing_history"] is None
    assert "分析队列已全部结束" in session["queue_advance_notice"]


def test_take_next_job_empty_without_serial_session_keeps_views(store):
    session = {
        QUEUE_SESSION_KEY: [],
        "viewing_watchlist": True,
    }
    assert take_next_job(session, finished_ticker="AAPL", store=store) is None
    assert session["viewing_watchlist"] is True
    assert "queue_advance_notice" not in session


def test_hydrate_queue_restores_from_disk(store):
    store.save(
        [
            AnalysisJob(ticker="MSFT", trade_date="2026-07-14", market="US"),
        ]
    )
    session: dict = {}
    restored = hydrate_queue(session, store=store)
    assert restored == 1
    assert [j.ticker for j in session[QUEUE_SESSION_KEY]] == ["MSFT"]
    assert session[SERIAL_QUEUE_SESSION_KEY] is True


def test_has_blocking_incomplete_run_only_running_or_paused():
    assert has_blocking_incomplete_run([]) is False
    assert has_blocking_incomplete_run(None) is False
    assert has_blocking_incomplete_run([{"status": "error"}]) is False
    assert has_blocking_incomplete_run([{"status": "running"}]) is True
    assert has_blocking_incomplete_run([{"status": "paused"}]) is True
    assert has_blocking_incomplete_run(
        [{"status": "error"}, {"status": "paused"}]
    ) is True


def test_maybe_autostart_restored_queue_when_idle(store):
    session = {
        QUEUE_SESSION_KEY: [
            AnalysisJob(ticker="300919", trade_date="2026-07-14", market="CN"),
            AnalysisJob(ticker="688401", trade_date="2026-07-14", market="CN"),
        ],
        SERIAL_QUEUE_SESSION_KEY: True,
        "viewing_history": "/tmp/old",
        "viewing_watchlist": True,
    }
    committed: list[str] = []

    def _before(job: AnalysisJob) -> None:
        # Must run while head is still on disk (before pop persist).
        assert [j.ticker for j in store.load()] == ["300919", "688401"]
        committed.append(job.ticker)

    store.save(list(session[QUEUE_SESSION_KEY]))
    started = maybe_autostart_restored_queue(
        session,
        restored=2,
        tracker_running=False,
        incomplete_entries=[],
        store=store,
        before_commit=_before,
    )
    assert started is not None
    assert started.ticker == "300919"
    assert committed == ["300919"]
    assert session["start_analysis"]["ticker"] == "300919"
    assert [j.ticker for j in session[QUEUE_SESSION_KEY]] == ["688401"]
    assert session["viewing_history"] is None
    assert session["viewing_watchlist"] is False
    assert "自动开始" in session["queue_advance_notice"]
    assert "300919" in session["queue_advance_notice"]


def test_maybe_autostart_skips_when_incomplete_running(store):
    session = {
        QUEUE_SESSION_KEY: [
            AnalysisJob(ticker="300919", trade_date="2026-07-14", market="CN"),
        ],
    }
    assert (
        maybe_autostart_restored_queue(
            session,
            restored=1,
            tracker_running=False,
            incomplete_entries=[{"status": "running", "ticker": "688401"}],
            store=store,
        )
        is None
    )
    assert "start_analysis" not in session
    assert len(session[QUEUE_SESSION_KEY]) == 1


def test_maybe_autostart_skips_when_tracker_running(store):
    session = {
        QUEUE_SESSION_KEY: [
            AnalysisJob(ticker="300919", trade_date="2026-07-14", market="CN"),
        ],
    }
    assert (
        maybe_autostart_restored_queue(
            session,
            restored=1,
            tracker_running=True,
            incomplete_entries=[],
            store=store,
        )
        is None
    )
    assert "start_analysis" not in session


def test_maybe_autostart_allows_error_incomplete(store):
    """error 不等于进行中，空闲恢复仍可自动开跑。"""
    session = {
        QUEUE_SESSION_KEY: [
            AnalysisJob(ticker="300919", trade_date="2026-07-14", market="CN"),
        ],
    }
    started = maybe_autostart_restored_queue(
        session,
        restored=1,
        tracker_running=False,
        incomplete_entries=[{"status": "error", "ticker": "688401"}],
        store=store,
    )
    assert started is not None
    assert session["start_analysis"]["ticker"] == "300919"


def test_maybe_autostart_noop_when_nothing_restored(store):
    session = {
        QUEUE_SESSION_KEY: [
            AnalysisJob(ticker="300919", trade_date="2026-07-14", market="CN"),
        ],
    }
    assert (
        maybe_autostart_restored_queue(
            session,
            restored=0,
            tracker_running=False,
            incomplete_entries=[],
            store=store,
        )
        is None
    )
    assert "start_analysis" not in session


def test_format_restored_queue_blocked_notice_matches_reason():
    assert "进行中/已暂停" in format_restored_queue_blocked_notice(
        1,
        tracker_running=False,
        incomplete_entries=[{"status": "running"}],
    )
    assert "仍有分析在跑" in format_restored_queue_blocked_notice(
        1,
        tracker_running=True,
        incomplete_entries=[],
    )
    assert "待启动" in format_restored_queue_blocked_notice(
        1,
        tracker_running=False,
        incomplete_entries=[],
        start_already_set=True,
    )


def test_from_mapping_accepts_stale_analysis_job_class_instance():
    """Streamlit reloads create a new AnalysisJob class; old instances must coerce."""
    from types import SimpleNamespace

    from web.analysis_queue import AnalysisJob, QUEUE_SESSION_KEY, queue_snapshot

    stale = SimpleNamespace(
        ticker="688401",
        trade_date="2026-07-14",
        market="CN",
        fresh=True,
    )
    # Pretend it looks like AnalysisJob but is not isinstance
    job = AnalysisJob.from_mapping(stale)  # type: ignore[arg-type]
    assert job == AnalysisJob(ticker="688401", trade_date="2026-07-14", market="CN")

    session = {QUEUE_SESSION_KEY: [stale, AnalysisJob(ticker="002648", trade_date="2026-07-14", market="CN")]}
    snap = queue_snapshot(session)
    assert [j.ticker for j in snap] == ["688401", "002648"]
    assert all(isinstance(j, AnalysisJob) for j in snap)
