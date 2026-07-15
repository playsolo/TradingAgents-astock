"""Sidebar: stock input, LLM config, and history list."""

from __future__ import annotations

import os

import streamlit as st

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.checkpointer import clear_checkpoint
from tradingagents.llm_clients.model_catalog import MODEL_OPTIONS
from tradingagents.watchlist.calendar import cn_today
from web.analysis_queue import (
    advance_queue,
    append_jobs,
    clear_queue,
    format_queue_job_caption,
    mark_serial_queue_session,
    parse_ticker_inputs,
    queue_snapshot,
    resolve_ticker_batch,
)
from web.history import (
    clear_incomplete_task,
    get_history,
    get_incomplete_history,
    record_incomplete_task,
)
from web.navigation import navigate
from web.stock_display import format_list_ticker_label

from web.auth_page import render_logout_button, render_admin_panel

# Provider display names in recommended order
_PROVIDERS: list[tuple[str, str]] = [
    ("MiniMax（推荐·国内直连）", "minimax"),
    ("DeepSeek", "deepseek"),
    ("通义千问 Qwen", "qwen"),
    ("智谱 GLM", "glm"),
    ("OpenAI", "openai"),
    ("Anthropic", "anthropic"),
    ("Google Gemini", "google"),
    ("xAI Grok", "xai"),
    ("OpenRouter（聚合·填 vendor/model 形式 ID）", "openrouter"),
    ("Ollama（本地）", "ollama"),
]

_PROVIDER_DISPLAY = [name for name, _ in _PROVIDERS]
_PROVIDER_KEYS = [key for _, key in _PROVIDERS]


def _default_provider_index() -> int:
    """默认选中的供应商下标。

    刷新页面（新会话）后 session_state 会清空，下拉框退回第一项。设置环境变量
    DEFAULT_LLM_PROVIDER（如 deepseek）即可把默认项固定，免去每次手动重选。
    非法/未设置时回退到列表第一项（保持上游默认 MiniMax）。
    """
    pref = os.getenv("DEFAULT_LLM_PROVIDER", "").strip().lower()
    if pref in _PROVIDER_KEYS:
        return _PROVIDER_KEYS.index(pref)
    return 0


def _normalize_us_ticker(raw: str) -> str:
    return (raw or "").strip().upper()


def _resolve_user_input(raw: str) -> tuple[str, str | None]:
    """Resolve raw user input to (ticker_code, error_msg).

    Accepts 6-digit codes or Chinese stock names (e.g. '宝光股份').
    Returns (code, None) on success or ("", error_msg) on failure.
    Prefers local stock_names reverse lookup so Streamlit click handlers
    do not block on mootdx full-market map builds.
    """
    from web.stock_display import lookup_code_by_cached_name, remember_resolved_name

    cached = lookup_code_by_cached_name(raw)
    if cached:
        return cached, None

    from tradingagents.dataflows.a_stock import resolve_ticker

    try:
        code = resolve_ticker(raw)
        remember_resolved_name(code, raw)
        return code, None
    except ValueError as e:
        return "", str(e)


def _resolve_user_input_for_market(raw: str, market: str) -> tuple[str, str | None]:
    """Resolve ticker for CN (A-share) or US (Yahoo-style symbol) markets."""
    if market == "US":
        code = _normalize_us_ticker(raw)
        if not code:
            return "", "请输入美股代码，例如 AAPL / NVDA / BRK.B"
        if any(ch.isspace() for ch in code):
            return "", "美股代码不能包含空格"
        return code, None
    return _resolve_user_input(raw)


def _clear_analysis_artifacts(ticker: str, trade_date: str) -> None:
    clear_incomplete_task(ticker, trade_date)
    clear_checkpoint(DEFAULT_CONFIG["data_cache_dir"], ticker, trade_date)


def _first_raw_ticker(raw_tickers: str) -> str:
    tokens = parse_ticker_inputs(raw_tickers)
    return tokens[0] if tokens else ""


