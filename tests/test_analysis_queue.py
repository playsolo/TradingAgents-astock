"""Sequential analysis queue: parse multi-ticker input and advance jobs."""

from __future__ import annotations

from pathlib import Path

import pytest

from web.analysis_queue import (
    QUEUE_SESSION_KEY,
    SERIAL_QUEUE_SESSION_KEY,
    AnalysisJob,
    AnalysisQueueStore,
    advance_queue,
    append_jobs,
    clear_queue,
    hydrate_queue,
    parse_ticker_inputs,
    prepend_job,
    resolve_ticker_batch,
    take_next_job,
)


@pytest.fixture
def store(tmp_path: Path) -> AnalysisQueueStore:
    return AnalysisQueueStore(tmp_path / "analysis_queue.json")


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
