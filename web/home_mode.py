"""Idle 主页视图模式：单票分析 vs 策略扫描（互斥，不走 st.tabs）。

Streamlit 1.59.x 的 st.tabs 在 Tab 内 widget/切换后会把所有 Tab 内容叠层显示
（见 streamlit#15892）。主页用 session_state 互斥渲染规避该问题，
并让侧栏「价值波段扫描」能直接进入扫描页。
"""

from __future__ import annotations

from typing import Any, Literal

HOME_MODE_KEY = "home_mode"
HOME_MODE_SINGLE = "single"
HOME_MODE_SCAN = "scan"

IdlePanel = Literal["welcome", "scan"]


def normalize_home_mode(value: Any) -> str:
    if value == HOME_MODE_SCAN:
        return HOME_MODE_SCAN
    return HOME_MODE_SINGLE


def get_home_mode(session: Any) -> str:
    return normalize_home_mode(session.get(HOME_MODE_KEY))


def set_home_mode(session: Any, mode: str) -> None:
    session[HOME_MODE_KEY] = normalize_home_mode(mode)


def resolve_idle_panel(mode: Any) -> IdlePanel:
    if normalize_home_mode(mode) == HOME_MODE_SCAN:
        return "scan"
    return "welcome"
