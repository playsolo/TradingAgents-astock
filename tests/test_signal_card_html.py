"""Regression: signal-card provenance must render as HTML, not a code block."""

from __future__ import annotations

from web.components.report_viewer import signal_card_html


def test_provenance_present_without_elapsed():
    """Bug: empty elapsed left a blank indented line before provenance,
    so Streamlit markdown rendered the model line as a raw HTML code block.
    """
    html = signal_card_html(
        "Hold",
        "ISRG 直觉外科公司",
        "2026-07-18",
        elapsed=None,
        final_state={
            "llm_provider": "minimax",
            "deep_think_llm": "MiniMax-M3",
            "quick_think_llm": "MiniMax-M3",
            "llm_fallback_chain": [
                {"provider": "deepseek", "model": "deepseek-v4-flash"},
            ],
        },
    )
    assert "TRADING SIGNAL" in html
    assert "HOLD" in html
    assert "ISRG 直觉外科公司" in html
    assert "模型 MiniMax-M3" in html
    assert "兜底 deepseek/deepseek-v4-flash" in html
    assert "🤖 minimax" in html
    # Continuous HTML — no blank line that would start a markdown code block.
    assert "\n\n" not in html
    assert html.lstrip() == html
    assert html.startswith("<div")
    # Provenance is a real child div, not escaped source text.
    assert 'color:#9aa' in html
    assert "&lt;div" not in html


def test_elapsed_and_provenance_both_render():
    html = signal_card_html(
        "Buy",
        "600519",
        "2026-07-01",
        elapsed=125.0,
        final_state={"llm_provider": "deepseek", "quick_think_llm": "deepseek-chat"},
    )
    assert "耗时 2:05" in html
    assert "快速 deepseek-chat" in html
    assert "🤖 deepseek" in html


def test_provenance_escapes_untrusted_model_names():
    html = signal_card_html(
        "Hold",
        "TEST",
        "2026-01-01",
        final_state={
            "llm_provider": '<script>alert(1)</script>',
            "deep_think_llm": "safe",
            "quick_think_llm": "safe",
        },
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
