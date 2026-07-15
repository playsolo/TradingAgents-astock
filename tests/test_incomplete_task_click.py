"""未完成任务侧栏：忙碌时仍可点，与单任务时代“点条目继续”一致。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from web.analysis_queue import QUEUE_SESSION_KEY, AnalysisJob
from web.components import sidebar
from web.parallel_runs import ACTIVE_RUNS_KEY, FOCUSED_RUN_KEY


def test_incomplete_resume_buttons_not_globally_disabled_when_busy():
    """Having another live run must not disable the whole incomplete list."""
    src = Path("web/components/sidebar.py").read_text(encoding="utf-8")
    section = src.split('st.markdown("#### 未完成任务")', 1)[1].split(
        'st.markdown("#### 历史记录")', 1
    )[0]
    assert "disabled=is_busy" not in section
    assert "activate_incomplete_task(" in section


def test_activate_incomplete_focuses_live_run_instead_of_restarting():
    session: dict = {}
    live = SimpleNamespace(
        ticker="600688",
        trade_date="2026-07-15",
        is_running=True,
        is_complete=False,
        error=None,
        market="CN",
    )
    session[ACTIVE_RUNS_KEY] = [live]

    action = sidebar.activate_incomplete_task(
        session, "600688", "2026-07-15", market="CN"
    )

    assert action == "focus"
    assert session[FOCUSED_RUN_KEY] == "600688"
    assert "start_analysis" not in session
    assert session.get("viewing_history") is None
    assert session.get("viewing_watchlist") is False


def test_activate_incomplete_starts_when_slots_free_even_if_other_run_busy():
    session: dict = {}
    other = SimpleNamespace(
        ticker="600688",
        trade_date="2026-07-15",
        is_running=True,
        is_complete=False,
        error=None,
        market="CN",
    )
    session[ACTIVE_RUNS_KEY] = [other]

    action = sidebar.activate_incomplete_task(
        session, "301031", "2026-07-15", market="CN"
    )

    assert action == "start"
    assert session["start_analysis"] == {
        "ticker": "301031",
        "trade_date": "2026-07-15",
        "market": "CN",
    }


def test_activate_incomplete_start_drops_queued_duplicate():
    session: dict = {
        QUEUE_SESSION_KEY: [
            AnalysisJob(
                ticker="301031",
                trade_date="2026-07-15",
                market="CN",
                fresh=False,
            ).to_dict(),
            AnalysisJob(
                ticker="600000",
                trade_date="2026-07-15",
                market="CN",
            ).to_dict(),
        ]
    }

    action = sidebar.activate_incomplete_task(
        session, "301031", "2026-07-15", market="CN"
    )

    assert action == "start"
    remaining = [AnalysisJob.from_mapping(j) for j in session[QUEUE_SESSION_KEY]]
    assert [j.ticker for j in remaining] == ["600000"]


def test_activate_incomplete_enqueues_when_no_slots():
    session: dict = {}
    # Cap is 3 by default; fill with 3 running trackers.
    session[ACTIVE_RUNS_KEY] = [
        SimpleNamespace(
            ticker=f"00000{i}",
            trade_date="2026-07-15",
            is_running=True,
            is_complete=False,
            error=None,
            market="CN",
        )
        for i in range(3)
    ]

    action = sidebar.activate_incomplete_task(
        session, "301031", "2026-07-15", market="CN"
    )

    assert action == "enqueue"
    assert "start_analysis" not in session
    jobs = session.get(QUEUE_SESSION_KEY) or []
    assert len(jobs) == 1
    job = AnalysisJob.from_mapping(jobs[0])
    assert job.ticker == "301031"
    assert job.trade_date == "2026-07-15"
    assert job.market == "CN"
    assert job.fresh is False