# Streamlit forbids mutating a widget key after the widget is instantiated in
# the same run. Defer clear to the next run (before text_area is created).
_PENDING_CLEAR_INPUT_TICKERS = "_pending_clear_input_tickers"
_INPUT_TICKERS_KEY = "input_tickers"


def request_clear_ticker_input(session_state) -> None:
    session_state[_PENDING_CLEAR_INPUT_TICKERS] = True


def apply_pending_clear_ticker_input(session_state) -> bool:
    """Apply a deferred clear of the ticker text_area. Call before the widget."""
    if not session_state.pop(_PENDING_CLEAR_INPUT_TICKERS, False):
        return False
    session_state[_INPUT_TICKERS_KEY] = ""
    return True


def _submit_analysis_jobs(raw_tickers: str, market: str, trade_date: str) -> None:
    """Start the first job immediately when idle; otherwise enqueue the whole batch."""
    tokens = parse_ticker_inputs(raw_tickers)
    if not tokens:
        st.error("❌ 请输入至少一个股票代码")
        return

    jobs, errors = resolve_ticker_batch(
        tokens,
        market=market,
        trade_date=trade_date,
        resolve_cn=_resolve_cn_or_raise,
    )
    for msg in errors:
        st.warning(f"⚠️ 已跳过 {msg}")
    if not jobs:
        st.error("❌ 没有可分析的有效代码")
        return

    tracker = st.session_state.get("tracker")
    is_busy = tracker is not None and tracker.is_running
    if is_busy:
        exclude = None
        if tracker.ticker and tracker.trade_date:
            market_now = getattr(tracker, "market", None) or market
            exclude = {(market_now, tracker.ticker, tracker.trade_date)}
        added = append_jobs(st.session_state, jobs, exclude=exclude)
        st.session_state["queue_advance_notice"] = (
            f"✅ 已加入分析队列 {added} 只（当前队列 {len(queue_snapshot(st.session_state))}）"
        )
        if added > 0:
            request_clear_ticker_input(st.session_state)
            # Rerun so apply_pending_clear_ticker_input runs before text_area.
            st.rerun()
        return

    head, *rest = jobs
    if market == "CN" and tokens:
        # jobs 已解析过；不要再次 resolve_ticker（中文名会卡 mootdx 全表）。
        raw0 = tokens[0].strip()
        if raw0 and raw0 != head.ticker and not (
            raw0.isdigit() and len(raw0) == 6
        ):
            st.success(f"✅ {raw0} → {head.ticker}")
    # Idle “开始分析” starts a new batch; drop any leftover queued jobs.
    clear_queue(st.session_state)
    append_jobs(st.session_state, rest)
    if rest:
        mark_serial_queue_session(st.session_state, True)
    # Record before sidebar继续往下画「未完成任务」，同一次脚本即可看到进行中。
    record_incomplete_task(
        head.ticker,
        head.trade_date,
        status="running",
        completed_stages=[],
    )
    st.session_state["start_analysis"] = head.to_start_request()
    st.session_state["viewing_history"] = None
    st.session_state["viewing_watchlist"] = False
    st.query_params.clear()
    st.query_params["view"] = "home"
    # Clear ticker input only after app.py successfully begins analysis.


def _resolve_cn_or_raise(raw: str) -> str:
    code, err = _resolve_user_input(raw)
    if err:
        raise ValueError(err)
    return code


def _render_analysis_queue() -> None:
    jobs = queue_snapshot(st.session_state)
    if not jobs:
        return
    st.markdown("#### 分析队列")
    st.caption(
        "串行执行，与观察池无关；完成后自动开始下一只。"
        "刷新后会从本地恢复，空闲时自动开跑。"
    )
    for idx, job in enumerate(jobs, start=1):
        st.caption(format_queue_job_caption(job, idx))

    tracker = st.session_state.get("tracker")
    is_busy = tracker is not None and tracker.is_running
    cont_col, clear_col = st.columns(2)
    if cont_col.button(
        "继续队列",
        key="resume_analysis_queue",
        use_container_width=True,
        disabled=is_busy,
        type="primary",
    ):
        head = advance_queue(st.session_state)
        if head is not None:
            mark_serial_queue_session(st.session_state, True)
            st.session_state["start_analysis"] = head.to_start_request()
            st.session_state["viewing_history"] = None
            st.session_state["viewing_watchlist"] = False
            st.query_params.clear()
            st.query_params["view"] = "home"
            st.rerun()
    if clear_col.button("清空队列", key="clear_analysis_queue", use_container_width=True):
        clear_queue(st.session_state)
        st.rerun()


def _render_analysis_controls(raw_tickers: str, trade_date_value: date) -> None:
    tracker = st.session_state.get("tracker")
    is_running = tracker is not None and tracker.is_running
    trade_date = trade_date_value.strftime("%Y-%m-%d")

    pause_col, resume_col, stop_col = st.columns(3)

    pause_disabled = not is_running or tracker.is_paused or tracker.stop_requested
    if pause_col.button(
        "暂停",
        key="sidebar_pause_analysis",
        use_container_width=True,
        disabled=pause_disabled,
    ):
        if tracker.pause():
            record_incomplete_task(
                tracker.ticker,
                tracker.trade_date,
                status="paused",
                completed_stages=tracker.completed_stages,
            )
        st.rerun()

    resume_disabled = not is_running or not tracker.is_paused or tracker.stop_requested
    if resume_col.button(
        "恢复",
        key="sidebar_resume_analysis",
        use_container_width=True,
        disabled=resume_disabled,
    ):
        if tracker.resume():
            record_incomplete_task(
                tracker.ticker,
                tracker.trade_date,
                status="running",
                completed_stages=tracker.completed_stages,
            )
        st.rerun()

    can_stop = (
        tracker is not None
        or bool(raw_tickers.strip())
        or bool(queue_snapshot(st.session_state))
    )
    if stop_col.button(
        "停止",
        key="sidebar_stop_analysis",
        use_container_width=True,
        disabled=not can_stop,
    ):
        clear_queue(st.session_state)
        target_ticker = tracker.ticker if tracker is not None and tracker.ticker else ""
        target_date = (
            tracker.trade_date
            if tracker is not None and tracker.trade_date
            else trade_date
        )

        if not target_ticker:
            market = st.session_state.get("analysis_market", "CN")
            target_ticker, err = _resolve_user_input_for_market(
                _first_raw_ticker(raw_tickers), market
            )
            if err:
                st.error(f"❌ {err}")
                return

        if tracker is not None and tracker.is_running:
            tracker.request_stop()
            clear_incomplete_task(target_ticker, target_date)
        else:
            if tracker is not None:
                tracker.mark_stopped()
                st.session_state["tracker"] = None
            _clear_analysis_artifacts(target_ticker, target_date)

        st.session_state["viewing_history"] = None
        st.success("已停止当前分析并清除队列；下一次开始会从头生成。")
        st.rerun()

    if tracker is not None and tracker.stop_requested:
        st.caption("正在停止并清空，收尾完成后可重新开始。")


