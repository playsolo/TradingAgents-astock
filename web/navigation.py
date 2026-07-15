"""Streamlit 侧栏视图 ↔ URL query 持久化。

支持：
  /?view=home
  /?view=watch
  /?view=inbox
  /?view=accuracy
  /?view=history&ticker=002648&date=2026-07-14
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote

import streamlit as st


def _results_dir() -> Path:
    return Path.home() / ".tradingagents" / "logs"


def history_path_for(ticker: str, date: str) -> str | None:
    path = (
        _results_dir()
        / str(ticker).upper()
        / "TradingAgentsStrategy_logs"
        / f"full_states_log_{date}.json"
    )
    return str(path) if path.exists() else None


def view_query(
    view: str,
    *,
    ticker: str | None = None,
    date: str | None = None,
) -> dict[str, str]:
    params: dict[str, str] = {"view": view}
    if ticker:
        params["ticker"] = str(ticker).upper()
    if date:
        params["date"] = date
    return params


def parse_view_params(params: dict[str, Any] | Any) -> dict[str, str | None]:
    def _get(key: str) -> str | None:
        try:
            val = params.get(key)  # type: ignore[attr-defined]
        except Exception:
            return None
        if val is None:
            return None
        if isinstance(val, (list, tuple)):
            val = val[0] if val else None
        s = str(val).strip()
        return s or None

    view = (_get("view") or "home").lower()
    if view not in {"home", "watch", "history", "inbox", "accuracy"}:
        view = "home"
    return {
        "view": view,
        "ticker": _get("ticker"),
        "date": _get("date"),
    }


def apply_query_to_session() -> None:
    """URL → session：刷新/分享链接时恢复侧栏视图。"""
    parsed = parse_view_params(st.query_params)
    view = parsed["view"]

    if view == "inbox":
        st.session_state["viewing_inbox"] = True
        st.session_state["viewing_watchlist"] = False
        st.session_state["viewing_accuracy"] = False
        st.session_state["viewing_history"] = None
        return

    if view == "watch":
        st.session_state["viewing_inbox"] = False
        st.session_state["viewing_watchlist"] = True
        st.session_state["viewing_accuracy"] = False
        st.session_state["viewing_history"] = None
        return

    if view == "accuracy":
        st.session_state["viewing_inbox"] = False
        st.session_state["viewing_watchlist"] = False
        st.session_state["viewing_accuracy"] = True
        st.session_state["viewing_history"] = None
        return

    if view == "history" and parsed["ticker"] and parsed["date"]:
        path = history_path_for(str(parsed["ticker"]), str(parsed["date"]))
        if path:
            st.session_state["viewing_inbox"] = False
            st.session_state["viewing_history"] = path
            st.session_state["viewing_watchlist"] = False
            st.session_state["viewing_accuracy"] = False
            return

    # home（或缺省）：离开观察/历史列表视图；进行中的分析靠 tracker 显示
    st.session_state["viewing_inbox"] = False
    st.session_state["viewing_watchlist"] = False
    st.session_state["viewing_accuracy"] = False
    # 若 URL 无 history，清掉历史视图（避免刷新后粘住）
    if view == "home":
        st.session_state["viewing_history"] = None


def navigate(
    view: str,
    *,
    ticker: str | None = None,
    date: str | None = None,
    path: str | None = None,
) -> None:
    """更新 session + URL 并 rerun。"""
    if view == "inbox":
        st.session_state["viewing_inbox"] = True
        st.session_state["viewing_watchlist"] = False
        st.session_state["viewing_accuracy"] = False
        st.session_state["viewing_history"] = None
        st.session_state["start_analysis"] = None
        params = view_query("inbox")
    elif view == "watch":
        st.session_state["viewing_inbox"] = False
        st.session_state["viewing_watchlist"] = True
        st.session_state["viewing_accuracy"] = False
        st.session_state["viewing_history"] = None
        st.session_state["start_analysis"] = None
        params = view_query("watch")
    elif view == "accuracy":
        st.session_state["viewing_inbox"] = False
        st.session_state["viewing_watchlist"] = False
        st.session_state["viewing_accuracy"] = True
        st.session_state["viewing_history"] = None
        st.session_state["start_analysis"] = None
        params = view_query("accuracy")
    elif view == "history":
        st.session_state["viewing_inbox"] = False
        st.session_state["viewing_watchlist"] = False
        st.session_state["viewing_accuracy"] = False
        st.session_state["start_analysis"] = None
        resolved = path or (
            history_path_for(ticker, date) if ticker and date else None
        )
        st.session_state["viewing_history"] = resolved
        # 尽量带上 ticker/date；若只有 path 则解析
        if (not ticker or not date) and resolved:
            p = Path(resolved)
            ticker = p.parent.parent.name
            date = p.stem.replace("full_states_log_", "")
        params = view_query("history", ticker=ticker, date=date)
    else:
        st.session_state["viewing_inbox"] = False
        st.session_state["viewing_watchlist"] = False
        st.session_state["viewing_accuracy"] = False
        st.session_state["viewing_history"] = None
        params = view_query("home")

    st.query_params.clear()
    for k, v in params.items():
        if v is not None:
            st.query_params[k] = v
    st.rerun()


def shareable_path(view: str, **kwargs: str) -> str:
    q = view_query(view, **kwargs)
    return "/?" + "&".join(f"{k}={quote(str(v))}" for k, v in q.items())
