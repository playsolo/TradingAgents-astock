"""策略扫描页：禁止 sleep+rerun 叠层，结果面板不得是 fragment。"""

from __future__ import annotations

import ast
from pathlib import Path

_SCANNER = Path("web/components/value_swing_scanner.py")


def _module_source() -> str:
    return _SCANNER.read_text(encoding="utf-8")


def test_scan_results_is_not_fragment():
    """有旧结果再重扫时，fragment 结果面板会残留并纵向叠两层。"""
    tree = ast.parse(_module_source())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "render_scan_results":
            for dec in node.decorator_list:
                text = ast.unparse(dec)
                assert "fragment" not in text, (
                    "render_scan_results must not be @st.fragment; "
                    f"found decorator {text!r}"
                )
            return
    raise AssertionError("render_scan_results not found")


def test_running_scan_uses_fragment_run_every_not_sleep_rerun():
    """运行中轮询必须用 fragment(run_every)；禁止 time.sleep + st.rerun。"""
    src = _module_source()
    assert "time.sleep(_POLL_INTERVAL_S)" not in src, (
        "scanner must not poll with time.sleep + st.rerun (causes UI stack)"
    )
    assert "@st.fragment(run_every=_POLL_INTERVAL_S)" in src
    assert "_render_running_scan_poll" in src
    assert "_render_dual_pool_running_poll" in src


def test_dual_pool_busy_hides_old_candidates_path():
    """双池扫描进行中应走进度 poll，而不是一边画旧结果一边 sleep。"""
    src = _module_source()
    # render_value_swing_scanner 在 both_busy 时调用进度 fragment
    idle = src.split("def render_value_swing_scanner")[-1]
    assert "_render_dual_pool_running_poll()" in idle
    # 忙时不应在同一分支先 overview 再 sleep
    assert "time.sleep" not in idle
