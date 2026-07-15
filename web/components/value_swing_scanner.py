"""Streamlit 价值波段扫描面板。

用法：在 app.py 主布局中调用 ``render_value_swing_scanner()``。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime

import streamlit as st

logger = logging.getLogger(__name__)


def _get_code_to_name():
    """从 a_stock 获取代码→名称映射。"""
    from tradingagents.dataflows.a_stock import _build_name_code_map
    _, c2n = _build_name_code_map()
    return c2n


def _run_scan(max_candidates: int) -> dict:
    """在后台线程中运行扫描，返回结果。"""
    from tradingagents.strategies.value_swing import (
        ScanResult,
        run_value_swing_scan,
    )
    try:
        result: ScanResult = run_value_swing_scan(max_candidates=max_candidates)
        return {
            "ok": True,
            "scan_date": result.scan_date,
            "total_stocks": result.total_stocks,
            "l0_passed": result.l0_passed,
            "l1_passed": result.l1_passed,
            "l2_passed": result.l2_passed,
            "candidates": [
                {
                    "code": c.code,
                    "name": c.name,
                    "price": c.price,
                    "pe_ttm": c.pe_ttm,
                    "pb": c.pb,
                    "peg": c.peg,
                    "signal_score": c.signal_score,
                    "debt_ratio": round(c.debt_ratio * 100, 1) if c.debt_ratio else None,
                    "revenue_growth": round(c.revenue_growth * 100, 1) if c.revenue_growth else None,
                    "above_ma20": c.above_ma20,
                    "near_ma250": c.near_ma250,
                }
                for c in result.candidates
            ],
            "duration_seconds": result.duration_seconds,
        }
    except Exception as e:
        logger.exception("价值波段扫描失败")
        return {"ok": False, "error": str(e)}


_RUNNING_SCAN = threading.Lock()


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
    # debt_ratio 如果有
    if candidate.get("debt_ratio") is not None:
        signals.append(f"负债{candidate['debt_ratio']:.0f}%")

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
                    信号 {score}/7
                </div>
            </div>
            <div style="display: flex; gap: 16px; margin-top: 10px; flex-wrap: wrap;">
                <span style="color: #888; font-size: 0.8rem;">PE {candidate['pe_ttm']:.1f}x</span>
                <span style="color: #888; font-size: 0.8rem;">PB {candidate['pb']:.1f}x</span>
                <span style="color: #888; font-size: 0.8rem;">价 {candidate['price']:.2f}</span>
                {''.join(f'<span style="color: #ff8c42; font-size: 0.8rem;">{s}</span>' for s in signals[:4])}
                {f'<span style="color: #4caf50; font-size: 0.8rem;">PEG {candidate["peg"]:.2f}</span>' if candidate.get("peg") else ''}
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_funnel_stats(result: dict):
    """渲染漏斗统计。"""
    cols = st.columns(5)
    metrics = [
        ("全 A 股", result["total_stocks"], ""),
        ("L0 通过", result["l0_passed"], "流动性/非ST"),
        ("L1 通过", result["l1_passed"], "估值/财务"),
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


def render_value_swing_scanner():
    """价值波段扫描主面板。"""
    st.header("📊 价值波段扫描")
    st.caption("三阶漏斗：全市场 → 流动性/估值 → 催化剂评分 → 推荐分级")

    # 当前数据日期
    today = datetime.now().strftime("%Y-%m-%d")

    # 扫描结果存储在 session 中
    scan_result_key = "_value_swing_scan_result"

    # 操作栏
    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        max_candidates = st.number_input(
            "候选上限",
            min_value=5, max_value=50, value=15,
            help="L2 最终输出的候选股票数量上限",
        )
    with col2:
        run_scan = st.button(
            "🚀 开始扫描",
            use_container_width=True,
            type="primary",
            disabled=st.session_state.get("_scan_running", False),
        )
    with col3:
        if st.button("🔄 重置", use_container_width=True):
            st.session_state.pop(scan_result_key, None)
            st.session_state.pop("_scan_running", None)
            st.rerun()

    # 执行扫描（后台线程）
    if run_scan:
        st.session_state["_scan_running"] = True
        with st.spinner("正在全市场扫描，预计 1~3 分钟..."):
            result = _run_scan(max_candidates)
        st.session_state[scan_result_key] = result
        st.session_state["_scan_running"] = False
        st.rerun()

    # 显示上次扫描结果
    result = st.session_state.get(scan_result_key)
    if result is None:
        if not run_scan:
            st.info("尚未运行扫描。设置候选上限后点击「开始扫描」。")
        return

    if not result.get("ok"):
        st.error(f"扫描失败: {result.get('error', '未知错误')}")
        return

    # 漏斗统计
    _render_funnel_stats(result)

    st.divider()

    # 候选列表
    candidates = result.get("candidates", [])
    render_scan_results(candidates)
