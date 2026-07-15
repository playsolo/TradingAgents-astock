"""加入/开始分析成功后，侧栏股票代码输入应被清空。"""

from __future__ import annotations

from pathlib import Path

from web.components import sidebar


def test_apply_pending_clear_ticker_input_clears_widget_key():
    state = {
        sidebar._PENDING_CLEAR_INPUT_TICKERS: True,
        sidebar._INPUT_TICKERS_KEY: "002891\n600010",
    }
    assert sidebar.apply_pending_clear_ticker_input(state) is True
    assert state[sidebar._INPUT_TICKERS_KEY] == ""
    assert sidebar._PENDING_CLEAR_INPUT_TICKERS not in state


def test_apply_pending_clear_ticker_input_noop_without_flag():
    state = {sidebar._INPUT_TICKERS_KEY: "300750"}
    assert sidebar.apply_pending_clear_ticker_input(state) is False
    assert state[sidebar._INPUT_TICKERS_KEY] == "300750"


def test_request_clear_ticker_input_sets_flag():
    state: dict = {}
    sidebar.request_clear_ticker_input(state)
    assert state[sidebar._PENDING_CLEAR_INPUT_TICKERS] is True


def test_submit_requests_clear_on_success_paths():
    """Busy 入队成功才清空；idle 成功开跑后由 app.py 请求清空。"""
    sidebar_src = Path("web/components/sidebar.py").read_text(encoding="utf-8")
    submit = sidebar_src.split("def _submit_analysis_jobs")[1].split("def _resolve_cn")[0]
    early = submit.split("if not jobs:")[0]
    assert "request_clear_ticker_input(" not in early
    busy = submit.split("if is_busy:")[1].split("head, *rest = jobs")[0]
    assert "if added > 0:" in busy
    assert "request_clear_ticker_input(" in busy
    assert "st.rerun()" in busy
    # Idle path must not arm clear before begin succeeds
    idle = submit.split("head, *rest = jobs")[1]
    assert "request_clear_ticker_input(" not in idle

    app_src = Path("web/app.py").read_text(encoding="utf-8")
    begin_block = app_src.split("_begin_analysis(start_req)")[1].split(
        "# ── Main area state machine"
    )[0]
    assert "request_clear_ticker_input(" in begin_block
    assert "st.rerun()" in begin_block


def test_render_applies_pending_clear_before_text_area():
    src = Path("web/components/sidebar.py").read_text(encoding="utf-8")
    render = src.split("def render_sidebar")[1]
    apply_idx = render.find("apply_pending_clear_ticker_input(")
    text_area_idx = render.find("key=_INPUT_TICKERS_KEY")
    assert apply_idx != -1
    assert text_area_idx != -1
    assert apply_idx < text_area_idx
