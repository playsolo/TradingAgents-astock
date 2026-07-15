"""Streamlit 价值波段扫描面板。

用法：在 app.py 主布局中调用 ``render_value_swing_scanner()``。
"""

from __future__ import annotations

import html as html_lib
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


# 候选磁贴网格：按主栏宽度 auto-fill，最多 3 列。
_CARD_GRID_MIN_PX = 340
_CARD_GRID_MAX_COLS = 3
_CARD_GRID_GAP_PX = 12


def attach_analysis_to_candidates(candidates: list[dict]) -> list[dict]:
    """把最近一次深度分析的操作建议挂到扫描候选上（不改写信号分）。"""
    from web.history import lookup_latest_action_plans

    if not candidates:
        return []
    plans = lookup_latest_action_plans([c.get("code", "") for c in candidates])
    out: list[dict] = []
    for c in candidates:
        row = dict(c)
        code = str(c.get("code") or "").strip().upper()
        plan = plans.get(code)
        if plan:
            row["analysis"] = plan
        out.append(row)
    return out


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


def _score_max_for(candidate: dict) -> int:
    from tradingagents.strategies.value_swing import l2_score_max

    raw = candidate.get("score_max")
    try:
        if raw is not None:
            return max(int(raw), 1)
    except (TypeError, ValueError):
        pass
    return l2_score_max()


def _candidate_why_line(candidate: dict) -> str:
    from tradingagents.strategies.value_swing import why_selected_line

    why = candidate.get("why")
    if isinstance(why, str) and why.strip():
        return why.strip()
    return why_selected_line(candidate)


def _analysis_vs_scan_note(scan_label: str, rating: str | None) -> str | None:
    """扫描分级与深度评级冲突时提示（不覆盖任一侧）。"""
    if not rating:
        return None
    r = rating.strip().lower()
    bearish = r in ("sell", "underweight")
    bullish_scan = scan_label in ("强烈推荐", "推荐")
    if bearish and bullish_scan:
        return "与扫描信号分歧"
    bullish = r in ("buy", "overweight")
    if bullish and scan_label == "待观察":
        return "分析偏多·扫描分低"
    return None


def _analysis_block_html(candidate: dict) -> str:
    """卡片底部：已完成分析时回填操作建议摘要。"""
    from web.components.report_viewer import action_plan_rating_style
    from web.navigation import shareable_path

    analysis = candidate.get("analysis")
    if not isinstance(analysis, dict):
        return (
            '<div style="margin-top:4px;padding-top:8px;border-top:1px solid #333;'
            'color:#666;font-size:0.75rem;">尚未深度分析</div>'
        )

    rating = analysis.get("rating")
    scan_label, _, _ = _build_recommendation_bar(candidate)
    note = _analysis_vs_scan_note(scan_label, rating if isinstance(rating, str) else None)

    if rating:
        color, rating_cn = action_plan_rating_style(str(rating))
    else:
        signal = str(analysis.get("signal") or "N/A")
        color, rating_cn = action_plan_rating_style(
            {"Buy": "Buy", "Sell": "Sell", "Hold": "Hold"}.get(signal, signal)
        )
        if signal == "N/A":
            rating_cn = "已分析"

    horizon = html_lib.escape(str(analysis.get("horizon") or "").strip())
    summary = html_lib.escape(str(analysis.get("summary") or "").strip())
    if len(summary) > 72:
        summary = summary[:72] + "…"
    date = html_lib.escape(str(analysis.get("date") or ""))
    non_holders = html_lib.escape(str(analysis.get("non_holders_action") or "").strip())

    ticker = str(candidate.get("code") or "")
    href = ""
    if analysis.get("date") and ticker:
        href = html_lib.escape(shareable_path("history", ticker=ticker, date=str(analysis["date"])))

    note_html = (
        f'<span style="color:#fbbf24;margin-left:6px;">{html_lib.escape(note)}</span>'
        if note
        else ""
    )
    meta_bits = [x for x in (horizon and f"时效 {horizon}", date and f"分析日 {date}") if x]
    meta = " · ".join(meta_bits)
    detail = non_holders or summary
    link = (
        f'<a href="{href}" target="_self" style="color:{color};text-decoration:underline;">查看报告</a>'
        if href
        else ""
    )

    parts = [
        '<div style="margin-top:4px;padding-top:8px;border-top:1px solid #333;">',
        f'<div style="font-size:0.75rem;color:#aaa;">操作建议 '
        f'<span style="color:{color};font-weight:700;">{html_lib.escape(rating_cn)}</span>'
        f"{note_html}</div>",
    ]
    if detail:
        parts.append(
            f'<div style="color:#ccc;font-size:0.75rem;margin-top:4px;line-height:1.35;">'
            f"{detail}</div>"
        )
    if meta or link:
        parts.append(
            f'<div style="color:#777;font-size:0.7rem;margin-top:4px;display:flex;'
            f'justify-content:space-between;gap:8px;flex-wrap:wrap;">'
            f"<span>{meta}</span><span>{link}</span></div>"
        )
    parts.append("</div>")
    return "".join(parts)


