"""Sidebar: stock input, LLM config, and history list."""

from __future__ import annotations

import os

import streamlit as st

from tradingagents.auth.model_config import load_model_config, model_config_exists
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.checkpointer import clear_checkpoint
from tradingagents.llm_clients.model_catalog import MODEL_OPTIONS
from tradingagents.watchlist.calendar import cn_today
from web.analysis_queue import (
    AnalysisJob,
    append_jobs,
    clear_queue,
    default_store,
    format_queue_job_caption,
    mark_serial_queue_session,
    parse_ticker_inputs,
    queue_snapshot,
    remove_job_identity,
    resolve_ticker_batch_mixed,
)
from tradingagents.analyze_worker import is_worker_mode
from web.history import (
    clear_incomplete_task,
    filter_history_by_ticker,
    get_history,
    get_incomplete_history,
    group_history_by_signal,
    record_incomplete_task,
    resolve_history_search_query,
    signal_count_label,
)
from web.home_mode import HOME_MODE_SCAN, set_home_mode
from web.navigation import navigate
from web.parallel_runs import (
    active_runs,
    cn_max_runs,
    has_running,
    request_fill_parallel_slots,
    running_count,
    set_focused_ticker,
    slots_available,
    us_max_runs,
)
from web.stock_display import format_list_ticker_label, signal_text_tag

