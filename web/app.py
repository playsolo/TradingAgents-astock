"""TradingAgents A股分析 — Streamlit Web UI."""

from __future__ import annotations

import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Must run before streamlit/pandas/pyarrow import: mimalloc TLS SIGSEGV on
# analysis worker threads (macOS "Python quit", exit 139).
from tradingagents.runtime.arrow_safety import (  # noqa: E402
    configure_arrow_memory_pool,
    warm_arrow_for_worker_threads,
)

configure_arrow_memory_pool()

from dotenv import load_dotenv  # noqa: E402

# override=True：让 .env 的值优先于进程里可能残留的空/旧环境变量（#66）。
# 注意：load_dotenv 仅在进程启动时执行一次，启动后修改 .env 仍需重启 Web 服务才生效。
load_dotenv(_PROJECT_ROOT / ".env", override=True)
# Re-assert after dotenv in case .env overwrote the pool.
configure_arrow_memory_pool()
warm_arrow_for_worker_threads()

import streamlit as st  # noqa: E402

from tradingagents.default_config import DEFAULT_CONFIG  # noqa: E402
from tradingagents.auth.model_config import load_model_config, model_config_exists  # noqa: E402

from web.analysis_queue import (  # noqa: E402
    AnalysisJob,
    default_store,
    format_restored_queue_blocked_notice,
    hydrate_queue,
    maybe_autostart_restored_queue,
    prepend_job,
    set_begin_analysis_hook,
    take_next_job,
)
from tradingagents.analyze_worker import is_worker_mode  # noqa: E402
from web.components.progress_panel import (  # noqa: E402
    render_multi_progress,
    render_running_progress,
)
from web.components.inbox_page import render_inbox_page  # noqa: E402
from web.components.report_viewer import render_report  # noqa: E402
from web.components.sidebar import render_sidebar, request_clear_ticker_input  # noqa: E402
from web.components.watch_page import render_watch_page  # noqa: E402
from web.history import (  # noqa: E402
    clear_incomplete_task,
    extract_signal,
    format_refresh_incomplete_notice,
    list_active_incomplete_tasks,
    load_analysis,
    record_incomplete_task,
)
from web.home_mode import (  # noqa: E402
    HOME_MODE_KEY,
    HOME_MODE_SCAN,
    HOME_MODE_SINGLE,
    resolve_idle_panel,
    set_home_mode,
)
from web.navigation import apply_query_to_session  # noqa: E402
from web.parallel_runs import (
    active_runs,
    add_tracker,
    focused_ticker,
    has_running,
    remove_finished_tracker,
    running_count,
    set_focused_ticker,
    slots_available,
    stop_run_by_ticker,
    pause_run_by_ticker,
    resume_run_by_ticker,
    try_fill_parallel_slots,
)
from web.progress import ProgressTracker  # noqa: E402
from web.runner import run_analysis_in_thread  # noqa: E402
from tradingagents.watchlist.scheduler import start_watchlist_scheduler  # noqa: E402

from web.auth_page import require_auth, get_current_user, render_logout_button, render_admin_panel  # noqa: E402

# ── Page config ──────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="TradingAgents-Astock A股分析",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# URL → session（观察池 / 历史可刷新、可收藏）——须在 set_page_config 之后
apply_query_to_session()

# ── Authentication gate ────────────────────────────────────────────────────
# Must be after set_page_config — require_auth calls st.stop() if not logged in.
require_auth()

# Restore waiting queue after browser refresh (session_state is empty on new session).
# Auto-start only when idle: no live tracker and no running/paused incomplete task
# on disk (refresh drops ProgressTracker while a daemon thread may still be alive).
_restored = hydrate_queue(st.session_state)
_tracker = st.session_state.get("tracker")
_tracker_running = bool(_tracker is not None and getattr(_tracker, "is_running", False))

# Refresh drops ProgressTracker; surface any incomplete run still recorded on disk.
_active_incomplete: list = []
if (
    not st.session_state.get("_refresh_incomplete_noticed")
    and _tracker is None
):
    st.session_state["_refresh_incomplete_noticed"] = True
    _active_incomplete = list_active_incomplete_tasks()

