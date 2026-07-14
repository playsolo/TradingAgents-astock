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
