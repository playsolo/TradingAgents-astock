"""Web「最新」页：站内事件中心（分析完成 / 失败 / 警告 / 观察告警）。"""

from __future__ import annotations

import time

import streamlit as st

from tradingagents import inbox
from web.navigation import navigate

_SEVERITY_ICON = {
    "info": "🟢",
    "warning": "🟡",
    "error": "🔴",
}

_MINUTE = 60
_HOUR = 3600
_DAY = 86400


def severity_icon(severity: str) -> str:
    """事件严重度对应的状态圆点图标。"""
    return _SEVERITY_ICON.get(severity, "⚪")


def relative_time(created_at: float | None, *, now: float | None = None) -> str:
    """把时间戳转成「刚刚 / N 分钟前 / N 小时前 / N 天前」。"""
    if created_at is None:
        return ""
    try:
        delta = (now if now is not None else time.time()) - float(created_at)
    except (TypeError, ValueError):
        return ""
    if delta < _MINUTE:
        return "刚刚"
    if delta < _HOUR:
        return f"{int(delta // _MINUTE)} 分钟前"
    if delta < _DAY:
        return f"{int(delta // _HOUR)} 小时前"
    return f"{int(delta // _DAY)} 天前"


def _open_event(event: dict) -> None:
    """标记已读并跳转到事件关联视图。"""
    inbox.mark_read(event.get("id", ""))
    link_view = event.get("link_view") or "home"
    if link_view == "history" and event.get("ticker") and event.get("trade_date"):
        navigate("history", ticker=event["ticker"], date=event["trade_date"])
    elif link_view == "watch":
        navigate("watch")
    else:
        navigate("home")


def render_inbox_page() -> None:
    st.markdown("### 🔔 最新")
    st.caption(
        "分析完成 / 失败、报告数据缺失、观察池告警都会汇总到这里。"
        "点击条目可标记已读并跳转到对应页面。"
    )

    events = inbox.list_events(limit=inbox.MAX_EVENTS)
    unread = sum(1 for e in events if not e.get("read"))

    top = st.columns([3, 1])
    top[0].markdown(f"共 {len(events)} 条 · 未读 {unread}")
    if top[1].button(
        "全部已读",
        use_container_width=True,
        disabled=unread == 0,
        key="inbox_mark_all_read",
    ):
        inbox.mark_all_read()
        st.rerun()

    if not events:
        st.info("暂无消息。完成分析或观察池复核后会在这里通知你。")
        return

    st.markdown("---")
    for event in events:
        icon = severity_icon(event.get("severity", "info"))
        title = event.get("title") or event.get("kind", "")
        unread_dot = "🔵 " if not event.get("read") else ""
        cols = st.columns([5, 1])
        with cols[0]:
            st.markdown(f"{unread_dot}{icon} **{title}**")
            detail = event.get("detail")
            if detail:
                st.caption(detail)
            st.caption(relative_time(event.get("created_at")))
        with cols[1]:
            st.button(
                "查看",
                key=f"inbox_open_{event.get('id')}",
                use_container_width=True,
                on_click=_open_event,
                args=(event,),
            )
        st.markdown("---")