_started = None
if _restored > 0 and not is_worker_mode():
    # Always re-check disk for the overlap gate (listing above is notice-only once).
    _gate_incomplete = _active_incomplete or list_active_incomplete_tasks()
    def _mark_autostart_running(job) -> None:
        # Before queue pop is persisted — blocks other sessions' idle gate.
        record_incomplete_task(
            job.ticker,
            job.trade_date,
            status="running",
            completed_stages=[],
        )

    _started = maybe_autostart_restored_queue(
        st.session_state,
        restored=_restored,
        tracker_running=_tracker_running,
        incomplete_entries=_gate_incomplete,
        before_commit=_mark_autostart_running,
    )
    if _started is not None:
        # Match「继续队列」: drop history/watch URL so rerun shows live progress.
        st.query_params.clear()
        st.query_params["view"] = "home"
    else:
        st.session_state["queue_advance_notice"] = format_restored_queue_blocked_notice(
            _restored,
            tracker_running=_tracker_running,
            incomplete_entries=_gate_incomplete,
            start_already_set=bool(st.session_state.get("start_analysis")),
        )

# After a successful auto-start, skip the overlap warning — a new run was just staged.
if _active_incomplete and _started is None:
    _incomplete_notice = format_refresh_incomplete_notice(_active_incomplete)
    if _incomplete_notice:
        existing = st.session_state.get("queue_advance_notice")
        st.session_state["queue_advance_notice"] = (
            f"{existing}；{_incomplete_notice}" if existing else _incomplete_notice
        )


# 默认可由独立守护 tradingagents-watch 负责到点观察（不开 Web 也跑）。
# 若要改回「仅 Web 存活时调度」，启动前设 WATCHLIST_SCHEDULER=1。
if os.getenv("WATCHLIST_SCHEDULER", "0").strip() not in {"0", "false", "off"}:
    _watchlist_cfg = load_model_config()
    start_watchlist_scheduler(config_provider=lambda: {
        "llm_provider": _watchlist_cfg.get("llm_provider", "deepseek"),
        "quick_think_llm": _watchlist_cfg.get("quick_think_llm", "deepseek-chat"),
        "deep_think_llm": _watchlist_cfg.get("deep_think_llm", "deepseek-chat"),
        "backend_url": (_watchlist_cfg.get("backend_url") or os.getenv("BACKEND_URL") or None),
    })

# ── Custom CSS ───────────────────────────────────────────────────────────────