def _render_llm_config() -> None:
    """Render LLM provider and model selection controls."""

    provider_idx = st.selectbox(
        "LLM 供应商",
        range(len(_PROVIDERS)),
        index=_default_provider_index(),
        format_func=lambda i: _PROVIDER_DISPLAY[i],
        key="llm_provider_idx",
        help="选择你配置了 API Key 的供应商（默认项可用环境变量 DEFAULT_LLM_PROVIDER 固定）",
    )
    provider_key = _PROVIDER_KEYS[provider_idx]
    st.session_state["llm_provider"] = provider_key

    if provider_key in MODEL_OPTIONS:
        quick_options = MODEL_OPTIONS[provider_key]["quick"]
        deep_options = MODEL_OPTIONS[provider_key]["deep"]

        quick_labels = [label for label, _ in quick_options]
        quick_values = [value for _, value in quick_options]
        deep_labels = [label for label, _ in deep_options]
        deep_values = [value for _, value in deep_options]

        quick_idx = st.selectbox(
            "快速思考模型",
            range(len(quick_options)),
            format_func=lambda i: quick_labels[i],
            key="quick_model_idx",
            help="用于常规分析任务，速度优先",
        )
        st.session_state["quick_think_llm"] = quick_values[quick_idx]

        deep_idx = st.selectbox(
            "深度思考模型",
            range(len(deep_options)),
            format_func=lambda i: deep_labels[i],
            key="deep_model_idx",
            help="用于辩论/决策等需要深度推理的任务",
        )
        st.session_state["deep_think_llm"] = deep_values[deep_idx]
    else:
        custom_quick = st.text_input("快速思考模型 ID", key="custom_quick_model")
        custom_deep = st.text_input("深度思考模型 ID", key="custom_deep_model")
        st.session_state["quick_think_llm"] = custom_quick
        st.session_state["deep_think_llm"] = custom_deep

    st.text_input(
        "API Base URL（第三方/代理，可选）",
        key="llm_base_url",
        placeholder="例: https://your-proxy.com/v1",
        help=(
            "通过第三方中转/代理访问 Claude、OpenAI 等模型时填写网关地址；"
            "留空则用所选供应商的官方地址。API Key 仍从 .env 读取，"
            "且每个供应商用各自的环境变量——"
            "OpenAI=OPENAI_API_KEY、DeepSeek=DEEPSEEK_API_KEY、"
            "通义=DASHSCOPE_API_KEY、智谱=ZHIPU_API_KEY、MiniMax=MINIMAX_API_KEY、"
            "Claude=ANTHROPIC_API_KEY、OpenRouter=OPENROUTER_API_KEY、xAI=XAI_API_KEY。"
            "也可在 .env 里设 BACKEND_URL 代替此处。"
        ),
    )


