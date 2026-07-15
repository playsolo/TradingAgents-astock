"""并行进度卡片 HTML：不得因空 signal 产生空行打断 Streamlit markdown HTML。"""

from __future__ import annotations

from web.components.progress_panel import format_run_card_html
from web.parallel_runs import RunSnapshot


def _snap(**overrides) -> RunSnapshot:
    base = dict(
        ticker="002050",
        trade_date="2026-07-15",
        market="CN",
        is_running=True,
        is_complete=False,
        is_paused=False,
        error=None,
        completed_stages=["a"] * 10,
        total_stages=12,
        llm_calls=1,
        tool_calls=0,
        tokens_in=35100,
        tokens_out=1091,
        elapsed=21.0,
        final_signal="",
        current_stage="pm",
        stage_reports={},
    )
    base.update(overrides)
    return RunSnapshot(**base)


def test_run_card_html_has_no_blank_lines_when_signal_empty():
    """Empty final_signal must not insert a blank/whitespace-only line.

    Streamlit markdown ends an HTML block at the first blank line, which then
    leaks the remaining raw tags as plain text (the reported bug).
    """
    html = format_run_card_html(_snap(final_signal=""), focused=True)
    assert "002050" in html
    assert "0:21" in html
    assert "A股" in html
    for i, line in enumerate(html.splitlines(), 1):
        assert line.strip() != "", f"blank line at {i} breaks markdown HTML block"


def test_run_card_html_includes_signal_preview_inline():
    html = format_run_card_html(_snap(final_signal="买入"), focused=False)
    assert "· 买入" in html
    for i, line in enumerate(html.splitlines(), 1):
        assert line.strip() != "", f"blank line at {i}"
