"""Idle 主页：默认策略扫描；单票入口仅在侧栏。"""

from __future__ import annotations

from pathlib import Path

from web.home_mode import (
    HOME_MODE_SCAN,
    HOME_MODE_SINGLE,
    normalize_home_mode,
    resolve_idle_panel,
)


def test_normalize_home_mode_defaults_to_scan():
    assert normalize_home_mode(None) == HOME_MODE_SCAN
    assert normalize_home_mode("") == HOME_MODE_SCAN
    assert normalize_home_mode("scan") == HOME_MODE_SCAN
    assert normalize_home_mode("single") == HOME_MODE_SINGLE
    assert normalize_home_mode("wat") == HOME_MODE_SCAN


def test_resolve_idle_panel_defaults_to_scan():
    assert resolve_idle_panel(HOME_MODE_SINGLE) == "welcome"
    assert resolve_idle_panel(HOME_MODE_SCAN) == "scan"
    assert resolve_idle_panel(None) == "scan"
    assert resolve_idle_panel("nope") == "scan"


def test_app_idle_is_scan_only_without_st_tabs():
    """首页固定策略扫描，不得再用 st.tabs / 单票欢迎页切换。"""
    src = Path("web/app.py").read_text(encoding="utf-8")
    idle = src.split("State 0: Idle")[-1]
    assert "st.tabs(" not in idle, (
        "idle home must not use st.tabs; render scan exclusively"
    )
    assert "render_value_swing_scanner" in idle
    assert "单票分析" not in idle
    assert "主页视图" not in idle


def test_sidebar_brand_navigates_home():
    src = Path("web/components/sidebar.py").read_text(encoding="utf-8")
    assert 'key="nav_brand_home"' in src
    assert 'navigate("home")' in src
    assert "set_home_mode" in src and "HOME_MODE_SCAN" in src


def test_sidebar_value_swing_sets_scan_mode():
    """侧栏「价值波段扫描」应切到 scan 并 navigate(home)。"""
    src = Path("web/components/sidebar.py").read_text(encoding="utf-8")
    assert "📊 价值波段扫描" in src
    assert "set_home_mode" in src and "HOME_MODE_SCAN" in src
    assert 'navigate("home")' in src