st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;900&display=swap');

    /* Hide Streamlit chrome for clean video recording.
       IMPORTANT: do NOT `display:none` the whole header OR the whole toolbar.
       In Streamlit >= 1.36 the "expand sidebar" button lives *inside* the
       toolbar (header > stToolbar > stExpandSidebarButton), so hiding either
       one makes a collapsed sidebar impossible to reopen (issue #36). Instead
       keep the header/toolbar in the DOM, make the header transparent, and
       hide only the individual chrome widgets we don't want on camera. */
    #MainMenu,
    footer,
    div[data-testid="stDecoration"],
    div[data-testid="stStatusWidget"],
    div[data-testid="stToolbarActions"],
    div[data-testid="stAppDeployButton"],
    span[data-testid="stMainMenu"] { display: none !important; }
    header[data-testid="stHeader"] {
        background: transparent !important;
        box-shadow: none !important;
    }
    /* Keep the sidebar collapse / expand controls always visible & clickable.
       Selector list spans multiple Streamlit versions. */
    button[data-testid="stExpandSidebarButton"],
    button[data-testid="stSidebarCollapseButton"],
    button[data-testid="collapsedControl"],
    [data-testid="stSidebarCollapsedControl"] {
        display: flex !important;
        visibility: visible !important;
        opacity: 1 !important;
    }

    html, body, [class*="css"] {
        font-family: 'Inter', -apple-system, sans-serif;
    }
    .stApp {
        background: #0a0a0a;
    }
    section[data-testid="stSidebar"] {
        background: #0f0f0f;
        border-right: 1px solid #1a1a1a;
    }
    .stMetric label { color: #888 !important; font-size: 0.8rem !important; }
    .stMetric [data-testid="stMetricValue"] {
        color: #ff5a1f !important;
        font-weight: 700 !important;
    }
    .stProgress > div > div > div {
        background: linear-gradient(90deg, #ff5a1f, #ff8c42) !important;
    }
    button[kind="primary"] {
        background: linear-gradient(135deg, #ff5a1f, #ff8c42) !important;
        border: none !important;
        font-weight: 700 !important;
        letter-spacing: 0.05em !important;
        box-shadow: 0 4px 15px rgba(255,90,31,0.3) !important;
        transition: all 0.2s ease !important;
    }
    button[kind="primary"]:hover {
        background: linear-gradient(135deg, #e04d15, #ff5a1f) !important;
        box-shadow: 0 6px 20px rgba(255,90,31,0.4) !important;
        transform: translateY(-1px) !important;
    }
    /* Secondary buttons (history items) */
    button[kind="secondary"] {
        background: #161616 !important;
        border: 1px solid #2a2a2a !important;
        color: #ccc !important;
        transition: all 0.2s ease !important;
    }
    button[kind="secondary"]:hover {
        background: #1e1e1e !important;
        border-color: #ff5a1f !important;
        color: #ff5a1f !important;
    }
    .stExpander {
        border: 1px solid #222 !important;
        border-radius: 8px !important;
    }
    .stTabs [data-baseweb="tab"] {
        color: #888 !important;
    }
    .stTabs [aria-selected="true"] {
        color: #ff5a1f !important;
        border-bottom-color: #ff5a1f !important;
    }
    /* Home mode switcher (replaces st.tabs which can stack panels on 1.59.x) */
    div[data-testid="stRadio"] > div {
        gap: 0.25rem !important;
    }
    div[data-testid="stRadio"] label[data-baseweb="radio"] {
        background: transparent !important;
        border-bottom: 2px solid transparent !important;
        padding: 0.4rem 0.9rem !important;
        color: #888 !important;
    }
    div[data-testid="stRadio"] label[data-baseweb="radio"]:has(input:checked) {
        color: #ff5a1f !important;
        border-bottom-color: #ff5a1f !important;
    }
    div[data-testid="stRadio"] label[data-baseweb="radio"] > div:first-child {
        display: none !important;
    }
    div[data-testid="stDownloadButton"] button {
        background: #1a1a2e !important;
        border: 1px solid #ff5a1f !important;
        color: #ff5a1f !important;
    }
    /* Text input styling */
    input[data-testid="stTextInputRootElement"] input,
    .stTextInput input {
        background: #161616 !important;
        border-color: #2a2a2a !important;
        color: #f5f1eb !important;
    }
    .stTextInput input:focus {
        border-color: #ff5a1f !important;
        box-shadow: 0 0 0 1px #ff5a1f !important;
    }
    /* Date input styling */
    .stDateInput input {
        background: #161616 !important;
        border-color: #2a2a2a !important;
        color: #f5f1eb !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ── Build config ─────────────────────────────────────────────────────────────

def _build_config() -> dict:
    """Build runtime config from the system model source of truth.

    When auth is on, the admin's persisted model config is used for all users.
    If auth is disabled (dev mode) or no model_config.json exists yet, fall
    back to session_state (which the sidebar populates).
    """
    config = DEFAULT_CONFIG.copy()

    # Persisted config is the source of truth: admin set it for everyone.
    if model_config_exists():
        persisted = load_model_config()
        config["llm_provider"] = persisted["llm_provider"]
        config["deep_think_llm"] = persisted["deep_think_llm"]
        config["quick_think_llm"] = persisted["quick_think_llm"]
        backend_url = (persisted.get("backend_url") or os.getenv("BACKEND_URL") or "").strip()
        config["backend_url"] = backend_url or None
    else:
        config["llm_provider"] = st.session_state.get("llm_provider", "minimax")
        config["deep_think_llm"] = st.session_state.get("deep_think_llm", "MiniMax-M2.7")
        config["quick_think_llm"] = st.session_state.get("quick_think_llm", "MiniMax-M2.7-highspeed")
        # Optional third-party / proxy endpoint. Sidebar input wins, else .env BACKEND_URL.
        backend_url = (st.session_state.get("llm_base_url") or os.getenv("BACKEND_URL") or "").strip()
        config["backend_url"] = backend_url or None
    config["data_vendors"] = {
        "core_stock_apis": "a_stock",
        "technical_indicators": "a_stock",
        "fundamental_data": "a_stock",
        "news_data": "a_stock",
        "signal_data": "a_stock",
    }
    # 与 CLI Research Depth=Deep 对齐：多空辩论 + 风险三方辩论各 5 轮
    config["max_debate_rounds"] = 5
    config["max_risk_discuss_rounds"] = 5
    config["checkpoint_enabled"] = True
    config["output_language"] = "Chinese"
    return config


# ── Start Analysis helpers ───────────────────────────────────────────────────

def _infer_market(ticker: str, explicit: str | None = None) -> str:
    if explicit in {"CN", "US"}:
        return explicit
    code = (ticker or "").strip()
    if code.isdigit() and len(code) == 6:
        return "CN"
    return "US"


def _begin_analysis(start_req: dict) -> ProgressTracker:
    """Create tracker + background thread for one analysis request.

    This is the single-start entry point (one job at a time).
    In parallel mode it also adds the tracker to the active runs pool.
    """
    market = _infer_market(start_req["ticker"], start_req.get("market"))
    st.session_state["analysis_market"] = market

    if start_req.get("fresh"):
        clear_incomplete_task(start_req["ticker"], start_req["trade_date"])
        if market == "CN":
            from tradingagents.graph.checkpointer import clear_checkpoint

            clear_checkpoint(
                DEFAULT_CONFIG["data_cache_dir"],
                start_req["ticker"],
                start_req["trade_date"],
            )

    tracker = ProgressTracker(
        ticker=start_req["ticker"],
        trade_date=start_req["trade_date"],
        market=market,
    )
    # Register into the parallel run pool.
    add_tracker(st.session_state, tracker)
    st.session_state["viewing_history"] = None
    st.session_state["viewing_watchlist"] = False
    st.session_state["viewing_inbox"] = False
    if start_req.get("watchlist_refresh"):
        st.session_state["watchlist_refresh_pending"] = {
            "ticker": start_req["ticker"],
            "trade_date": start_req["trade_date"],
        }
    else:
        st.session_state["watchlist_refresh_pending"] = None
    run_analysis_in_thread(
        ticker=start_req["ticker"],
        trade_date=start_req["trade_date"],
        config=_build_config(),
        tracker=tracker,
        market=market,
        extra_past_context=str(start_req.get("past_context") or ""),
    )
    return tracker


def _begin_analysis_from_job(job: AnalysisJob) -> ProgressTracker:
    """Start one analysis from an AnalysisJob (for parallel queue dispatch)."""
    return _begin_analysis(job.to_start_request())


def _enqueue_start_request(start_req: dict) -> None:
    """Worker mode: push a start request onto the disk queue instead of running it."""
    market = _infer_market(start_req["ticker"], start_req.get("market"))
    job = AnalysisJob(
        ticker=start_req["ticker"],
        trade_date=start_req["trade_date"],
        market=market,
        fresh=bool(start_req.get("fresh", True)),
    )
    added = default_store().append_atomic([job])
    st.session_state["viewing_history"] = None
    st.session_state["viewing_watchlist"] = False
    st.session_state["viewing_inbox"] = False
    if added:
        st.session_state["queue_advance_notice"] = (
            f"✅ {job.ticker} 已提交后台分析队列，完成后可在历史查看"
        )
    else:
        st.session_state["queue_advance_notice"] = f"{job.ticker} 已在后台队列中"


# Register the hook so analysis_queue can start jobs without circular import.
set_begin_analysis_hook(_begin_analysis_from_job)


# ── Sidebar ──────────────────────────────────────────────────────────────────

with st.sidebar:
    render_sidebar()


# ── Handle "Start Analysis" trigger from sidebar / resume ───────────────────

start_req = st.session_state.pop("start_analysis", None)
if start_req:
    if is_worker_mode():
        # Web only enqueues; the standalone worker executes the analysis.
        _enqueue_start_request(start_req)
    else:
        _begin_analysis(start_req)
    # Only after a successful begin/enqueue: clear ticker box on the following rerun.
    request_clear_ticker_input(st.session_state)
    # Sidebar already rendered above; rerun so 暂停/未完成任务/队列控件与 tracker 同步。
    st.rerun()


# ── Main area state machine ─────────────────────────────────────────────────

# Only define a single-tracker ref for the auto-start block above; the
# multi-run state machine uses active_runs() directly.
viewing_history: str | None = st.session_state.get("viewing_history")
viewing_watchlist: bool = bool(st.session_state.get("viewing_watchlist"))
viewing_inbox: bool = bool(st.session_state.get("viewing_inbox"))


def _consume_watchlist_refresh_pending(active: ProgressTracker) -> None:
    """Apply or clear watchlist baseline refresh before queue auto-advance."""
    pending = st.session_state.get("watchlist_refresh_pending")
    if not (
        isinstance(pending, dict)
        and pending.get("ticker") == active.ticker
        and pending.get("trade_date") == active.trade_date
    ):
        return

    if active.is_complete and active.final_state:
        from tradingagents.watchlist.service import refresh_from_analysis, resolve_log_path

        try:
            from datetime import datetime

            from web.auth_page import current_watch_store

            watch_store = current_watch_store()
            refreshed = refresh_from_analysis(
                active.final_state,
                ticker=active.ticker,
                trade_date=active.trade_date,
                log_path=resolve_log_path(active.ticker, active.trade_date),
                store=watch_store,
            )
            stance = refreshed.baseline.stance
            summary = (
                f"完整再分析已完成（基准日 {active.trade_date}）| 立场 {stance}"
                + (
                    f" | {refreshed.baseline.thesis_summary}"
                    if refreshed.baseline.thesis_summary
                    else ""
                )
            )
            watch_store.mark_observed(
                active.ticker,
                datetime.now().isoformat(timespec="seconds"),
                summary=summary,
                briefing=None,
            )
            st.success(f"观察池基准已更新：{active.ticker}（完整再分析）")
        except Exception as exc:  # noqa: BLE001
            st.warning(f"分析完成，但更新观察池基准失败：{exc}")
        st.session_state["watchlist_refresh_pending"] = None
        return

    if active.error:
        st.session_state["watchlist_refresh_pending"] = None
        st.warning("完整再分析失败，观察池基准未更新。可修复后从观察池重试。")


# ── Multi-run lifecycle: clean finished trackers, fill empty slots ──────
# Fill whenever free slots + queued jobs (not only after a finish), so busy
# enqueue / idle multi-submit / 「继续队列」都能立刻并行开跑。
# In worker mode the standalone process owns execution — Web never runs here.
if not is_worker_mode() and not st.session_state.get("_parallel_lifecycle_ran"):
    st.session_state["_parallel_lifecycle_ran"] = True
    need_rerun = False
    for t in active_runs(st.session_state):
        if t.is_complete or t.error:
            need_rerun = True
            _consume_watchlist_refresh_pending(t)
            remove_finished_tracker(st.session_state, t.ticker, t.trade_date)

    _fill_incomplete = None
    if not has_running(st.session_state):
        # Cold / refresh session: avoid overlapping a daemon still marked running.
        _fill_incomplete = list_active_incomplete_tasks()
    started = try_fill_parallel_slots(
        st.session_state,
        _begin_analysis_from_job,
        incomplete_entries=_fill_incomplete,
    )
    if started:
        tickers = ", ".join(t.ticker for t in started)
        remaining = len(st.session_state.get("analysis_queue") or [])
        suffix = f"（队列剩余 {remaining}）" if remaining else ""
        st.session_state["queue_advance_notice"] = (
            f"并行开跑 {tickers}{suffix}"
        )
        st.rerun()
    elif need_rerun:
        st.rerun()

st.session_state["_parallel_lifecycle_ran"] = False

queue_notice = st.session_state.pop("queue_advance_notice", None)
if queue_notice:
    st.info(queue_notice)

# ── State routing ──────────────────────────────────────────────────────────

_focus = focused_ticker(st.session_state) or ""
_all_runs = active_runs(st.session_state)
_any_running = has_running(st.session_state)

# State 0.4: Inbox (event center)
if viewing_inbox and not _any_running:
    render_inbox_page()

# State 0.5: Watchlist observation page
elif viewing_watchlist and not _any_running:
    render_watch_page()

# State 1: Viewing a historical analysis
elif viewing_history:
    try:
        state = load_analysis(viewing_history)
        signal = extract_signal(state)
        ticker = Path(viewing_history).parent.parent.name
        trade_date = Path(viewing_history).stem.replace("full_states_log_", "")

        def _on_history_state_updated(updated: dict) -> None:
            st.session_state["viewing_history"] = viewing_history

        render_report(
            state,
            ticker,
            trade_date,
            signal,
            log_path=viewing_history,
            llm_config=_build_config(),
            on_state_updated=_on_history_state_updated,
        )
    except Exception as exc:
        st.error(f"加载失败: {exc}")

# State 2: Multi-run progress (when any run is active)
elif _any_running:
    render_multi_progress()

# State 3: Show a finished report (pick focused or last completed)
elif _all_runs:
    target = None
    for t in _all_runs:
        if t.ticker == _focus and (t.is_complete or t.error):
            target = t
            break
    if target is None:
        for t in reversed(_all_runs):
            if t.is_complete or t.error:
                target = t
                break
    if target is not None:
        if target.error:
            st.error(f"{target.ticker} 分析失败: {target.error}")
            st.caption("已完成阶段保存在本地断点中。")
        else:
            def _on_live_state_updated(updated: dict) -> None:
                target.final_state = updated

            live_config = _build_config()
            live_log = (
                Path(live_config["results_dir"])
                / target.ticker.upper()
                / "TradingAgentsStrategy_logs"
                / f"full_states_log_{target.trade_date}.json"
            )
            render_report(
                target.final_state,
                target.ticker,
                target.trade_date,
                target.signal,
                elapsed=target.elapsed,
                log_path=str(live_log) if live_log.exists() else None,
                llm_config=live_config,
                on_state_updated=_on_live_state_updated,
            )
    else:
        st.info("所有分析任务已完成，无可用报告。")

# State 0: Idle — exclusive single-stock / strategy-scan panels (no st.tabs;
# Streamlit 1.59.x can stack all tab bodies after switch/widget rerun).
else:
    if HOME_MODE_KEY not in st.session_state:
        set_home_mode(st.session_state, HOME_MODE_SINGLE)

    selected = st.radio(
        "主页视图",
        options=[HOME_MODE_SINGLE, HOME_MODE_SCAN],
        format_func=lambda m: (
            "📈 单票分析" if m == HOME_MODE_SINGLE else "📊 策略扫描"
        ),
        horizontal=True,
        key=HOME_MODE_KEY,
        label_visibility="collapsed",
    )

    if resolve_idle_panel(selected) == "scan":
        from web.components.value_swing_scanner import render_value_swing_scanner

        render_value_swing_scanner()
    else:
        st.markdown(
            """
            <div style="
                display: flex;
                flex-direction: column;
                align-items: center;
                justify-content: center;
                min-height: 60vh;
                text-align: center;
            ">
                <div style="font-size: 4rem; margin-bottom: 1rem;">📈</div>
                <div style="
                    font-size: 2.5rem;
                    font-weight: 900;
                    margin-bottom: 0.5rem;
                ">
                    <span style="color: #ff5a1f;">Trading</span><span style="color: #f5f1eb;">Agents</span><span style="color: #f5f1eb;">-</span><span style="color: #ff5a1f;">Astock</span>
                </div>
                <div style="color: #888; font-size: 1.1rem; max-width: 500px; line-height: 1.6;">
                    A股 / 美股多Agent投研分析<br>
                    侧栏选择市场 → 分析师辩论 → 风控评估 → 最终决策
                </div>
                <div style="
                    margin-top: 2rem;
                    padding: 1rem 2rem;
                    border: 1px solid #222;
                    border-radius: 12px;
                    color: #666;
                    font-size: 0.9rem;
                ">
                    ← 在左侧选择 A股或美股，输入一只或多只代码后开始分析
                </div>
                <div style="
                    margin-top: 2.5rem;
                    padding: 0.8rem 1.5rem;
                    color: #555;
                    font-size: 0.75rem;
                    max-width: 500px;
                    line-height: 1.6;
                    border-top: 1px solid #1a1a1a;
                ">
                    ⚠️ 本项目仅供学习研究与技术演示，不构成任何投资建议。<br>
                    投资决策请咨询持牌专业机构。作者不对使用本工具产生的任何损失承担责任。
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )