"""分析进行中时进度刷新不得阻塞主脚本（影响侧栏「加入分析队列」响应）。"""

from pathlib import Path


def test_running_analysis_uses_fragment_not_sleep_rerun():
    """主路径禁止 time.sleep + st.rerun 轮询；改用 fragment(run_every)。"""
    app_src = Path("web/app.py").read_text(encoding="utf-8")
    panel_src = Path("web/components/progress_panel.py").read_text(encoding="utf-8")

    assert "time.sleep(" not in app_src, (
        "web/app.py must not block the script runner with time.sleep "
        "(sidebar clicks wait until sleep finishes)"
    )
    assert "render_running_progress" in app_src
    assert "@st.fragment" in panel_src or "st.fragment(" in panel_src
    assert "run_every" in panel_src


def test_progress_poll_interval_is_short():
    from web.components.progress_panel import PROGRESS_POLL_SECONDS

    assert 0 < PROGRESS_POLL_SECONDS <= 2


def test_app_surfaces_incomplete_tasks_after_refresh():
    """Browser refresh drops ProgressTracker; incomplete disk state must be noticed."""
    app_src = Path("web/app.py").read_text(encoding="utf-8")
    assert "list_active_incomplete_tasks" in app_src
    assert "format_refresh_incomplete_notice" in app_src
    assert "_refresh_incomplete_noticed" in app_src


def test_app_autostarts_restored_queue_when_idle():
    """刷新恢复队列后，空闲时应自动开跑，避免用户还要点「继续队列」。"""
    app_src = Path("web/app.py").read_text(encoding="utf-8")
    queue_src = Path("web/analysis_queue.py").read_text(encoding="utf-8")
    assert "maybe_autostart_restored_queue" in app_src
    assert "format_restored_queue_blocked_notice" in app_src
    assert "不会自动开跑" in queue_src  # blocked path still explains manual resume
    # Auto-start marks running via before_commit before queue pop / begin.
    assert "before_commit=_mark_autostart_running" in app_src
    marker_block = app_src.split("def _mark_autostart_running")[1].split(
        "_started = maybe_autostart_restored_queue"
    )[0]
    assert "record_incomplete_task(" in marker_block


def test_app_reruns_after_begin_so_sidebar_shows_running_task():
    """开始分析在侧栏之后才 _begin_analysis；必须 rerun 才能亮起暂停/未完成任务。"""
    app_src = Path("web/app.py").read_text(encoding="utf-8")
    begin_idx = app_src.find("_begin_analysis(start_req)")
    assert begin_idx != -1
    assert "st.rerun()" in app_src[begin_idx : begin_idx + 280]
    # In multi-run mode, the lifecycle block fills free slots then reruns.
    lifecycle = app_src.split("# ── Multi-run lifecycle")[1].split("# ── State routing")[0]
    assert "try_fill_parallel_slots" in lifecycle
    assert "st.rerun()" in lifecycle


def test_submit_records_incomplete_before_start_and_skips_double_resolve():
    """Idle start 应落盘进行中，且成功提示不得再次 resolve_ticker。"""
    sidebar_src = Path("web/components/sidebar.py").read_text(encoding="utf-8")
    submit = sidebar_src.split("def _submit_analysis_jobs")[1].split("def _resolve_cn")[0]
    assert "record_incomplete_task(" in submit
    assert "_resolve_user_input(token)" not in submit
