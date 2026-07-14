"""Real-time progress display for the analysis pipeline."""

from __future__ import annotations

import streamlit as st

from web.progress import PIPELINE_STAGES, ProgressTracker

# Fragment auto-refresh interval. Must stay short: full-app sleep+rerun blocked
# sidebar clicks (enqueue) for up to this long on every poll tick.
PROGRESS_POLL_SECONDS = 2


def _status_badge(status: str) -> str:
    if status == "done":
        return '<span style="color:#22c55e; font-size:1.3rem;">●</span>'
    if status == "active":
        return '<span style="color:#ff5a1f; font-size:1.3rem;">◉</span>'
    return '<span style="color:#333; font-size:1.3rem;">○</span>'


def _format_time(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def _split_stages(stages: list[dict[str, str]]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Analyst stages first (report_key ends with _report except debate etc.), then pipeline."""
    analyst_ids = {"market", "social", "news", "fundamentals", "policy", "hot_money", "lockup"}
    analysts = [s for s in stages if s["id"] in analyst_ids]
    post = [s for s in stages if s["id"] not in analyst_ids]
    if not analysts:
        # Fallback: first half / second half
        mid = max(1, len(stages) // 2)
        return stages[:mid], stages[mid:]
    return analysts, post


@st.fragment(run_every=PROGRESS_POLL_SECONDS)
def render_running_progress() -> None:
    """Poll progress without blocking the script runner.

    Unlike ``time.sleep`` + ``st.rerun()``, fragment ticks only re-run this
    block, so sidebar widget clicks (e.g. 加入分析队列) are handled immediately
    on a full-app rerun instead of waiting for the poll sleep.
    """
    tracker = st.session_state.get("tracker")
    if tracker is None:
        return
    if not tracker.is_running:
        # Finished / errored during a fragment tick — refresh full app for
        # report / error / queue advance UI.
        st.rerun()
        return
    render_progress(tracker)


def render_progress(tracker: ProgressTracker) -> None:
    """Render the pipeline progress panel."""

    stages = list(tracker.stages) if tracker.stages else list(PIPELINE_STAGES)
    market_tag = "美股" if getattr(tracker, "market", "CN") == "US" else "A股"

    st.markdown(
        f"""
        <div style="text-align:center; margin:1rem 0 0.5rem;">
            <span style="font-size:1.6rem; font-weight:700; color:#f5f1eb;">
                分析进行中
            </span>
            <span style="font-size:1.1rem; color:#888; margin-left:0.8rem;">
                {tracker.ticker}
            </span>
            <span style="font-size:0.85rem; color:#666; margin-left:0.5rem;">
                · {market_tag}
            </span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if tracker.stop_requested:
        st.caption("正在停止当前分析并清空内容；收尾完成后可重新开始。")
        return

    if tracker.is_paused:
        st.caption("当前分析已暂停。")

    completed = len(tracker.completed_stages)
    total = len(stages)
    pct = completed / total if total else 0
    st.progress(pct, text=f"{completed}/{total} 阶段完成  ·  {_format_time(tracker.elapsed)}")

    analyst_stages, post_stages = _split_stages(stages)

    st.markdown(
        '<div style="margin:0.5rem 0 0.3rem; font-size:0.85rem; color:#888;">ANALYSTS</div>',
        unsafe_allow_html=True,
    )

    cols = st.columns(max(len(analyst_stages), 1))
    for col, stage in zip(cols, analyst_stages):
        status = tracker.stage_status(stage["id"])
        badge = _status_badge(status)
        label_color = "#f5f1eb" if status == "active" else "#888" if status == "pending" else "#22c55e"
        col.markdown(
            f"""
            <div style="text-align:center; padding:0.5rem 0;">
                {badge}<br>
                <span style="font-size:0.75rem; color:{label_color};">{stage['name']}</span>
            </div>
            """,
            unsafe_allow_html=True,
        )

    if post_stages:
        st.markdown(
            '<div style="margin:0.8rem 0 0.3rem; font-size:0.85rem; color:#888;">PIPELINE</div>',
            unsafe_allow_html=True,
        )

        cols2 = st.columns(len(post_stages))
        for col, stage in zip(cols2, post_stages):
            status = tracker.stage_status(stage["id"])
            badge = _status_badge(status)
            label_color = "#f5f1eb" if status == "active" else "#888" if status == "pending" else "#22c55e"
            col.markdown(
                f"""
                <div style="text-align:center; padding:0.5rem 0;">
                    {badge}<br>
                    <span style="font-size:0.75rem; color:{label_color};">{stage['name']}</span>
                </div>
                """,
                unsafe_allow_html=True,
            )

    st.markdown("---")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("LLM 调用", tracker.llm_calls)
    c2.metric("工具调用", tracker.tool_calls)
    c3.metric("输入 Tokens", f"{tracker.tokens_in:,}")
    c4.metric("输出 Tokens", f"{tracker.tokens_out:,}")

    if tracker.error:
        st.error(f"错误: {tracker.error}")

    completed_reports = [
        (stage["name"], stage["icon"], tracker.stage_reports[stage["id"]])
        for stage in stages
        if stage["id"] in tracker.stage_reports
    ]

    if completed_reports:
        st.markdown(
            '<div style="margin:0.5rem 0 0.3rem; font-size:0.85rem; color:#888;">'
            f"REPORTS ({len(completed_reports)})</div>",
            unsafe_allow_html=True,
        )
        for name, icon, report in reversed(completed_reports):
            is_latest = (name == completed_reports[-1][0])
            with st.expander(f"{icon} {name}", expanded=is_latest):
                st.markdown(report[:3000])
