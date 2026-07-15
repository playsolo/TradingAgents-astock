"""Render the completed analysis report with expandable sections and PDF download."""

from __future__ import annotations

import html
import re
from typing import Any, Callable

import streamlit as st

from web.pdf_export import generate_markdown, generate_pdf
from web.report_repair import (
    has_hard_missing_data,
    list_repairable_missing_sections,
    probe_section_data,
    regenerate_section,
    repair_all_missing_sections,
    section_supports_repair,
)
from web.stock_display import normalize_stock_mentions, stock_display_label

_SECTION_TITLES = {
    "market_report": "技术分析",
    "sentiment_report": "市场情绪",
    "news_report": "新闻舆情",
    "fundamentals_report": "基本面",
    "policy_report": "政策分析",
    "hot_money_report": "游资追踪",
    "lockup_report": "解禁/减持",
}


def _strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL).strip()


def _signal_style(signal: str) -> tuple[str, str]:
    s = signal.upper()
    if "BUY" in s:
        return "#22c55e", "买入"
    if "SELL" in s:
        return "#ef4444", "卖出"
    return "#fbbf24", "持有"


_ANALYST_SECTIONS = [
    ("market_report", "📊 技术分析"),
    ("sentiment_report", "💬 市场情绪"),
    ("news_report", "📰 新闻舆情"),
    ("fundamentals_report", "📋 基本面"),
    ("policy_report", "🏛️ 政策分析"),
    ("hot_money_report", "🔥 游资追踪"),
    ("lockup_report", "🔒 解禁/减持"),
]


def _safe_filename_label(label: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "_", label).strip("_")
    return cleaned or "report"


_RATING_STYLE: dict[str, tuple[str, str]] = {
    "buy": ("#22c55e", "买入"),
    "overweight": ("#22c55e", "增持"),
    "hold": ("#fbbf24", "持有"),
    "underweight": ("#ef4444", "减持"),
    "sell": ("#ef4444", "卖出"),
}


def action_plan_rating_style(rating: str) -> tuple[str, str]:
    """Map a 5-tier rating to (hex color, Chinese label).

    Unknown values fall back to the neutral colour with the raw text so the
    card still renders something sensible.
    """
    return _RATING_STYLE.get(str(rating or "").strip().lower(), ("#fbbf24", str(rating or "")))


