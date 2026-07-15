"""历史记录点击不应因 navigate 被局部 import 遮蔽而 UnboundLocalError。"""

from __future__ import annotations

import ast
from pathlib import Path


def test_render_sidebar_does_not_locally_import_navigate():
    """render_sidebar 内局部 import navigate 会遮蔽模块级导入。

    Python 把函数内任意处的 import 都视为局部绑定；点击历史记录时
    策略按钮分支未执行，就会在 navigate("history", ...) 处抛 UnboundLocalError。
    """
    src = Path("web/components/sidebar.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != "render_sidebar":
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.ImportFrom):
                continue
            if child.module != "web.navigation":
                continue
            names = [alias.name for alias in child.names]
            assert "navigate" not in names, (
                "render_sidebar must not locally import navigate; "
                "use the module-level import to avoid UnboundLocalError "
                "when clicking history / watchlist"
            )
        break
    else:
        raise AssertionError("render_sidebar not found")
