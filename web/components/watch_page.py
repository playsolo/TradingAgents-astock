"""Web「观察」页：观察池列表、盘面 briefing 与变更告警。"""

from __future__ import annotations

from datetime import date, datetime

import streamlit as st

from tradingagents.watchlist.calendar import (
    OBSERVE_SLOTS,
    action_validity_expires_on,
    effective_valid_trading_days,
    is_action_validity_expired,
)
from tradingagents.watchlist.models import LEAN_LABELS, WatchItem
from tradingagents.watchlist.service import prior_context_from_baseline
from web.auth_page import current_watch_store
from web.stock_display import format_list_ticker_label

_SCENARIO_UI = (
    ("optimistic", "乐观"),
    ("neutral", "中性"),
    ("pessimistic", "悲观"),
)


def _fmt_observed_at(raw: str | None) -> str:
    """格式化观察时间戳。

    当天的时间显示为「今日 HH:MM:SS」，非当天显示原格式。
    """
    if raw is None:
        return "尚未执行"
    try:
        dt = datetime.fromisoformat(raw)
        today = date.today()
        if dt.date() == today:
            return f"今日 {dt.strftime('%H:%M:%S')}"
        return raw
    except (ValueError, TypeError):
        return raw


def render_watch_page() -> None:
    st.markdown("### 📡 观察池")
    st.caption(
        "仅 A 股 · 交易日 "
        + " / ".join(f"{h:02d}:{m:02d}" for h, m in OBSERVE_SLOTS)
        + " 轻量复核（推荐 launchd 守护 `com.tradingagents.watchlist`：登录自启、崩溃自动重启；睡眠/关机将跳过）。"
        " 跟踪时效对齐报告「操作建议」时限（区间取上限）；过期后自动停跟，"
        "需新完整分析或再次加入观察池以续命。"
        " 告警条件：立场变化、仓位变动>5个百分点、价格偏离基准>5%、跌破止损、新增重大风险。"
        " 时限内每次观察（手动或定时）都会更新盘面小结与乐观/中性/悲观三情景。"
    )
    st.warning("港股 / 美股尚未支持。本功能仅供研究，不构成投资建议。")

    store = current_watch_store()
    items = store.list_items()
    if not items:
        st.info("暂无观察标的。完成分析后在报告页点击「加入观察池」。")
        return

    for item in items:
        b = item.baseline
        expired = is_action_validity_expired(b)
        unread = len(item.alerts)
        extras = [f"基准 {b.stance}"]
        if b.position_pct is not None:
            extras.append(f"仓位 {b.position_pct:g}%")
        if expired:
            extras.append("已过期")
        if unread:
            extras.append(f"{unread} 条告警")
        title = format_list_ticker_label(b.ticker, *extras)

        with st.expander(title, expanded=bool(unread) or bool(item.last_briefing) or expired):
            c1, c2, c3 = st.columns(3)
            c1.metric("基准价", f"{b.baseline_price:g}" if b.baseline_price else "—")
            c2.metric("分析日", b.trade_date)
            c3.metric("上次观察", _fmt_observed_at(item.last_observed_at))

            expires = action_validity_expires_on(b)
            days = effective_valid_trading_days(b.valid_trading_days)
            horizon_label = b.horizon_raw or f"{days}个交易日（默认）"
            st.caption(f"操作时效：{horizon_label} · 有效至 {expires.isoformat()}（含）")

            _render_observation_block(item)

            if b.thesis_summary:
                st.caption(f"基准逻辑：{b.thesis_summary}")

            on = st.toggle("启用自动观察", value=item.enabled, key=f"watch_en_{b.ticker}")
            if on != item.enabled:
                store.set_enabled(b.ticker, on)
                st.rerun()

            if item.alerts:
                st.markdown("**告警**")
                for alert in item.alerts[:20]:
                    st.markdown(
                        f"- `{alert.observed_at}` **{alert.title}**（{alert.kind}）：{alert.detail}"
                    )
            else:
                st.caption("相对基准无实质变化（告警条件未触发）。")

            if expired:
                st.warning(
                    f"操作建议时限已过（基准日 {b.trade_date}，"
                    f"有效 {days} 个交易日至 {expires.isoformat()}）。"
                    "自动观察已停；请完整再分析或从新报告重新加入观察池以续命。"
                )

            b1, b2, b3 = st.columns(3)
            observe_label = "完整再分析" if expired else "立即观察一次"
            if b1.button(observe_label, key=f"watch_now_{b.ticker}", use_container_width=True):
                fresh = store.get(b.ticker)
                if fresh and is_action_validity_expired(fresh.baseline):
                    today = date.today().isoformat()
                    st.session_state["start_analysis"] = {
                        "ticker": fresh.baseline.ticker,
                        "trade_date": today,
                        "fresh": True,
                        "market": "CN",
                        "past_context": prior_context_from_baseline(fresh.baseline),
                        "watchlist_refresh": True,
                    }
                    st.session_state["viewing_watchlist"] = False
                    st.rerun()
                else:
                    with st.spinner(f"正在观察 {b.ticker}…"):
                        from tradingagents.watchlist.observe import observe_item
                        from tradingagents.watchlist.scheduler import build_quick_llm

                        cfg = {
                            "llm_provider": st.session_state.get("llm_provider", "deepseek"),
                            "quick_think_llm": st.session_state.get(
                                "quick_think_llm", "deepseek-chat"
                            ),
                            "backend_url": st.session_state.get("llm_base_url") or None,
                        }
                        if fresh:
                            slot = f"manual-{datetime.now().isoformat(timespec='seconds')}"
                            alerts = observe_item(
                                fresh,
                                store=store,
                                llm=build_quick_llm(cfg),
                                slot_key=slot,
                                force=True,
                            )
                            if alerts:
                                st.success(f"发现 {len(alerts)} 条变化")
                            else:
                                st.info("观察完成：已更新盘面小结与三情景（无告警）")
                    st.rerun()

            if b2.button("查看原报告", key=f"watch_report_{b.ticker}", use_container_width=True):
                if b.log_path:
                    from web.navigation import navigate

                    navigate(
                        "history",
                        ticker=b.ticker,
                        date=b.trade_date,
                        path=b.log_path,
                    )
                else:
                    st.warning("未找到对应分析日志路径")

            if b3.button("移出观察池", key=f"watch_rm_{b.ticker}", use_container_width=True):
                store.remove(b.ticker)
                st.rerun()


def _render_observation_block(item: WatchItem) -> None:
    briefing = item.last_briefing
    if briefing is not None and (
        briefing.market_brief or any(s.view for s in briefing.scenarios.values())
    ):
        st.markdown("**本次观察**")
        if briefing.market_brief:
            st.markdown(f"**盘面小结**\n\n{briefing.market_brief}")
        lean_label = LEAN_LABELS.get(briefing.lean, "中性")
        lean_line = f"**今日倾向：{lean_label}**"
        if briefing.lean_reason:
            lean_line += f" — {briefing.lean_reason}"
        st.markdown(lean_line)
        cols = st.columns(3)
        for col, (key, title) in zip(cols, _SCENARIO_UI):
            sc = briefing.scenarios.get(key)
            with col:
                st.markdown(f"**{title}**")
                if sc and sc.view:
                    st.markdown(sc.view)
                if sc and sc.reason:
                    st.caption(sc.reason)
        if item.last_summary:
            with st.expander("完整摘要（文本）", expanded=False):
                st.markdown(item.last_summary)
        return

    if item.last_summary:
        st.markdown(f"**最近摘要**\n\n{item.last_summary}")
