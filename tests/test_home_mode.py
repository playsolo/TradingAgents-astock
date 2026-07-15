"""Idle 主页：单票 / 策略扫描应互斥渲染，避免 st.tabs 叠层。"""

from __future__ import annotations

import ast
from pathlib import Path

from web.home_mode import (
    HOME_MODE_KEY,
    HOME_MODE_SCAN,
    HOME_MODE_SINGLE,
    normalize_home_mode,
    resolve_idle_panel,
)


def test_normalize_home_mode_defaults_and_rejects_unknown():
    assert normalize_home_mode(None) == HOME_MODE_SINGLE
    assert normalize_home_mode("") == HOME_MODE_SINGLE
    assert normalize_home_mode("scan") == HOME_MODE_SCAN
    assert normalize_home_mode("single") == HOME_MODE_SINGLE
    assert normalize_home_mode("wat") == HOME_MODE_SINGLE


def test_resolve_idle_panel_is_exclusive():
    assert resolve_idle_panel(HOME_MODE_SINGLE) == "welcome"
    assert resolve_idle_panel(HOME_MODE_SCAN) == "scan"
    assert resolve_idle_panel("nope") == "welcome"


def test_app_idle_does_not_use_st_tabs_for_home_switch():
    """主页单票/扫描切换不得再用 st.tabs（1.59 会叠层显示全部 Tab 内容）。"""
    src = Path("web/app.py").read_text(encoding="utf-8")
    # Idle 区块应通过 home_mode 互斥分支，而不是同时挂两个 tab 容器内容
    assert "st.tabs(" not in src.split("State 0: Idle")[-1], (
        "idle home must not use st.tabs; use exclusive home_mode rendering"
    )
    assert "resolve_idle_panel" in src or HOME_MODE_KEY in src
    assert "render_value_swing_scanner" in src


def test_sidebar_value_swing_sets_scan_mode():
    """侧栏「价值波段扫描」应切到 scan，而不是仅 navigate(home) 停在欢迎页。"""
    src = Path("web/components/sidebar.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    found_button = False
    sets_scan = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == "📊 价值波段扫描":
            found_button = True
        if isinstance(node, ast.Constant) and node.value == HOME_MODE_SCAN:
            sets_scan = True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"set_home_mode", "set_home_mode_scan"}
        ):
            sets_scan = True

    assert found_button, "value swing sidebar button missing"
    assert sets_scan or (
        f'["{HOME_MODE_KEY}"]' in src and f'"{HOME_MODE_SCAN}"' in src
    ), "sidebar must set home_mode to scan when opening value swing"