def _candidate_card_html(candidate: dict) -> str:
    """生成单张竖排磁贴 HTML（供自适应网格使用）。"""
    label, color, icon = _build_recommendation_bar(candidate)
    code = html_lib.escape(str(candidate["code"]))
    name = html_lib.escape(str(candidate.get("name") or candidate["code"]))
    score = candidate["signal_score"]
    score_max = _score_max_for(candidate)
    why = html_lib.escape(_candidate_why_line(candidate))
    analysis_html = _analysis_block_html(candidate)

    # 禁止空行：Streamlit markdown 会在空行处截断 HTML 块。
    return (
        f'<div style="background:linear-gradient(160deg,#1a1a1a,#222);border:1px solid {color}44;'
        f'border-radius:12px;padding:14px 16px;height:100%;box-sizing:border-box;'
        f'display:flex;flex-direction:column;gap:8px;">'
        f'<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;">'
        f'<span style="color:{color};font-weight:700;font-size:0.75rem;background:{color}22;'
        f'padding:2px 8px;border-radius:999px;white-space:nowrap;">'
        f"{icon} {html_lib.escape(label)}</span>"
        f'<span style="color:{color};font-weight:700;font-size:1.05rem;white-space:nowrap;">'
        f"信号 {score}/{score_max}</span></div>"
        f"<div><div style=\"color:#fff;font-weight:700;font-size:1.15rem;line-height:1.2;\">{code}</div>"
        f'<div style="color:#aaa;font-size:0.85rem;margin-top:2px;overflow:hidden;'
        f'text-overflow:ellipsis;white-space:nowrap;">{name}</div></div>'
        f'<div style="display:flex;flex-wrap:wrap;gap:10px 14px;color:#888;font-size:0.8rem;">'
        f"<span>PE {candidate['pe_ttm']:.1f}x</span>"
        f"<span>PB {candidate['pb']:.1f}x</span>"
        f"<span>价 {candidate['price']:.2f}</span></div>"
        f'<div style="color:#bbb;font-size:0.75rem;line-height:1.4;">'
        f'<span style="color:#888;">入选原因</span> {why}</div>'
        f"{analysis_html}</div>"
    )


def _candidate_grid_html(candidates: list[dict]) -> str:
    """把多张磁贴包进按可视宽度自适应（最多 3 列）的 CSS Grid。"""
    if not candidates:
        return ""
    min_track = (
        f"max({_CARD_GRID_MIN_PX}px, "
        f"calc(100% / {_CARD_GRID_MAX_COLS} - {_CARD_GRID_GAP_PX}px))"
    )
    cards = "".join(_candidate_card_html(c) for c in candidates)
    return (
        f'<div style="display:grid;grid-template-columns:repeat(auto-fill, minmax({min_track}, 1fr));'
        f'gap:{_CARD_GRID_GAP_PX}px;margin-bottom:8px;">{cards}</div>'
    )


def _render_candidate_grid(candidates: list[dict]) -> None:
    """渲染一组候选股票为自适应多列网格。"""
    html = _candidate_grid_html(candidates)
    if html:
        st.markdown(html, unsafe_allow_html=True)


def _render_selection_rules(rules: dict | None = None) -> None:
    """展示当前（或落盘）选股规则。"""
    from tradingagents.strategies.value_swing import selection_rules_snapshot

    snap = rules if isinstance(rules, dict) and rules.get("l0") else selection_rules_snapshot()
    l2 = snap.get("l2") or {}
    with st.expander("选股规则（当前口径）", expanded=False):
        c0, c1, c2, c3 = st.columns(4)
        c0.markdown("**L0 流动性**")
        for line in snap.get("l0") or []:
            c0.caption(f"• {line}")
        c1.markdown("**L1a 估值**")
        for line in snap.get("l1a") or []:
            c1.caption(f"• {line}")
        c2.markdown("**L1b 财务**")
        for line in snap.get("l1b") or []:
            c2.caption(f"• {line}")
        c3.markdown("**L2 催化剂**")
        c3.caption(l2.get("note") or "")
        active = l2.get("active") or []
        if active:
            c3.caption("计分：" + " · ".join(active) + f"（满分 {l2.get('score_max', '?')}）")
        dormant = l2.get("dormant") or []
        if dormant:
            c3.caption("暂未启用：" + " · ".join(dormant))


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
def render_scan_results(candidates: list[dict], *, rules: dict | None = None):
    """渲染扫描结果面板（强烈推荐/推荐/关注分组）。"""
    if not candidates:
        st.info("当前无候选股票。请点击「开始扫描」生成候选池。")
        return

    _render_selection_rules(rules)
    candidates = attach_analysis_to_candidates(candidates)

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

    analyzed = sum(1 for c in candidates if c.get("analysis"))

    # 强烈推荐
    if strong_buy:
        st.subheader("🥇 强烈推荐")
        _render_candidate_grid(strong_buy)
        st.divider()

    # 推荐
    if buy:
        st.subheader("🥈 推荐")
        _render_candidate_grid(buy)
        st.divider()

    # 关注
    if watch:
        st.subheader("📋 关注")
        with st.expander(f"展开 {len(watch)} 只关注股票", expanded=False):
            _render_candidate_grid(watch)

    # 其他
    if other:
        with st.expander(f"待观察 ({len(other)} 只)", expanded=False):
            _render_candidate_grid(other)

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
            f"共 {len(candidates)} 只候选，其中 {total} 只有明确推荐信号；"
            f"已回填操作建议 {analyzed} 只。"
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
    render_scan_results(
        result.get("candidates", []),
        rules=result.get("rules"),
    )


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