def _fmt_price(value: Any) -> str:
    """Render a price with up to 2 decimals, trimming trailing zeros."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    text = f"{num:.2f}".rstrip("0").rstrip(".")
    return text or "0"


def _zone(low: Any, high: Any) -> str | None:
    """Format a price zone; tolerate a single stated bound."""
    parts = [_fmt_price(v) for v in (low, high) if v is not None]
    if not parts:
        return None
    return " – ".join(parts)


def format_action_plan_levels(levels: dict[str, Any]) -> list[tuple[str, str]]:
    """Turn the ActionPlanLevels dict into labeled, display-ready rows.

    Only levels explicitly present (non-null) are returned, in a fixed order:
    减持区间 → 止损位 → 关注支撑 → 回补区间.
    """
    levels = levels or {}
    rows: list[tuple[str, str]] = []

    reduce_zone = _zone(levels.get("reduce_low"), levels.get("reduce_high"))
    if reduce_zone:
        rows.append(("减持区间", reduce_zone))

    if levels.get("stop_loss") is not None:
        rows.append(("止损位", _fmt_price(levels["stop_loss"])))

    if levels.get("watch_support") is not None:
        rows.append(("关注支撑", _fmt_price(levels["watch_support"])))

    reentry_zone = _zone(levels.get("reentry_low"), levels.get("reentry_high"))
    if reentry_zone:
        rows.append(("回补区间", reentry_zone))

    return rows


def action_plan_card_html(plan: dict[str, Any]) -> str:
    """Build the action-plan card HTML.

    All model-generated text (summary / horizon / rating label) is
    HTML-escaped before interpolation because the card is rendered with
    ``unsafe_allow_html=True``.
    """
    color, rating_cn = action_plan_rating_style(plan.get("rating", ""))
    summary = _strip_think(str(plan.get("summary") or ""))
    horizon = str(plan.get("horizon") or "").strip()

    horizon_html = (
        f'<span style="margin-left:auto; font-size:0.85rem; color:#9aa4b2;">时限 {html.escape(horizon)}</span>'
        if horizon
        else ""
    )
    header = (
        '<div style="display:flex; align-items:center; gap:0.6rem; margin-bottom:0.6rem;">'
        '<span style="font-size:0.8rem; letter-spacing:2px; color:#9aa4b2;">操作建议</span>'
        f'<span style="background:{color}22; color:{color}; font-weight:800;'
        ' padding:2px 12px; border-radius:12px; border:1px solid '
        f'{color}55;">{html.escape(rating_cn)}</span>'
        f"{horizon_html}"
        "</div>"
    )
    summary_html = (
        f'<div style="color:#e8e3da; font-size:0.98rem; line-height:1.5;">{html.escape(summary)}</div>'
        if summary
        else ""
    )
    return (
        f'<div style="'
        f"background:#12151c;"
        f"border:1px solid #2a2f3a;"
        f"border-left:4px solid {color};"
        f"border-radius:12px;"
        f"padding:1rem 1.2rem;"
        f'margin:-0.5rem 0 1.5rem;">'
        f"{header}{summary_html}"
        f"</div>"
    )


def _render_action_plan_card(final_state: dict[str, Any]) -> None:
    """Render the structured action-plan card below the signal banner.

    Non-fatal: if no ``action_plan`` was extracted (older run or a provider
    without structured output), nothing is shown and the prose report stands.
    """
    plan = final_state.get("action_plan")
    if not isinstance(plan, dict) or not plan.get("rating"):
        return

    holders = _strip_think(str(plan.get("holders_action") or ""))
    non_holders = _strip_think(str(plan.get("non_holders_action") or ""))
    rows = format_action_plan_levels(plan.get("levels") or {})

    st.markdown(action_plan_card_html(plan), unsafe_allow_html=True)

    if holders or non_holders:
        col_h, col_n = st.columns(2)
        with col_h:
            st.caption("已持仓者")
            st.markdown(f"**{holders or '—'}**")
        with col_n:
            st.caption("未持仓者")
            st.markdown(f"**{non_holders or '—'}**")

    if rows:
        cols = st.columns(len(rows))
        for col, (label, value) in zip(cols, rows):
            col.metric(label, value)


def _display_report_text(text: Any, ticker: str, final_state: dict[str, Any]) -> str:
    cleaned = _strip_think(str(text))
    return normalize_stock_mentions(cleaned, ticker, final_state)


def resolve_report_market(
    ticker: str,
    *,
    sidebar_market: str | None = None,
    tracker_market: str | None = None,
) -> str:
    """Decide if a report is A-share or US for UI gating (watchlist etc.).

    Prefer ticker shape, then tracker.market. Sidebar market is ignored because
    the radio can change after a run completes.
    """
    code = (ticker or "").strip().upper()
    if code.isdigit() and len(code) == 6:
        return "CN"
    if tracker_market in {"CN", "US"}:
        return tracker_market
    if any(ch.isalpha() for ch in code):
        return "US"
    if sidebar_market in {"CN", "US"}:
        return sidebar_market
    return "CN"


def _probe_session_key(section_key: str, ticker: str, trade_date: str) -> str:
    return f"probe_ok::{ticker}::{trade_date}::{section_key}"


def _batch_detail_key(ticker: str, trade_date: str) -> str:
    return f"batch_repair_detail::{ticker}::{trade_date}"


def _batch_flash_key(ticker: str, trade_date: str) -> str:
    return f"batch_repair_flash::{ticker}::{trade_date}"


def _render_batch_repair_banner(
    *,
    final_state: dict[str, Any],
    ticker: str,
    trade_date: str,
    log_path: str | None,
    llm_config: dict[str, Any] | None,
    on_state_updated: Callable[[dict[str, Any]], None] | None,
) -> None:
    """Top-level one-click: probe all missing sections, then auto-regenerate."""
    flash = st.session_state.pop(_batch_flash_key(ticker, trade_date), None)
    if isinstance(flash, dict):
        for line in flash.get("success") or []:
            st.success(line)
        for line in flash.get("error") or []:
            st.error(line)
        for line in flash.get("info") or []:
            st.info(line)

    missing = list_repairable_missing_sections(final_state)
    detail = st.session_state.get(_batch_detail_key(ticker, trade_date))
    if detail and (missing or flash):
        with st.expander("一键重试详情", expanded=bool(flash)):
            st.code(detail, language="text")

    if not missing:
        return

    labels = "、".join(_SECTION_TITLES.get(k, k) for k in missing)
    st.warning(f"检测到可修复的数据缺失：{labels}")

    if not llm_config:
        st.caption("请先配置 LLM，再使用一键重试。")
        return

    if st.button(
        "🔁 一键重试并重新生成",
        key=f"batch_repair_{ticker}_{trade_date}",
        type="primary",
        use_container_width=True,
        help="重新拉取缺失章节的数据源；成功后自动重写该节报告并刷新质量门控",
    ):
        with st.spinner("正在重试数据并自动重新生成缺失章节…"):
            try:
                result = repair_all_missing_sections(
                    state=final_state,
                    config=llm_config,
                    log_path=log_path,
                )
            except Exception as exc:  # noqa: BLE001
                st.error(f"一键重试失败：{exc}")
                return

        st.session_state[_batch_detail_key(ticker, trade_date)] = result.detail
        for key in result.regenerated:
            probe_key = _probe_session_key(key, ticker, trade_date)
            st.session_state.pop(probe_key, None)
            st.session_state.pop(f"{probe_key}::detail", None)

        if on_state_updated:
            on_state_updated(result.state)

        flash_msg: dict[str, list[str]] = {"success": [], "error": [], "info": []}
        if result.regenerated:
            done = "、".join(_SECTION_TITLES.get(k, k) for k in result.regenerated)
            flash_msg["success"].append(f"已自动重新生成：{done}")
            if "final_trade_decision" in (result.state or {}) and (
                "downstream" in (result.detail or "")
            ):
                flash_msg["success"].append(
                    "已按修复后的证据重新生成投资计划与最终决策（请下拉查看最新「最终投资建议」）"
                )
        if result.probe_failed:
            failed = "、".join(_SECTION_TITLES.get(k, k) for k in result.probe_failed)
            flash_msg["error"].append(f"数据源仍失败，未重新生成：{failed}")
        if result.errors:
            flash_msg["error"].append(
                "部分章节生成失败：\n" + "\n".join(result.errors)
            )
        if not result.regenerated and not result.probe_failed and not result.errors:
            flash_msg["info"].append("没有需要处理的章节。")
        st.session_state[_batch_flash_key(ticker, trade_date)] = flash_msg
        st.rerun()


def _render_section_repair(
    *,
    section_key: str,
    title: str,
    content: str,
    final_state: dict[str, Any],
    ticker: str,
    trade_date: str,
    log_path: str | None,
    llm_config: dict[str, Any] | None,
    on_state_updated: Callable[[dict[str, Any]], None] | None,
) -> None:
    """Show retry / regenerate controls when a section contains hard [数据缺失]."""
    if not has_hard_missing_data(content):
        return
    if not section_supports_repair(section_key):
        st.info(
            f"{title} 存在数据缺失标记；本节暂不支持单独修复，请在侧栏对同标的重新完整分析。"
        )
        return
    if not llm_config:
        st.caption("检测到数据缺失；请配置 LLM 后使用「重试数据 / 重新生成本节」。")
        return

    st.warning(f"{title} 存在 `[数据缺失]` 标记，可先重试数据源，成功后再重新生成本节。")
    probe_key = _probe_session_key(section_key, ticker, trade_date)
    col_probe, col_regen = st.columns(2)
    with col_probe:
        if st.button(
            "🔄 重试数据",
            key=f"probe_btn_{ticker}_{trade_date}_{section_key}",
            use_container_width=True,
        ):
            with st.spinner("正在重新拉取本节关键数据…"):
                result = probe_section_data(section_key, ticker, trade_date)
            st.session_state[probe_key] = result.ok
            st.session_state[f"{probe_key}::detail"] = result.detail
            if result.ok:
                st.success("数据源已恢复，可重新生成本节。")
            else:
                st.error("数据源仍失败，稍后可再试。")

    with col_regen:
        probe_ok = bool(st.session_state.get(probe_key))
        if st.button(
            "✨ 重新生成本节",
            key=f"regen_btn_{ticker}_{trade_date}_{section_key}",
            use_container_width=True,
            disabled=not probe_ok,
            help="需先「重试数据」成功后才能重新生成",
        ):
            with st.spinner(f"正在重新生成{title}并刷新质量门控…"):
                try:
                    updated = regenerate_section(
                        state=final_state,
                        section_key=section_key,
                        config=llm_config,
                        log_path=log_path,
                    )
                except Exception as exc:  # noqa: BLE001
                    st.error(f"重新生成失败：{exc}")
                    return
            if on_state_updated:
                on_state_updated(updated)
            st.session_state.pop(probe_key, None)
            st.session_state.pop(f"{probe_key}::detail", None)
            if log_path:
                st.success(f"{title} 已重新生成，并写入报告文件。")
            else:
                st.success(f"{title} 已重新生成（仅当前会话；未找到可写报告文件）。")
            st.rerun()

    detail = st.session_state.get(f"{probe_key}::detail")
    if detail:
        with st.expander("最近一次数据重试结果", expanded=False):
            st.code(detail, language="text")


def render_report(
    final_state: dict[str, Any],
    ticker: str,
    trade_date: str,
    signal: str,
    elapsed: float | None = None,
    log_path: str | None = None,
    llm_config: dict[str, Any] | None = None,
    on_state_updated: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    """Render the full analysis report."""

    color, cn_signal = _signal_style(signal)
    ticker_label = stock_display_label(ticker, final_state)

    stats_html = ""
    if elapsed is not None:
        m, s = divmod(int(elapsed), 60)
        stats_html = f'<div style="font-size:0.9rem; color:#888; margin-top:0.3rem;">耗时 {m}:{s:02d}</div>'

    st.markdown(
        f"""
        <div style="
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            border: 1px solid #333;
            border-radius: 16px;
            padding: 2rem;
            text-align: center;
            margin: 1rem 0 2rem;
        ">
            <div style="font-size:0.9rem; color:#888; letter-spacing:2px;">TRADING SIGNAL</div>
            <div style="font-size:3.5rem; font-weight:900; color:{color}; margin:0.3rem 0;">
                {signal.upper()}
            </div>
            <div style="font-size:1.2rem; color:#f5f1eb;">
                {ticker_label} · {trade_date}
            </div>
            {stats_html}
        </div>
        """,
        unsafe_allow_html=True,
    )

    _render_action_plan_card(final_state)

    st.caption("⚠️ 本报告由 AI 自动生成，仅供学习研究，不构成投资建议。")

    _render_batch_repair_banner(
        final_state=final_state,
        ticker=ticker,
        trade_date=trade_date,
        log_path=log_path,
        llm_config=llm_config,
        on_state_updated=on_state_updated,
    )

    # Markdown export always works (no font dependency); PDF is generated
    # lazily and guarded so a PDF/font failure never crashes the results page.
    col_md, col_pdf, col_watch = st.columns([1, 1, 1])
    with col_md:
        md_text = generate_markdown(final_state, ticker, trade_date, signal)
        st.download_button(
            "📥 下载 Markdown",
            data=md_text.encode("utf-8"),
            file_name=f"TradingAgents-Astock_{_safe_filename_label(ticker_label)}_{trade_date}.md",
            mime="text/markdown",
            use_container_width=True,
        )
    with col_pdf:
        try:
            pdf_bytes = generate_pdf(final_state, ticker, trade_date, signal)
            st.download_button(
                "📄 下载 PDF",
                data=pdf_bytes,
                file_name=f"TradingAgents-Astock_{_safe_filename_label(ticker_label)}_{trade_date}.pdf",
                mime="application/pdf",
                use_container_width=True,
            )
        except Exception as exc:  # noqa: BLE001 — never let PDF crash the page
            st.button(
                "📄 PDF 不可用",
                disabled=True,
                use_container_width=True,
                help=f"PDF 生成失败，请改用 Markdown 导出。原因：{exc}",
            )
    with col_watch:
        tracker = st.session_state.get("tracker")
        market = resolve_report_market(
            ticker,
            tracker_market=getattr(tracker, "market", None),
        )
        if market == "US":
            st.button(
                "📡 观察池（仅A股）",
                use_container_width=True,
                disabled=True,
                key=f"add_watch_{ticker}_{trade_date}",
                help="美股分析结果暂不支持加入观察池",
            )
        elif st.button("📡 加入观察池", use_container_width=True, key=f"add_watch_{ticker}_{trade_date}"):
            from tradingagents.watchlist.service import add_from_analysis, resolve_log_path
            from web.auth_page import current_watch_store

            try:
                add_from_analysis(
                    final_state,
                    ticker=ticker,
                    trade_date=trade_date,
                    log_path=resolve_log_path(ticker, trade_date),
                    store=current_watch_store(),
                )
                st.success(f"已加入观察池：{ticker}（仅 A 股 · 本机运行时定时复核）")
            except Exception as exc:  # noqa: BLE001
                st.error(f"加入观察池失败：{exc}")

    st.markdown("---")

    inv_plan = final_state.get("investment_plan", "")
    if inv_plan:
        st.markdown("### 👔 最终投资建议")
        st.markdown(_display_report_text(inv_plan, ticker, final_state))
        st.markdown("---")

    st.markdown("### 📊 分析师报告")

    for key, title in _ANALYST_SECTIONS:
        content = final_state.get(key, "")
        if not content:
            continue
        with st.expander(title, expanded=has_hard_missing_data(content)):
            _render_section_repair(
                section_key=key,
                title=title,
                content=str(content),
                final_state=final_state,
                ticker=ticker,
                trade_date=trade_date,
                log_path=log_path,
                llm_config=llm_config,
                on_state_updated=on_state_updated,
            )
            st.markdown(_display_report_text(content, ticker, final_state))

    debate = final_state.get("investment_debate_state")
    if debate and isinstance(debate, dict):
        st.markdown("### ⚔️ 多空辩论")
        tab_bull, tab_bear, tab_judge = st.tabs(["多方", "空方", "研究经理"])
        with tab_bull:
            st.markdown(_display_report_text(debate.get("bull_history", "") or "无数据", ticker, final_state))
        with tab_bear:
            st.markdown(_display_report_text(debate.get("bear_history", "") or "无数据", ticker, final_state))
        with tab_judge:
            st.markdown(_display_report_text(debate.get("judge_decision", "") or "无数据", ticker, final_state))

    trader_decision = final_state.get("trader_investment_decision", "")
    if trader_decision:
        with st.expander("💹 交易员决策", expanded=False):
            st.markdown(_display_report_text(trader_decision, ticker, final_state))

    risk = final_state.get("risk_debate_state")
    if risk and isinstance(risk, dict):
        st.markdown("### 🛡️ 风控评估")
        tab_agg, tab_con, tab_neu, tab_rj = st.tabs(["激进", "保守", "中性", "风控决策"])
        with tab_agg:
            st.markdown(_display_report_text(risk.get("aggressive_history", "") or "无数据", ticker, final_state))
        with tab_con:
            st.markdown(_display_report_text(risk.get("conservative_history", "") or "无数据", ticker, final_state))
        with tab_neu:
            st.markdown(_display_report_text(risk.get("neutral_history", "") or "无数据", ticker, final_state))
        with tab_rj:
            st.markdown(_display_report_text(risk.get("judge_decision", "") or "无数据", ticker, final_state))

    dqs = final_state.get("data_quality_summary", "")
    if dqs:
        with st.expander("✅ 数据质量", expanded=False):
            st.markdown(_display_report_text(dqs, ticker, final_state))