from web.auth_page import render_logout_button, render_admin_panel, render_model_config_info

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

    优先级：管理员的持久化配置（仅当文件存在时）> 环境变量 DEFAULT_LLM_PROVIDER > 列表第一项。
    """
    if model_config_exists():
        admin_cfg = load_model_config()
        admin_provider = admin_cfg.get("llm_provider", "")
        if admin_provider in _PROVIDER_KEYS:
            return _PROVIDER_KEYS.index(admin_provider)
    pref = os.getenv("DEFAULT_LLM_PROVIDER", "").strip().lower()
    if pref in _PROVIDER_KEYS:
        return _PROVIDER_KEYS.index(pref)
    return 0


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


def _clear_analysis_artifacts(ticker: str, trade_date: str) -> None:
    clear_incomplete_task(ticker, trade_date)
    clear_checkpoint(DEFAULT_CONFIG["data_cache_dir"], ticker, trade_date)


def _infer_market_for_ticker(ticker: str, market: str | None = None) -> str:
    if market in {"CN", "US"}:
        return market
    code = (ticker or "").strip()
    if code.isdigit() and len(code) == 6:
        return "CN"
    return "US"


def _is_live_incomplete_run(session, ticker: str, trade_date: str) -> bool:
    for run in active_runs(session):
        if (
            run.ticker == ticker
            and run.trade_date == trade_date
            and run.is_running
            and not run.is_complete
            and not run.error
        ):
            return True
    return False


def activate_incomplete_task(
    session,
    ticker: str,
    trade_date: str,
    market: str | None = None,
) -> str:
    """Handle a sidebar click on an incomplete task.

    Returns ``focus`` / ``start`` / ``enqueue`` so the UI can mirror single-task
    "click to continue" while still allowing parallel slots.
    """
    ticker = ticker.strip().upper()
    trade_date = trade_date.strip()
    resolved_market = _infer_market_for_ticker(ticker, market)

    session["viewing_history"] = None
    session["viewing_watchlist"] = False
    session["viewing_accuracy"] = False

    if is_worker_mode():
        # Worker mode: never run in-process. Push onto the disk queue.
        default_store().append_atomic(
            [
                AnalysisJob(
                    ticker=ticker,
                    trade_date=trade_date,
                    market=resolved_market,
                    fresh=False,
                )
            ]
        )
        session["queue_advance_notice"] = (
            f"✅ {ticker} 已提交后台分析队列，完成后可在历史查看"
        )
        return "enqueue"

    if _is_live_incomplete_run(session, ticker, trade_date):
        set_focused_ticker(session, ticker)
        return "focus"

    # Drop any queued duplicate before starting, or we risk a second worker
    # when slots free up later (add_tracker only replaces the session handle).
    remove_job_identity(session, (resolved_market, ticker, trade_date))

    if slots_available(session, market=resolved_market) > 0:
        session["start_analysis"] = {
            "ticker": ticker,
            "trade_date": trade_date,
            "market": resolved_market,
        }
        return "start"

    append_jobs(
        session,
        [
            AnalysisJob(
                ticker=ticker,
                trade_date=trade_date,
                market=resolved_market,
                fresh=False,
            )
        ],
    )
    session["queue_advance_notice"] = (
        f"✅ {ticker} 已加入分析队列（并行槽位已满）"
    )
    return "enqueue"


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


def _submit_analysis_jobs(
    raw_tickers: str,
    trade_date: str,
    *,
    force_full_reeval: bool = False,
) -> None:
    """Start the first job immediately when idle; otherwise enqueue the whole batch.

    Tokens are auto-classified as CN or US by ``resolve_ticker_batch_mixed``.
    """
    tokens = parse_ticker_inputs(raw_tickers)
    if not tokens:
        st.error("❌ 请输入至少一个股票代码")
        return

    jobs, errors = resolve_ticker_batch_mixed(
        tokens,
        trade_date=trade_date,
        resolve_cn=_resolve_cn_or_raise,
        force_full_reeval=force_full_reeval,
        analysis_mode="auto",
    )
    for msg in errors:
        st.warning(f"⚠️ 已跳过 {msg}")
    if not jobs:
        st.error("❌ 没有可分析的有效代码")
        return

    if is_worker_mode():
        # Web only enqueues; the standalone worker drains the disk queue.
        added = default_store().append_atomic(jobs)
        queued = len(default_store().load())
        st.session_state["queue_advance_notice"] = (
            f"✅ 已提交 {added} 只到后台分析队列，完成后见历史（队列共 {queued}）"
        )
        request_clear_ticker_input(st.session_state)
        st.rerun()
        return

    running = running_count(st.session_state)
    is_busy = running > 0 or has_running(st.session_state)
    if is_busy:
        # Exclude any currently-running jobs from being re-queued.
        exclude: set[tuple[str, str, str]] = set()
        for t in active_runs(st.session_state):
            if t.ticker and t.trade_date:
                exclude.add((getattr(t, "market", market), t.ticker, t.trade_date))
        added = append_jobs(st.session_state, jobs, exclude=exclude)
        cn_slots = slots_available(st.session_state, market="CN")
        us_slots = slots_available(st.session_state, market="US")
        queued = len(queue_snapshot(st.session_state))
        if (cn_slots > 0 or us_slots > 0) and added > 0:
            st.session_state["queue_advance_notice"] = (
                f"✅ 已加入 {added} 只，空闲 A股 {cn_slots} / 美股 {us_slots} 个槽位将自动并行开跑"
                f"（队列共 {queued}）"
            )
        else:
            st.session_state["queue_advance_notice"] = (
                f"✅ 已加入分析队列 {added} 只（当前队列 {queued}）"
            )
        if added > 0:
            request_clear_ticker_input(st.session_state)
            # Rerun so lifecycle fills free slots and clears the ticker box.
            st.rerun()
        return

    head, *rest = jobs
    head_market = getattr(head, "market", "CN")
    if head_market == "CN" and tokens:
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
    st.session_state["viewing_accuracy"] = False
    st.query_params.clear()
    st.query_params["view"] = "home"
    # Clear ticker input only after app.py successfully begins analysis.


def _resolve_cn_or_raise(raw: str) -> str:
    code, err = _resolve_user_input(raw)
    if err:
        raise ValueError(err)
    return code


def _render_incomplete_tasks() -> None:
    """Render 未完成任务 list (inline, no fragment — refreshed via queue fragment)."""
    incomplete = get_incomplete_history()
    if not incomplete:
        st.caption("暂无未完成任务")
        return
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
        ):
            activate_incomplete_task(
                st.session_state,
                t,
                d,
                market=(
                    "CN" if t.isdigit() and len(t) == 6 else "US"
                ),
            )
            st.query_params.clear()
            st.query_params["view"] = "home"
            st.rerun()


@st.fragment(run_every=2.0)
def _render_queue_and_incomplete() -> None:
    """Fragment that auto-refreshes the analysis queue and incomplete tasks.

    Both sections are grouped in one fragment so Streamlit does not need to
    manage two independent sidebar fragments with overlapping widget trees.
    When no background work exists the fragment renders once and stops polling.
    """
    jobs = queue_snapshot(st.session_state)
    incomplete = get_incomplete_history()
    has_work = bool(has_running(st.session_state) or jobs or incomplete)

    if jobs:
        _render_analysis_queue_inner()
    if has_work:
        st.markdown("---")
        st.markdown("#### 未完成任务")
        _render_incomplete_tasks()


def _render_analysis_queue_inner() -> None:
    """Render the analysis queue display (fragment body)."""
    if is_worker_mode():
        _render_worker_queue()
        return
    jobs = queue_snapshot(st.session_state)
    if not jobs:
        return
    st.markdown("#### 分析队列")
    cn_slots = slots_available(st.session_state, market="CN")
    us_slots = slots_available(st.session_state, market="US")
    st.caption(
        f"A股最多 {cn_max_runs()} 只并行，空闲 {cn_slots} 个槽位；"
        f"美股最多 {us_max_runs()} 只并行，空闲 {us_slots} 个槽位。"
        "刷新后会从本地恢复，空闲时自动开跑。"
    )
    for idx, job in enumerate(jobs, start=1):
        st.caption(format_queue_job_caption(job, idx))

    is_busy = slots_available(st.session_state) < max_jobs_configured()
    cont_col, clear_col = st.columns(2)
    if cont_col.button(
        "继续队列",
        key="resume_analysis_queue",
        use_container_width=True,
        disabled=is_busy and not jobs,
        type="primary",
    ):
        # Force-fill free slots (overrides refresh incomplete gate).
        mark_serial_queue_session(st.session_state, True)
        request_fill_parallel_slots(st.session_state, force=True)
        st.session_state["viewing_history"] = None
        st.session_state["viewing_watchlist"] = False
        st.session_state["viewing_accuracy"] = False
        st.query_params.clear()
        st.query_params["view"] = "home"
        st.rerun()
    if clear_col.button("清空队列", key="clear_analysis_queue", use_container_width=True):
        clear_queue(st.session_state)
        st.rerun()


def _render_worker_queue() -> None:
    """Worker mode: queue is disk-authoritative and drained by a separate process."""
    jobs = default_store().load()
    if not jobs:
        return
    st.markdown("#### 分析队列")
    st.caption(
        f"共 {len(jobs)} 只等待后台执行（最多 {max_jobs_configured()} 并行）。"
        "关闭页面也会继续跑，完成后见历史。"
    )
    for idx, job in enumerate(jobs, start=1):
        st.caption(format_queue_job_caption(job, idx))
    if st.button("清空队列", key="clear_analysis_queue_worker", use_container_width=True):
        default_store().clear_atomic()
        st.rerun()


def max_jobs_configured() -> int:
    from web.parallel_runs import max_runs
    return max_runs()


def _render_history_page(entries: list[dict], page_size: int = 20, tab_key: str = "") -> None:
    """Render a paginated list of history entries with signal badges."""
    if not entries:
        st.caption("无匹配记录")
        return

    # 分页
    total = len(entries)
    total_pages = (total + page_size - 1) // page_size
    page_key = f"_hist_page_{tab_key}" if tab_key else "_hist_page"
    page = st.session_state.get(page_key, 0)
    if page >= total_pages:
        page = 0
        st.session_state[page_key] = 0

    start = page * page_size
    end = min(start + page_size, total)
    page_entries = entries[start:end]

    for entry in page_entries:
        t, d = entry["ticker"], entry["date"]
        signal = entry.get("signal", "N/A")
        sig_label = signal_text_tag(signal)
        label = format_list_ticker_label(t, d)
        display = f"{sig_label} {label}" if sig_label else label
        if st.button(
            display,
            key=f"{tab_key}_hist_{t}_{d}_{start}",
            use_container_width=True,
        ):
            navigate("history", ticker=t, date=d, path=entry["path"])

    # 翻页控制
    if total_pages > 1:
        cols = st.columns([1, 2, 1])
        with cols[0]:
            prev_key = f"{tab_key}_hist_prev"
            if st.button("◀ 上一页", key=prev_key, use_container_width=True, disabled=page == 0):
                st.session_state[page_key] = page - 1
                st.rerun()
        with cols[1]:
            st.caption(f"{page + 1}/{total_pages}")
        with cols[2]:
            next_key = f"{tab_key}_hist_next"
            if st.button("下一页 ▶", key=next_key, use_container_width=True, disabled=page >= total_pages - 1):
                st.session_state[page_key] = page + 1
                st.rerun()


def _render_analysis_controls(raw_tickers: str, trade_date_value: date) -> None:
    is_busy = has_running(st.session_state)
    runs = active_runs(st.session_state)
    trade_date = trade_date_value.strftime("%Y-%m-%d")

    pause_col, resume_col, stop_col = st.columns(3)

    any_not_paused = any(t.is_running and not t.is_paused and not t.stop_requested for t in runs)
    any_paused = any(t.is_paused for t in runs)

    if pause_col.button(
        "全部暂停",
        key="sidebar_pause_analysis",
        use_container_width=True,
        disabled=not any_not_paused,
    ):
        from web.parallel_runs import pause_all_runs
        pause_all_runs(st.session_state)
        st.rerun()

    if resume_col.button(
        "全部恢复",
        key="sidebar_resume_analysis",
        use_container_width=True,
        disabled=not any_paused,
    ):
        for t in runs:
            if t.is_paused:
                t.resume()
        st.rerun()

    can_stop = (
        bool(runs)
        or bool(raw_tickers.strip())
        or bool(queue_snapshot(st.session_state))
    )
    if stop_col.button(
        "清空队列并停止",
        key="sidebar_stop_analysis",
        use_container_width=True,
        disabled=not can_stop,
    ):
        clear_queue(st.session_state)
        from web.parallel_runs import stop_all_runs
        stop_all_runs(st.session_state)
        st.rerun()


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

    if st.button(
        "TradingAgents-Astock",
        key="nav_brand_home",
        use_container_width=True,
        help="回到策略扫描首页",
    ):
        set_home_mode(st.session_state, HOME_MODE_SCAN)
        navigate("home")
    st.markdown(
        """
        <div style="text-align:center; margin:-0.35rem 0 1.25rem 0;">
            <div style="font-size:0.85rem; color:#888;">
                A股多Agent投研系统
            </div>
            <div style="font-size:0.7rem; color:#555; margin-top:0.3rem;">
                by <a href="https://github.com/playsolo" style="color:#ff5a1f; text-decoration:none;">playsolo</a>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    from tradingagents import inbox

    unread = inbox.unread_count()
    inbox_label = f"🔔 最新（{unread}）" if unread else "🔔 最新"
    if st.button(
        inbox_label,
        key="nav_inbox",
        use_container_width=True,
        type="primary" if unread else "secondary",
        help="站内消息：分析完成 / 失败、数据缺失警告、观察池告警",
    ):
        navigate("inbox")

    if st.button(
        "📊 信号准确率",
        key="nav_accuracy",
        use_container_width=True,
        help="方向命中跟踪：做多/做空/持有后验结算",
    ):
        navigate("accuracy")

    st.markdown("---")
    st.markdown("#### 新建分析")

    apply_pending_clear_ticker_input(st.session_state)
    ticker_input = st.text_area(
        "股票代码（可多个）",
        placeholder=(
            "每行一个，或逗号分隔\n"
            "A股: 300750, 600519, 宁德时代\n"
            "美股: AAPL, NVDA, BRK.B"
        ),
        key=_INPUT_TICKERS_KEY,
        height=88,
        help=(
            "支持 A 股代码/中文全称和美股代码混输，系统自动判断市场。"
            "分析为串行队列，不会加入观察池。"
            "美股将通过本机 TradingAgents 项目子进程分析，进度实时回传。"
        ),
    )

    trade_date = st.date_input(
        "分析日期",
        value=cn_today(),
        key="input_date",
    )

    force_full_reeval = st.checkbox(
        "强制重新评估（忽略校准锚点，全量分析）",
        value=False,
        key="force_full_reeval",
        help=(
            "勾选后本次一律全量重跑并重置校准锚点。"
            "不勾选时由系统自动决定：无异常走伪增量，硬闸/不确定则全量。"
        ),
    )

    # 模型配置：仅 admin 可修改，其他用户只读
    user = st.session_state.get("auth_user")
    is_admin = bool(user and getattr(user, "role", None) == "admin")
    if is_admin:
        with st.expander("⚙️ 模型配置", expanded=False):
            _render_llm_config()
            st.caption(
                "美股分析会把此处模型配置转发到本机 TradingAgents；"
                "API Key 仍读对应项目的 `.env`。"
                "路径可用环境变量 US_TRADINGAGENTS_ROOT / US_TRADINGAGENTS_PYTHON 覆盖。"
            )
    else:
        render_model_config_info()

    any_stopping = any(
        t.stop_requested for t in active_runs(st.session_state)
    )
    has_input = bool(parse_ticker_inputs(ticker_input or ""))
    if any_stopping:
        start_label = "停止中..."
    elif has_running(st.session_state):
        start_label = "加入分析队列"
    else:
        start_label = "开始分析"

    if st.button(
        start_label,
        use_container_width=True,
        disabled=(any_stopping or not has_input),
        type="primary",
    ):
        _submit_analysis_jobs(
            ticker_input or "",
            trade_date.strftime("%Y-%m-%d"),
            force_full_reeval=bool(force_full_reeval),
        )

    _render_analysis_controls(ticker_input or "", trade_date)
    _render_queue_and_incomplete()
    st.markdown("#### 策略")
    if st.button(
        "📊 策略扫描",
        key="nav_value_swing",
        use_container_width=True,
        help="价值波段 + 成长加速双池：各自 Top15，互不改规则",
    ):
        set_home_mode(st.session_state, HOME_MODE_SCAN)
        navigate("home")

    st.markdown("#### 观察")
    from web.auth_page import current_watch_store

    watch_items = current_watch_store().list_items()
    watch_n = len(watch_items)
    alert_n = sum(len(i.alerts) for i in watch_items)
    watch_label = f"📡 观察池（{watch_n}）"
    if alert_n:
        watch_label += f" · {alert_n}告警"
    if st.button(watch_label, key="nav_watchlist", use_container_width=True):
        navigate("watch")

    st.markdown("---")
    st.markdown("#### 历史记录")

    history = get_history()
    if not history:
        st.caption("暂无历史记录")
        return

    search_raw = st.text_input(
        "搜索历史",
        placeholder="代码或中文名，如 300750 / 宁德时代 / AAPL",
        key="history_search_query",
        help="按 A 股代码、中文名或美股代码过滤；留空显示全部。",
        label_visibility="collapsed",
    )
    filter_ticker, filter_err = resolve_history_search_query(search_raw or "")
    if filter_err:
        st.caption(f"⚠️ {filter_err}")
        st.markdown("---")
        st.caption("⚠️ 仅供学习研究，不构成投资建议")
        return
    if filter_ticker:
        history = filter_history_by_ticker(history, filter_ticker)
        # 搜索条件变化时回到第一页，避免停在空页
        prev = st.session_state.get("_history_filter_ticker")
        if prev != filter_ticker:
            st.session_state["_history_filter_ticker"] = filter_ticker
            for tab_key in ("all", "buy", "sell", "hold"):
                st.session_state[f"_hist_page_{tab_key}"] = 0
        if not history:
            st.caption(f"未找到 {filter_ticker} 的历史报告")
            st.markdown("---")
            st.caption("⚠️ 仅供学习研究，不构成投资建议")
            return
    else:
        st.session_state.pop("_history_filter_ticker", None)

    # 按信号分组
    groups = group_history_by_signal(history)
    total = len(history)
    st.caption(signal_count_label(groups, total))

    # 分类 tab
    tab_all, tab_buy, tab_sell, tab_hold = st.tabs(
        ["全部", f"买入({len(groups['Buy'])})",
         f"卖出({len(groups['Sell'])})", f"持有({len(groups['Hold'])})"]
    )

    page_size = 20
    with tab_all:
        _render_history_page(history, page_size, tab_key="all")
    with tab_buy:
        _render_history_page(groups["Buy"], page_size, tab_key="buy")
    with tab_sell:
        _render_history_page(groups["Sell"], page_size, tab_key="sell")
    with tab_hold:
        _render_history_page(groups["Hold"], page_size, tab_key="hold")

    st.markdown("---")
    st.caption("⚠️ 仅供学习研究，不构成投资建议")
