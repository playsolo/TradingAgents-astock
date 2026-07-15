"""Real-time progress display for the analysis pipeline.

Both single-run (legacy) and multi-run (parallel) render functions.
"""

from __future__ import annotations

import streamlit as st

from web.parallel_runs import RunSnapshot
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


def format_run_card_html(snap: RunSnapshot, focused: bool) -> str:
    """Build the compact run-card HTML for one snapshot.

    Kept blank-line-free on purpose: Streamlit's markdown renderer terminates an
    HTML block at the first blank (or whitespace-only) line and then prints the
    remaining tags as literal text. An empty ``final_signal`` used to leave such
    a whitespace-only line, which leaked raw ``</span>`` tags into the UI.
    """
    card_bg = "#1a1a2e" if focused else "#161616"
    border = "1px solid #ff5a1f" if focused else "1px solid #2a2a2a"
    market_tag = "美股" if snap.market == "US" else "A股"
    status_icon = "🟢" if snap.is_running else "✅" if snap.is_complete else "🔴"
    signal_preview = f" · {snap.final_signal}" if snap.final_signal else ""
    meta = f"{market_tag} · {snap.trade_date}{signal_preview}"

    lines = [
        f'<div style="background:{card_bg}; border:{border}; border-radius:8px; padding:0.6rem 1rem; margin:0.3rem 0;">',
        '<div style="display:flex; justify-content:space-between; align-items:center;">',
        '<span style="font-size:1rem; font-weight:600; color:#f5f1eb;">',
        f"{status_icon} {snap.ticker}",
        f'<span style="font-size:0.75rem; color:#888; margin-left:0.5rem;">{meta}</span>',
        "</span>",
        f'<span style="font-size:0.8rem; color:#666;">{_format_time(snap.elapsed)}</span>',
        "</div>",
        "</div>",
    ]
    return "\n".join(lines)


def _split_stages(stages: list[dict[str, str]]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Analyst stages first (report_key ends with _report except debate etc.), then pipeline."""
    analyst_ids = {"market", "social", "news", "fundamentals", "policy", "hot_money", "lockup"}
    analysts = [s for s in stages if s["id"] in analyst_ids]
    post = [s for s in stages if s["id"] not in analyst_ids]
    if not analysts:
        mid = max(1, len(stages) // 2)
        return stages[:mid], stages[mid:]
    return analysts, post


def _render_single_progress(tracker: ProgressTracker) -> None:
    """Render the pipeline progress panel for one tracker."""
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


@st.fragment(run_every=PROGRESS_POLL_SECONDS)
def render_running_progress() -> None:
    """Legacy single-tracker fragment — kept for backward compat.

    In parallel mode the new ``render_multi_progress`` fragment is used instead.
    """
    tracker = st.session_state.get("tracker")
    if tracker is None:
        return
    if not tracker.is_running:
        st.rerun()
        return
    _render_single_progress(tracker)


@st.fragment(run_every=PROGRESS_POLL_SECONDS)
def render_multi_progress() -> None:
    """Render progress cards for all active runs.

    Each run shows a compact progress card. The focused run gets expanded detail.
    """
    from web.parallel_runs import (
        active_runs,
        focused_ticker,
        set_focused_ticker,
        take_snapshots,
    )

    snapshots = take_snapshots(st.session_state)
    if not snapshots:
        st.info("无进行中的分析任务。")
        return

    focus = focused_ticker(st.session_state) or snapshots[0].ticker

    st.markdown(
        f"""
        <div style="text-align:center; margin:0.5rem 0;">
            <span style="font-size:1.4rem; font-weight:700; color:#f5f1eb;">
                并行分析中
            </span>
            <span style="font-size:0.9rem; color:#888; margin-left:0.5rem;">
                {sum(1 for s in snapshots if s.is_running)} running
                · {sum(1 for s in snapshots if s.is_complete)} done
            </span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # ── Compact cards for each run ──────────────────────────────────────
    for snap in snapshots:
        is_focused = snap.ticker == focus
        pct = len(snap.completed_stages) / max(snap.total_stages, 1)

        with st.container():
            st.markdown(
                format_run_card_html(snap, focused=is_focused),
                unsafe_allow_html=True,
            )
            st.progress(
                pct,
                text=f"{len(snap.completed_stages)}/{snap.total_stages} stages · LLM {snap.llm_calls}",
            )

            if snap.is_paused:
                st.caption("已暂停")
            if snap.error:
                st.error(snap.error)

            # Focus button
            if is_focused:
                col_info = st.columns([1, 1, 1, 1, 1])
                col_info[0].metric("LLM", snap.llm_calls)
                col_info[1].metric("Tools", snap.tool_calls)
                col_info[2].metric("Tok In", f"{snap.tokens_in:,}")
                col_info[3].metric("Tok Out", f"{snap.tokens_out:,}")

                # Show stage reports if available
                if snap.stage_reports:
                    with st.expander("阶段报告", expanded=False):
                        for stage_id, report in list(snap.stage_reports.items())[-3:]:
                            st.markdown(f"**{stage_id}**")
                            st.markdown(report[:1500])
            else:
                if st.button(f"聚焦 {snap.ticker}", key=f"focus_{snap.ticker}"):
                    set_focused_ticker(st.session_state, snap.ticker)
                    st.rerun()

            st.divider()

    # ── If nothing is running but snapshots remain, rerun to show reports ─
    if all(not s.is_running for s in snapshots):
        st.rerun()