def render_sidebar() -> None:
    """Render the sidebar with input controls and history."""

    # ── Auth info & logout ──────────────────────────────────────────
    render_logout_button()
    render_admin_panel()

    st.markdown(
        """
        <div style="text-align:center; margin-bottom:1.5rem;">
            <span style="font-size:2rem; font-weight:800; color:#ff5a1f;">Trading</span><span style="font-size:2rem; font-weight:800; color:#f5f1eb;">Agents</span><span style="font-size:2rem; font-weight:800; color:#f5f1eb;">-</span><span style="font-size:2rem; font-weight:800; color:#ff5a1f;">Astock</span>
            <div style="font-size:0.85rem; color:#888; margin-top:0.2rem;">
                A股多Agent投研系统
            </div>
            <div style="font-size:0.7rem; color:#555; margin-top:0.3rem;">
                by <a href="https://github.com/playsolo" style="color:#ff5a1f; text-decoration:none;">playsolo</a>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown("---")
    st.markdown("#### 新建分析")

    market_label = st.radio(
        "市场",
        options=["A股", "美股"],
        horizontal=True,
        key="input_market_label",
        help="美股将通过本机 TradingAgents 项目子进程分析，进度实时回传",
    )
    market = "US" if market_label == "美股" else "CN"
    st.session_state["analysis_market"] = market

    apply_pending_clear_ticker_input(st.session_state)
    ticker_input = st.text_area(
        "股票代码（可多个）",
        placeholder=(
            "每行一个，或逗号分隔\n例: AAPL, NVDA, BRK.B"
            if market == "US"
            else "每行一个，或逗号分隔\n例: 300750, 600519\n或: 宁德时代"
        ),
        key=_INPUT_TICKERS_KEY,
        height=88,
        help=(
            "支持一次输入多只美股代码（Yahoo 风格）。分析为串行队列，不会加入观察池。"
            if market == "US"
            else "支持一次输入多只 A 股代码或中文全称。分析为串行队列，不会加入观察池。"
        ),
    )

    trade_date = st.date_input(
        "分析日期",
        value=cn_today(),
        key="input_date",
    )

    with st.expander("⚙️ 模型配置", expanded=False):
        _render_llm_config()
        if market == "US":
            st.caption(
                "美股模式会把此处模型配置转发到本机 TradingAgents；"
                "API Key 仍读对应项目的 `.env`。"
                "路径可用环境变量 US_TRADINGAGENTS_ROOT / US_TRADINGAGENTS_PYTHON 覆盖。"
            )

    tracker = st.session_state.get("tracker")
    is_busy = tracker is not None and tracker.is_running
    is_stopping = is_busy and tracker.stop_requested
    has_input = bool(parse_ticker_inputs(ticker_input or ""))
    if is_stopping:
        start_label = "停止中..."
    elif is_busy:
        start_label = "加入分析队列"
    else:
        start_label = "开始分析"

    if st.button(
        start_label,
        use_container_width=True,
        disabled=(is_stopping or not has_input),
        type="primary",
    ):
        _submit_analysis_jobs(
            ticker_input or "",
            market,
            trade_date.strftime("%Y-%m-%d"),
        )

    _render_analysis_controls(ticker_input or "", trade_date)
    _render_analysis_queue()

    if market == "CN":
        st.markdown("---")
        st.markdown("#### 策略")
        if st.button(
            "📊 价值波段扫描",
            key="nav_value_swing",
            use_container_width=True,
            help="全市场 → 流动性/估值筛选 → 催化剂评分 → 推荐分级",
        ):
            from web.navigation import navigate
            navigate("home")  # 回到主页（在扫描 tab 中展示）

        st.markdown("#### 观察")
        from tradingagents.watchlist.store import default_store

        watch_items = default_store().list_items()
        watch_n = len(watch_items)
        alert_n = sum(len(i.alerts) for i in watch_items)
        watch_label = f"📡 观察池（{watch_n}）"
        if alert_n:
            watch_label += f" · {alert_n}告警"
        if st.button(watch_label, key="nav_watchlist", use_container_width=True):
            navigate("watch")

    st.markdown("---")
    st.markdown("#### 未完成任务")

    incomplete = get_incomplete_history()
    if not incomplete:
        st.caption("暂无未完成任务")
    else:
        for entry in incomplete[:10]:
            t, d = entry["ticker"], entry["trade_date"]
            status_label = {
                "error": "出错",
                "paused": "已暂停",
                "running": "进行中",
            }.get(entry.get("status"), "可继续")
            step = entry.get("checkpoint_step")
            step_label = f"step {step}" if step is not None else ""
            label = format_list_ticker_label(t, d, status_label, step_label)
            if st.button(
                label,
                key=f"resume_{t}_{d}",
                use_container_width=True,
                disabled=is_busy,
            ):
                st.session_state["start_analysis"] = {
                    "ticker": t,
                    "trade_date": d,
                    "market": (
                        "CN" if t.isdigit() and len(t) == 6 else "US"
                    ),
                }
                st.session_state["viewing_history"] = None
                st.session_state["viewing_watchlist"] = False
                st.query_params.clear()
                st.query_params["view"] = "home"

    st.markdown("---")
    st.markdown("#### 历史记录")

    history = get_history()
    if not history:
        st.caption("暂无历史记录")
        return

    for entry in history[:20]:
        t, d = entry["ticker"], entry["date"]
        label = format_list_ticker_label(t, d)
        if st.button(label, key=f"hist_{t}_{d}", use_container_width=True):
            navigate("history", ticker=t, date=d, path=entry["path"])

    st.markdown("---")
    st.caption("⚠️ 仅供学习研究，不构成投资建议")
