"""Web worker 模式：只入队、不在进程内执行分析。"""

from __future__ import annotations

from pathlib import Path

import pytest

from web import history
from web.analysis_queue import QUEUE_SESSION_KEY, AnalysisJob, AnalysisQueueStore
from web.components import sidebar
from web.parallel_runs import ACTIVE_RUNS_KEY


@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADINGAGENTS_ANALYSIS_EXECUTOR", "worker")
    store = AnalysisQueueStore(tmp_path / "analysis_queue.json")
    monkeypatch.setattr(sidebar, "default_store", lambda: store)
    monkeypatch.setattr(history, "_INCOMPLETE_TASKS_FILE", tmp_path / "incomplete.json")
    return store


# ── Source-level guards (worker must not run analysis in-process) ────────────

def test_app_start_request_enqueues_in_worker_mode():
    app_src = Path("web/app.py").read_text(encoding="utf-8")
    assert "is_worker_mode" in app_src
    block = app_src.split("start_req = st.session_state.pop")[1].split(
        "# ── Main area state machine"
    )[0]
    assert "is_worker_mode()" in block
    assert "_enqueue_start_request" in block


def test_app_lifecycle_fill_skipped_in_worker_mode():
    app_src = Path("web/app.py").read_text(encoding="utf-8")
    lifecycle_guard = app_src.split("# ── Multi-run lifecycle")[1].split(
        "st.session_state[\"_parallel_lifecycle_ran\"] = False"
    )[0]
    assert "is_worker_mode()" in lifecycle_guard


def test_submit_has_worker_only_enqueue_branch():
    src = Path("web/components/sidebar.py").read_text(encoding="utf-8")
    submit = src.split("def _submit_analysis_jobs")[1].split("def _resolve_cn")[0]
    assert "is_worker_mode()" in submit
    assert "append_atomic" in submit
    # Worker branch must not set start_analysis / run in-process.
    worker_branch = submit.split("is_worker_mode()")[1].split("return")[0]
    assert "start_analysis" not in worker_branch


# ── Behavior: activate incomplete + queue read from disk ─────────────────────

def test_activate_incomplete_enqueues_to_disk_in_worker_mode(worker_env):
    history.record_incomplete_task(
        "300253", "2026-07-15", status="error", error="worker 重启"
    )
    session: dict = {ACTIVE_RUNS_KEY: []}
    action = sidebar.activate_incomplete_task(
        session, "300253", "2026-07-15", market="CN"
    )
    assert action == "enqueue"
    # Not started in-process:
    assert "start_analysis" not in session
    # Written to disk queue, not session queue:
    queued = worker_env.load()
    assert [j.ticker for j in queued] == ["300253"]
    assert history.get_incomplete_history() == []


def test_activate_incomplete_dedupes_on_disk(worker_env):
    worker_env.save([AnalysisJob(ticker="300253", trade_date="2026-07-15", market="CN")])
    history.record_incomplete_task(
        "300253", "2026-07-15", status="error", error="worker 重启"
    )
    session: dict = {ACTIVE_RUNS_KEY: []}
    sidebar.activate_incomplete_task(session, "300253", "2026-07-15", market="CN")
    queued = worker_env.load()
    assert [j.ticker for j in queued] == ["300253"]
    assert history.get_incomplete_history() == []


def test_waiting_queue_jobs_reads_disk_in_worker_mode(worker_env):
    """Sidebar must see disk jobs even when session_state was never hydrated."""
    worker_env.save(
        [AnalysisJob(ticker="601138", trade_date="2026-07-20", market="CN")]
    )
    session: dict = {}  # empty session — matches hydrate-on-empty + later enqueue
    jobs = sidebar._waiting_queue_jobs(session)
    assert [j.ticker for j in jobs] == ["601138"]
    assert QUEUE_SESSION_KEY not in session


def test_waiting_queue_jobs_uses_session_outside_worker_mode(monkeypatch, tmp_path):
    monkeypatch.delenv("TRADINGAGENTS_ANALYSIS_EXECUTOR", raising=False)
    store = AnalysisQueueStore(tmp_path / "analysis_queue.json")
    store.save([AnalysisJob(ticker="601138", trade_date="2026-07-20", market="CN")])
    monkeypatch.setattr(sidebar, "default_store", lambda: store)
    session = {
        QUEUE_SESSION_KEY: [
            AnalysisJob(ticker="300033", trade_date="2026-07-20", market="CN").to_dict()
        ]
    }
    jobs = sidebar._waiting_queue_jobs(session)
    assert [j.ticker for j in jobs] == ["300033"]


def test_queue_fragment_gates_on_waiting_helper():
    """Worker disk queue must drive the sidebar gate, not session snapshot alone."""
    src = Path("web/components/sidebar.py").read_text(encoding="utf-8")
    frag = src.split("def _render_queue_and_incomplete")[1].split(
        "def _render_analysis_queue_inner"
    )[0]
    assert "_waiting_queue_jobs" in frag
    assert "queue_snapshot(st.session_state)" not in frag
