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
    """将候选代码排入分析队列（窄口径重叠票跳过深分析）。"""
    from tradingagents.inbox import emit_analysis_skipped
    from web.analysis_queue import (
        AnalysisJob,
        append_jobs,
        mark_serial_queue_session,
        partition_scan_jobs_for_enqueue,
    )

    today = datetime.now().strftime("%Y-%m-%d")
    jobs = [
        AnalysisJob(
            ticker=c["code"],
            trade_date=today,
            market="CN",
            fresh=True,
            source="scan",
        )
        for c in candidates
    ]
    keep, skipped = partition_scan_jobs_for_enqueue(jobs, as_of=today)
    for ticker, reason in skipped:
        emit_analysis_skipped(ticker, today, reason=reason)
        try:
            from tradingagents.analysis.skip_followup import follow_up_scan_skip
            from web.auth_page import current_watch_store

            follow_up_scan_skip(
                ticker,
                trade_date=today,
                market="CN",
                watch_store=current_watch_store(),
                llm=None,
                escalate=True,
            )
        except Exception:  # noqa: BLE001
            pass
    if not keep:
        if skipped:
            st.session_state["queue_advance_notice"] = (
                f"候选均沿用校准锚点，已跳过深分析 {len(skipped)} 只（见事件中心）"
            )
        return 0
    added = append_jobs(
        st.session_state,
        keep,
        exclude={(None, None, None)},  # 不排除已有项
    )
    if added:
        mark_serial_queue_session(st.session_state, True)
        skip_note = f"，另跳过 {len(skipped)} 只" if skipped else ""
        st.session_state["queue_advance_notice"] = (
            f"已将 {added} 只候选加入分析队列{skip_note}"
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


def _is_growth_candidate(candidate: dict) -> bool:
    return bool(
        candidate.get("track")
        or candidate.get("np_ttm_yoy") is not None
        or candidate.get("strategy") == "growth_accel"
    )


def _score_max_for(candidate: dict) -> int:
    raw = candidate.get("score_max")
    try:
        if raw is not None:
            return max(int(raw), 1)
    except (TypeError, ValueError):
        pass
    if _is_growth_candidate(candidate):
        from tradingagents.strategies.growth_accel import l2_score_max
    else:
        from tradingagents.strategies.value_swing import l2_score_max
    return l2_score_max()


def _candidate_why_line(candidate: dict) -> str:
    why = candidate.get("why")
    if isinstance(why, str) and why.strip():
        return why.strip()
    if _is_growth_candidate(candidate):
        from tradingagents.strategies.growth_accel import why_selected_line
    else:
        from tradingagents.strategies.value_swing import why_selected_line
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


def _candidate_metrics_html(candidate: dict) -> str:
    """卡片中部指标行：成长池强调利润增速，价值池强调 PE/PB。"""
    pe = float(candidate.get("pe_ttm") or 0)
    pb = float(candidate.get("pb") or 0)
    price = float(candidate.get("price") or 0)
    bits = [
        f"<span>PE {pe:.1f}x</span>",
        f"<span>PB {pb:.1f}x</span>",
        f"<span>价 {price:.2f}</span>",
    ]
    if _is_growth_candidate(candidate):
        yoy = candidate.get("np_ttm_yoy")
        if yoy is not None:
            bits.insert(0, f"<span>净利TTM {float(yoy):.0f}%</span>")
        elif candidate.get("track") == "loss":
            bits.insert(0, "<span>亏损成长</span>")
        if candidate.get("dual_pool"):
            bits.append('<span style="color:#fbbf24;">价值&成长</span>')
    return (
        '<div style="display:flex;flex-wrap:wrap;gap:10px 14px;color:#888;font-size:0.8rem;">'
        + "".join(bits)
        + "</div>"
    )


def _candidate_card_html(candidate: dict) -> str:
    """生成单张竖排磁贴 HTML（供自适应网格使用）。"""
    label, color, icon = _build_recommendation_bar(candidate)
    code = html_lib.escape(str(candidate["code"]))
    name = html_lib.escape(str(candidate.get("name") or candidate["code"]))
    score = candidate["signal_score"]
    score_max = _score_max_for(candidate)
    why = html_lib.escape(_candidate_why_line(candidate))
    analysis_html = _analysis_block_html(candidate)
    metrics = _candidate_metrics_html(candidate)

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
        f"{metrics}"
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


def _render_selection_rules(rules: dict | None = None, *, strategy: str = "value_swing") -> None:
    """展示当前（或落盘）选股规则。"""
    if isinstance(rules, dict) and rules.get("l0"):
        snap = rules
    elif strategy == "growth_accel":
        from tradingagents.strategies.growth_accel import selection_rules_snapshot

        snap = selection_rules_snapshot()
    else:
        from tradingagents.strategies.value_swing import selection_rules_snapshot

        snap = selection_rules_snapshot()
    l2 = snap.get("l2") or {}
    is_growth = strategy == "growth_accel" or snap.get("strategy") == "growth_accel"
    with st.expander("选股规则（当前口径）", expanded=False):
        c0, c1, c2, c3 = st.columns(4)
        c0.markdown("**L0 流动性**")
        for line in snap.get("l0") or []:
            c0.caption(f"• {line}")
        c1.markdown("**L1a 预筛**" if is_growth else "**L1a 估值**")
        for line in snap.get("l1a") or []:
            c1.caption(f"• {line}")
        c2.markdown("**L1 增长核**" if is_growth else "**L1b 财务**")
        l1_lines = snap.get("l1") or snap.get("l1b") or []
        for line in l1_lines:
            c2.caption(f"• {line}")
        c3.markdown("**L2 排序**" if is_growth else "**L2 催化剂**")
        c3.caption(l2.get("note") or "")
        active = l2.get("active") or []
        if active:
            c3.caption("计分：" + " · ".join(active) + f"（满分 {l2.get('score_max', '?')}）")
        dormant = l2.get("dormant") or []
        if dormant:
            c3.caption("暂未启用：" + " · ".join(dormant))


def _render_funnel_stats(result: dict, *, strategy: str = "value_swing"):
    """渲染漏斗统计。"""
    cols = st.columns(6)
    if strategy == "growth_accel" or result.get("strategy") == "growth_accel":
        metrics = [
            ("全 A 股", result["total_stocks"], ""),
            ("L0 通过", result["l0_passed"], "流动性/含亏损"),
            ("L1a 预筛", result["l1a_passed"], "成交额前 N"),
            ("L1b 通过", result["l1b_passed"], "利润增速/拐点"),
            ("L2 候选", result["l2_passed"], "前景轻加权"),
            ("耗时", f"{result['duration_seconds']:.0f}s", ""),
        ]
    else:
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


def render_scan_results(
    candidates: list[dict],
    *,
    rules: dict | None = None,
    strategy: str = "value_swing",
):
    """渲染扫描结果面板（强烈推荐/推荐/关注分组）。

    注意：不要加 ``@st.fragment``。重扫时父级会切换到进度视图；
    若结果面板仍是 fragment，Streamlit 会残留旧 DOM，再叠加
    ``time.sleep`` + ``st.rerun`` 就会整页纵向叠两层。
    """
    if not candidates:
        st.info("当前无候选股票。请点击「开始扫描」生成候选池。")
        return

    _render_selection_rules(rules, strategy=strategy)
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
        if st.button("🧠 全部入分析队列", key=f"_enq_all_{strategy}", use_container_width=True, type="primary"):
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


def _scan_detach_caption() -> None:
    st.caption(
        "扫描在独立进程运行，关闭页面 / 浏览器 / 停掉 Web 都不影响；"
        "稍后回来可继续查看进度与结果。"
    )


def _is_strategy_launching(strategy: str, status: str) -> bool:
    from tradingagents.strategies.scan_runner import is_process_alive
    from tradingagents.strategies.scan_store import SCAN_STATUS_RUNNING

    pid_key, ts_key = _launch_keys(strategy)
    launch_pid = st.session_state.get(pid_key)
    launched_at = st.session_state.get(ts_key)
    return (
        launch_pid is not None
        and launched_at is not None
        and status != SCAN_STATUS_RUNNING
        and is_process_alive(launch_pid)
        and (time.time() - launched_at) < _LAUNCH_GRACE_S
    )


@st.fragment(run_every=_POLL_INTERVAL_S)
def _render_running_scan_poll(strategy: str) -> None:
    """运行中只刷进度；结束后整页 rerun 以展示结果（禁止 sleep+rerun）。"""
    from tradingagents.strategies.scan_runner import reconcile_stale_running
    from tradingagents.strategies.scan_store import (
        SCAN_STATUS_RUNNING,
        default_store,
        resolve_strategy,
    )

    strategy = resolve_strategy(strategy)
    store = default_store(strategy)
    reconcile_stale_running(store)
    record = store.load()
    status = record.get("status")
    pid_key, ts_key = _launch_keys(strategy)

    if status == SCAN_STATUS_RUNNING:
        st.session_state.pop(pid_key, None)
        st.session_state.pop(ts_key, None)
        _render_progress(record.get("progress"))
        _scan_detach_caption()
        return

    if _is_strategy_launching(strategy, status):
        st.info("扫描进程正在启动…")
        _scan_detach_caption()
        return

    # 完成 / 失败 / 进程已退出：回到完整页面渲染结果与控件
    st.rerun()


@st.fragment(run_every=_POLL_INTERVAL_S)
def _render_dual_pool_running_poll() -> None:
    """双池扫描进行中：只显示两侧进度，不渲染旧候选（避免叠层）。"""
    from tradingagents.strategies.scan_runner import reconcile_stale_running
    from tradingagents.strategies.scan_store import (
        SCAN_STATUS_RUNNING,
        STRATEGY_GROWTH_ACCEL,
        STRATEGY_VALUE_SWING,
        default_store,
    )

    if not _any_strategy_running():
        st.rerun()
        return

    st.subheader("扫描进行中")
    left, right = st.columns(2)
    panels = (
        (left, STRATEGY_VALUE_SWING, "价值波段"),
        (right, STRATEGY_GROWTH_ACCEL, "成长加速"),
    )
    for col, strategy, title in panels:
        with col:
            st.markdown(f"**{title}**")
            store = default_store(strategy)
            reconcile_stale_running(store)
            rec = store.load()
            status = rec.get("status")
            if status == SCAN_STATUS_RUNNING:
                _render_progress(rec.get("progress"))
            elif _is_strategy_launching(strategy, status) or _session_launch_busy(
                ("both", strategy)
            ):
                st.info("扫描进程正在启动…")
            else:
                n = len((rec.get("result") or {}).get("candidates") or [])
                if n:
                    st.success(f"本池已完成（上次 {n} 只；全部结束后刷新对照）")
                else:
                    st.caption("本池尚未开始或暂无结果。")
    _scan_detach_caption()


def _render_finished_result(record: dict, *, strategy: str = "value_swing"):
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

    _render_funnel_stats(result, strategy=strategy)
    st.divider()
    render_scan_results(
        result.get("candidates", []),
        rules=result.get("rules"),
        strategy=strategy,
    )


def _launch_keys(strategy: str) -> tuple[str, str]:
    return f"_scan_launch_pid_{strategy}", f"_scan_launch_ts_{strategy}"


def render_strategy_scanner(strategy: str = "value_swing"):
    """单策略扫描主面板（独立进程 + 持久化）。"""
    from tradingagents.strategies.scan_runner import (
        reconcile_stale_running,
        start_detached_scan,
    )
    from tradingagents.strategies.scan_store import (
        SCAN_STATUS_FAILED,
        SCAN_STATUS_RUNNING,
        STRATEGY_GROWTH_ACCEL,
        default_store,
        resolve_strategy,
    )

    strategy = resolve_strategy(strategy)
    if strategy == STRATEGY_GROWTH_ACCEL:
        st.subheader("🚀 成长加速扫描")
        st.caption("三阶漏斗：全市场 → 利润TTM增速/亏损拐点 → 前景轻加权 → Top 15（不设 PE/PB 顶）")
    else:
        st.subheader("📊 价值波段扫描")
        st.caption("三阶漏斗：全市场 → 流动性/估值 → 催化剂评分 → 推荐分级")

    store = default_store(strategy)
    reconcile_stale_running(store)
    record = store.load()
    status = record.get("status")

    pid_key, ts_key = _launch_keys(strategy)
    launching = _is_strategy_launching(strategy, status)
    is_running = status == SCAN_STATUS_RUNNING or launching
    # 双池同扫时一侧先结束，另一侧仍在跑：禁止对本侧再点「开始扫描」
    other_busy = _any_strategy_running() and not is_running

    if is_running:
        # 进行中不渲染旧候选：旧 result 仍留在 store 里供失败回退，但 UI 只显示进度。
        _render_running_scan_poll(strategy)
        return

    st.session_state.pop(pid_key, None)
    st.session_state.pop(ts_key, None)

    if other_busy:
        st.info("另一策略扫描进行中，请稍候；可用顶部「价值+成长都扫」统一启动。")
    else:
        col1, col2, col3 = st.columns([2, 1, 1])
        with col1:
            max_candidates = st.number_input(
                "候选上限（仅本策略）",
                min_value=5,
                max_value=50,
                value=15,
                help="只重跑本池时生效；双池同扫请用顶部控件",
                key=f"max_cand_{strategy}",
            )
        with col2:
            run_scan = st.button(
                "🚀 仅扫本池",
                use_container_width=True,
                type="secondary",
                key=f"run_scan_{strategy}",
            )
        with col3:
            auto_enqueue = st.checkbox(
                "完成后自动入队",
                value=False,
                help="夜间无人值守：扫描完成后自动把候选排入分析队列",
                key=f"auto_enq_{strategy}",
            )

        if run_scan:
            try:
                pid = start_detached_scan(
                    max_candidates=int(max_candidates),
                    enqueue_on_success=bool(auto_enqueue),
                    strategy=strategy,
                )
                st.session_state[pid_key] = pid
                st.session_state[ts_key] = time.time()
                logger.info("%s 扫描已在独立进程启动 pid=%s", strategy, pid)
                st.rerun()
            except RuntimeError as exc:
                st.warning(str(exc))

    if status == SCAN_STATUS_FAILED:
        st.error(f"上次扫描失败: {record.get('error', '未知错误')}")

    _render_finished_result(record, strategy=strategy)


def _mark_dual_pool(candidates: list[dict], other_codes: set[str]) -> list[dict]:
    out = []
    for c in candidates:
        row = dict(c)
        code = str(c.get("code") or "").strip()
        if code in other_codes:
            row["dual_pool"] = True
        out.append(row)
    return out


def _render_dual_pool_overview():
    from tradingagents.strategies.scan_store import (
        STRATEGY_GROWTH_ACCEL,
        STRATEGY_VALUE_SWING,
        default_store,
    )

    value_rec = default_store(STRATEGY_VALUE_SWING).load()
    growth_rec = default_store(STRATEGY_GROWTH_ACCEL).load()
    value_cands = (value_rec.get("result") or {}).get("candidates") or []
    growth_cands = (growth_rec.get("result") or {}).get("candidates") or []
    v_codes = {str(c.get("code") or "") for c in value_cands}
    g_codes = {str(c.get("code") or "") for c in growth_cands}
    overlap = sorted(v_codes & g_codes - {""})

    st.subheader("双池对照")
    st.caption("价值波段与成长加速各自独立扫描；重叠标的会标注「价值&成长」。")
    if overlap:
        st.info("双池重叠：" + "、".join(overlap))
    else:
        st.caption("当前无重叠标的（或某一侧尚未扫描）。")

    left, right = st.columns(2)
    with left:
        st.markdown("**价值波段**")
        if value_cands:
            render_scan_results(
                _mark_dual_pool(value_cands, g_codes),
                rules=(value_rec.get("result") or {}).get("rules"),
                strategy=STRATEGY_VALUE_SWING,
            )
        else:
            st.info("价值波段暂无结果。")
    with right:
        st.markdown("**成长加速**")
        if growth_cands:
            render_scan_results(
                _mark_dual_pool(growth_cands, v_codes),
                rules=(growth_rec.get("result") or {}).get("rules"),
                strategy=STRATEGY_GROWTH_ACCEL,
            )
        else:
            st.info("成长加速暂无结果。")


def _session_launch_busy(keys: tuple[str, ...]) -> bool:
    """刚点击启动、落盘尚未翻到 running 时的宽限窗口。"""
    from tradingagents.strategies.scan_runner import is_process_alive

    now = time.time()
    for key in keys:
        pid = st.session_state.get(f"_scan_launch_pid_{key}")
        ts = st.session_state.get(f"_scan_launch_ts_{key}")
        if (
            pid is not None
            and ts is not None
            and is_process_alive(pid)
            and (now - ts) < _LAUNCH_GRACE_S
        ):
            return True
    return False


def _any_strategy_running() -> bool:
    from tradingagents.strategies.scan_runner import (
        is_process_alive,
        reconcile_stale_running,
    )
    from tradingagents.strategies.scan_store import (
        SCAN_STATUS_RUNNING,
        STRATEGY_GROWTH_ACCEL,
        STRATEGY_VALUE_SWING,
        default_store,
    )

    if _session_launch_busy(("both", STRATEGY_VALUE_SWING, STRATEGY_GROWTH_ACCEL)):
        return True
    for strategy in (STRATEGY_VALUE_SWING, STRATEGY_GROWTH_ACCEL):
        store = default_store(strategy)
        reconcile_stale_running(store)
        rec = store.load()
        if rec.get("status") == SCAN_STATUS_RUNNING and is_process_alive(rec.get("pid")):
            return True
    return False


def _render_both_scan_controls():
    """价值+成长同扫（独立进程顺序跑完两池）。"""
    from tradingagents.strategies.scan_runner import start_detached_scan
    from tradingagents.strategies.scan_store import STRATEGY_BOTH

    busy = _any_strategy_running()
    if busy:
        st.info("🔄 双池扫描进行中 — 下方切换「价值波段 / 成长加速」查看进度；完成后按钮会恢复。")
        return True

    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        max_candidates = st.number_input(
            "两池候选上限（各）",
            min_value=5,
            max_value=50,
            value=15,
            key="max_cand_both",
            help="价值波段与成长加速各自输出的 Top N",
        )
    with c2:
        run_both = st.button(
            "🚀 价值+成长都扫",
            use_container_width=True,
            type="primary",
            key="run_scan_both",
        )
    with c3:
        auto_enqueue = st.checkbox(
            "完成后自动入队",
            value=False,
            key="auto_enq_both",
            help="两池候选都会写入分析队列",
        )
    if run_both:
        try:
            pid = start_detached_scan(
                max_candidates=int(max_candidates),
                enqueue_on_success=bool(auto_enqueue),
                strategy=STRATEGY_BOTH,
            )
            st.session_state["_scan_launch_pid_both"] = pid
            st.session_state["_scan_launch_ts_both"] = time.time()
            logger.info("双池扫描已启动 pid=%s", pid)
            st.rerun()
        except RuntimeError as exc:
            st.warning(str(exc))
    return False


def render_value_swing_scanner():
    """首页策略扫描：价值波段 / 成长加速 / 双池对照（radio 互斥，避免 tabs 叠层）。"""
    from tradingagents.strategies.scan_store import (
        STRATEGY_GROWTH_ACCEL,
        STRATEGY_VALUE_SWING,
    )

    st.header("📊 策略扫描")
    st.caption("推荐用顶部一键扫两池；下方可查看进度或单独重跑某一侧。")
    both_busy = _render_both_scan_controls()
    st.divider()
    mode = st.radio(
        "查看池",
        options=["双池对照", "价值波段", "成长加速"],
        horizontal=True,
        key="strategy_scan_pool_mode",
        help="两套漏斗彼此独立；重叠标的在双池对照中标注。",
    )
    if mode == "成长加速":
        render_strategy_scanner(STRATEGY_GROWTH_ACCEL)
    elif mode == "价值波段":
        render_strategy_scanner(STRATEGY_VALUE_SWING)
    elif both_busy:
        # 进行中只刷进度，不画旧候选；避免 fragment 残留 + sleep/rerun 叠层
        _render_dual_pool_running_poll()
    else:
        _render_dual_pool_overview()
