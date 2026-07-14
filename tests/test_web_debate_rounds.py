"""Web UI 辩论轮数默认值测试。

CLI 的 Research Depth=Deep 对应 max_*_rounds=5；Web 侧边栏不暴露该选项，
因此 Web _build_config 强制使用 Deep（5 轮），与 CLI Deep 一致。
"""

from pathlib import Path


def test_web_build_config_defaults_to_five_debate_rounds():
    source = Path("web/app.py").read_text(encoding="utf-8")
    assert 'config["max_debate_rounds"] = 5' in source
    assert 'config["max_risk_discuss_rounds"] = 5' in source
