"""Streamlit 价值波段扫描面板。

用法：在 app.py 主布局中调用 ``render_value_swing_scanner()``。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

import streamlit as st

logger = logging.getLogger(__name__)

# 扫描进程启动后到子进程写入 running 状态之间的宽限窗口（秒）。
_LAUNCH_GRACE_S = 20
# 运行中面板自动轮询间隔（秒）。
_POLL_INTERVAL_S = 2


def _get_code_to_name():
    """从 a_stock 获取代码→名称映射。"""
    from tradingagents.dataflows.a_stock import _build_name_code_map
    _, c2n = _build_name_code_map()
    return c2n



def _enqueue_candidates(candidates: list[dict]):
    """将候选代码排入分析队列。"""
    from web.analysis_queue import (
        AnalysisJob,
        append_jobs,
        mark_serial_queue_session,
    )

    today = datetime.now().strftime("%Y-%m-%d")
    jobs = [
        AnalysisJob(ticker=c["code"], trade_date=today, market="CN", fresh=True)
        for c in candidates
    ]
    added = append_jobs(
        st.session_state,
        jobs,
        exclude={(None, None, None)},  # 不排除已有项
    )
    if added:
        mark_serial_queue_session(st.session_state, True)
        st.session_state["queue_advance_notice"] = (
            f"已将 {added} 只候选股票加入分析队列，准备逐个深入分析"
        )
    return added


def _build_recommendation_bar(candidate: dict) -> tuple[str, str, str]:
    """根据 L2 信号分构建中文推荐标签 + 颜色 + 图标。

    Returns (label, color_hex, icon).
    """
    score = candidate["signal_score"]
    if score >= 5:
        return "强烈推荐", "#ff5a1f", "🥇"
    elif score >= 3:
        return "推荐", "#ff8c42", "🥈"
    elif score >= 1:
        return "关注", "#888888", "📋"
    return "待观察", "#555555", "🔍"


def _render_candidate_card(candidate: dict, index: int):
    """渲染一张候选股票卡片。"""
    label, color, icon = _build_recommendation_bar(candidate)
    code = candidate["code"]
    name = candidate.get("name") or code
    score = candidate["signal_score"]

    # 信号标签
    signals = []
    if candidate["above_ma20"]:
        signals.append("站上MA20")
    if candidate["near_ma250"]:
        signals.append("接近年线")
    if candidate.get("revenue_growth") and candidate["revenue_growth"] > 0:
        signals.append(f"营收+{candidate['revenue_growth']:.0f}%")
    if candidate.get("debt_ratio") is not None:
        signals.append(f"负债{candidate['debt_ratio']:.0f}%")
    if candidate.get("news_found"):
        signals.append("📰有新闻")
    if candidate.get("hot_topic_match"):
        signals.append("🔥热点题材")
    if candidate.get("concept_active"):
        signals.append("🧠概念活跃")

    st.markdown(
        f"""
        <div style="
            background: linear-gradient(135deg, #1a1a1a, #222222);
            border: 1px solid {color}44;
            border-radius: 12px;
            padding: 16px;
            margin-bottom: 12px;
        ">
            <div style="display: flex; justify-content: space-between; align-items: center;">
                <div>
                    <span style="font-size: 1.4rem;">{icon}</span>
                    <span style="
                        color: {color};
                        font-weight: 700;
                        font-size: 0.85rem;
                        background: {color}22;
                        padding: 2px 10px;
                        border-radius: 20px;
                        margin-left: 8px;
                    ">{label}</span>
                    <span style="
                        color: #ffffff;
                        font-weight: 700;
                        font-size: 1.1rem;
                        margin-left: 10px;
                    ">{code}</span>
                    <span style="color: #aaaaaa; font-size: 0.9rem; margin-left: 6px;">{name}</span>
                </div>
                <div style="color: {color}; font-weight: 700; font-size: 1.2rem;">
                    信号 {score}/10
                </div>
            </div>
            <div style="display: flex; gap: 16px; margin-top: 10px; flex-wrap: wrap;">
                <span style="color: #888; font-size: 0.8rem;">PE {candidate['pe_ttm']:.1f}x</span>
                <span style="color: #888; font-size: 0.8rem;">PB {candidate['pb']:.1f}x</span>
                <span style="color: #888; font-size: 0.8rem;">价 {candidate['price']:.2f}</span>
                {''.join(f'<span style="color: #ff8c42; font-size: 0.8rem;">{s}</span>' for s in signals[:4])}
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_funnel_stats(result: dict):
    """渲染漏斗统计。"""
    cols = st.columns(6)
    metrics = [
        ("全 A 股", result["total_stocks"], ""),
        ("L0 通过", result["l0_passed"], "流动性/非ST"),
        ("L1a 通过", result["l1a_passed"], "PE/PB快速估值"),
        ("L1b 通过", result["l1b_passed"], "财务验证"),
        ("L2 候选", result["l2_passed"], "催化剂评分"),
        ("耗时", f"{result['duration_seconds']:.0f}s", ""),
    ]
    for col, (label, value, help_text) in zip(cols, metrics):
        with col:
            st.metric(label=label, value=value, help=help_text)


@st.fragment
def render_scan_results(candidates: list[dict]):
    """渲染扫描结果面板（强烈推荐/推荐/关注分组）。"""
    if not candidates:
        st.info("当前无候选股票。请点击「开始扫描」生成候选池。")
        return

    # 分组
    strong_buy: list[dict] = []
    buy: list[dict] = []
    watch: list[dict] = []
    other: list[dict] = []

    for c in candidates:
        label, _, _ = _build_recommendation_bar(c)
        if label == "强烈推荐":
            strong_buy.append(c)
        elif label == "推荐":
            buy.append(c)
        elif label == "关注":
            watch.append(c)
        else:
            other.append(c)

    # 强烈推荐
    if strong_buy:
        st.subheader("🥇 强烈推荐")
        for i, c in enumerate(strong_buy):
            _render_candidate_card(c, i)
        st.divider()

    # 推荐
    if buy:
        st.subheader("🥈 推荐")
        for i, c in enumerate(buy):
            _render_candidate_card(c, i)
        st.divider()

    # 关注
    if watch:
        st.subheader("📋 关注")
        with st.expander(f"展开 {len(watch)} 只关注股票", expanded=False):
            for i, c in enumerate(watch):
                _render_candidate_card(c, i)

    # 其他
    if other:
        with st.expander(f"待观察 ({len(other)} 只)", expanded=False):
            for i, c in enumerate(other):
                _render_candidate_card(c, i)

    # 操作按钮
    col1, col2 = st.columns([1, 5])
    with col1:
        if st.button("🧠 全部入分析队列", use_container_width=True, type="primary"):
            added = _enqueue_candidates(candidates)
            if added:
                st.success(f"已加入 {added} 只到分析队列")
                st.rerun()

    with col2:
        total = len(strong_buy) + len(buy) + len(watch)
        st.caption(
            f"共 {len(candidates)} 只候选，其中 {total} 只有明确推荐信号。"
            "点击「全部入分析队列」自动排队进行深度多 Agent 分析。"
        )


_STAGE_ORDER = ("L0", "L1a", "L1b", "L2", "done")


def _render_progress(progress: dict | None):
    """渲染运行中进度：阶段 + 各阶通过数 + 百分比 + 当前个股。"""
    if not progress:
        st.info("扫描进程已启动，正在初始化全市场种子…")
        return

    percent = int(progress.get("percent", 0))
    stage = progress.get("stage", "")
    label = progress.get("stage_label", stage)
    stage_pos = (_STAGE_ORDER.index(stage) + 1) if stage in _STAGE_ORDER else 0
    caption = f"{percent}% · 阶段 {stage_pos}/{len(_STAGE_ORDER)}：{label}"
    st.progress(min(max(percent, 0), 100) / 100.0, text=caption)

    cols = st.columns(5)
    funnel = [
        ("全 A 股", progress.get("total_stocks", 0)),
        ("L0 通过", progress.get("l0_passed", 0)),
        ("L1a 通过", progress.get("l1a_passed", 0)),
        ("L1b 通过", progress.get("l1b_passed", 0)),
        ("L2 候选", progress.get("l2_passed", 0)),
    ]
    for col, (name, value) in zip(cols, funnel):
        with col:
            st.metric(label=name, value=value)

    code = progress.get("current_code")
    if code:
        name = progress.get("current_name") or ""
        idx = progress.get("stage_index") or 0
        total = progress.get("stage_total") or 0
        pos = f"（{idx}/{total}）" if total else ""
        st.caption(f"🔎 正在验证：{code} {name} {pos}")


def _render_finished_result(record: dict):
    """渲染最近一次完成扫描的结果（来自持久化存储）。"""
    from tradingagents.strategies.scan_store import SCAN_STATUS_COMPLETED

    result = record.get("result")
    if not result:
        st.info("尚未运行扫描。设置候选上限后点击「开始扫描」。")
        return

    if record.get("status") == SCAN_STATUS_COMPLETED:
        finished = record.get("finished_at")
        if finished:
            note = f"上次扫描完成于 {finished}"
            enqueued = record.get("enqueued")
            if enqueued:
                note += f" · 已自动入队 {enqueued} 只"
            st.caption(note)
    else:
        st.caption("下方为上一次成功扫描的结果。")

    _render_funnel_stats(result)
    st.divider()
    render_scan_results(result.get("candidates", []))


def render_value_swing_scanner():
    """价值波段扫描主面板（独立进程 + 持久化，关闭页面/浏览器不影响扫描）。"""
    from tradingagents.strategies.scan_runner import (
        is_process_alive,
        reconcile_stale_running,
        start_detached_scan,
    )
    from tradingagents.strategies.scan_store import (
        SCAN_STATUS_FAILED,
        SCAN_STATUS_RUNNING,
        default_store,
    )

    st.header("📊 价值波段扫描")
    st.caption("三阶漏斗：全市场 → 流动性/估值 → 催化剂评分 → 推荐分级")

    store = default_store()
    # 若上次运行的进程已死但状态仍为 running，标记失败，避免卡死。
    reconcile_stale_running(store)
    record = store.load()
    status = record.get("status")

    # 「启动中」宽限：仅当刚拉起的子进程仍存活且状态尚未翻到 running 时才成立，
    # 这样启动瞬间崩溃的扫描会立刻显示失败，而不是卡在「启动中」。
    launch_pid = st.session_state.get("_scan_launch_pid")
    launched_at = st.session_state.get("_scan_launch_ts")
    launching = (
        launch_pid is not None
        and launched_at is not None
        and status != SCAN_STATUS_RUNNING
        and is_process_alive(launch_pid)
        and (time.time() - launched_at) < _LAUNCH_GRACE_S
    )
    is_running = status == SCAN_STATUS_RUNNING or launching

    # 操作栏
    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        max_candidates = st.number_input(
            "候选上限",
            min_value=5, max_value=50, value=15,
            help="L2 最终输出的候选股票数量上限",
            disabled=is_running,
        )
    with col2:
        run_scan = st.button(
            "🚀 开始扫描",
            use_container_width=True,
            type="primary",
            disabled=is_running,
        )
    with col3:
        auto_enqueue = st.checkbox(
            "完成后自动入队",
            value=False,
            disabled=is_running,
            help="夜间无人值守：扫描完成后自动把候选排入分析队列",
        )

    if run_scan:
        try:
            pid = start_detached_scan(
                max_candidates=int(max_candidates),
                enqueue_on_success=bool(auto_enqueue),
            )
            st.session_state["_scan_launch_pid"] = pid
            st.session_state["_scan_launch_ts"] = time.time()
            logger.info("价值波段扫描已在独立进程启动 pid=%s", pid)
            st.rerun()
        except RuntimeError as exc:
            st.warning(str(exc))

    if is_running:
        if status == SCAN_STATUS_RUNNING:
            st.session_state.pop("_scan_launch_pid", None)
            st.session_state.pop("_scan_launch_ts", None)
            _render_progress(record.get("progress"))
        else:
            st.info("扫描进程正在启动…")
        st.caption(
            "扫描在独立进程运行，关闭页面 / 浏览器 / 停掉 Web 都不影响；"
            "稍后回来可继续查看进度与结果。"
        )
        time.sleep(_POLL_INTERVAL_S)
        st.rerun()
        return

    # 非运行态：清除启动标记
    st.session_state.pop("_scan_launch_pid", None)
    st.session_state.pop("_scan_launch_ts", None)

    if status == SCAN_STATUS_FAILED:
        st.error(f"上次扫描失败: {record.get('error', '未知错误')}")

    _render_finished_result(record)
