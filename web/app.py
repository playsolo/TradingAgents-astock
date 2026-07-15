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

from web.analysis_queue import (  # noqa: E402
    format_restored_queue_blocked_notice,
    hydrate_queue,
    maybe_autostart_restored_queue,
    prepend_job,
    take_next_job,
)
from web.components.progress_panel import render_running_progress  # noqa: E402
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
from web.navigation import apply_query_to_session  # noqa: E402
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
if _restored > 0:
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
    start_watchlist_scheduler(config_provider=lambda: {
        "llm_provider": st.session_state.get("llm_provider", "deepseek"),
        "quick_think_llm": st.session_state.get("quick_think_llm", "deepseek-chat"),
        "deep_think_llm": st.session_state.get("deep_think_llm", "deepseek-chat"),
        "backend_url": (st.session_state.get("llm_base_url") or os.getenv("BACKEND_URL") or None),
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
    config = DEFAULT_CONFIG.copy()
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
    """Create tracker + background thread for one analysis request."""
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
    st.session_state["tracker"] = tracker
    st.session_state["viewing_history"] = None
    st.session_state["viewing_watchlist"] = False
    if start_req.get("watchlist_refresh"):
        st.session_state["watchlist_refresh_pending"] = {
            "ticker": start_req["ticker"],
            "trade_date": start_req["trade_date"],
        }
    else:
        # 非观察池升级启动时清掉残留 pending，避免误回写基准
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


# ── Sidebar ──────────────────────────────────────────────────────────────────

with st.sidebar:
    render_sidebar()


# ── Handle "Start Analysis" trigger from sidebar / resume ───────────────────

start_req = st.session_state.pop("start_analysis", None)
if start_req:
    _begin_analysis(start_req)
    # Only after a successful begin: clear ticker box on the following rerun.
    request_clear_ticker_input(st.session_state)
    # Sidebar already rendered above; rerun so 暂停/未完成任务/队列控件与 tracker 同步。
    st.rerun()


# ── Main area state machine ─────────────────────────────────────────────────

tracker: ProgressTracker | None = st.session_state.get("tracker")
viewing_history: str | None = st.session_state.get("viewing_history")
viewing_watchlist: bool = bool(st.session_state.get("viewing_watchlist"))


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

            from tradingagents.watchlist.store import default_store

            refreshed = refresh_from_analysis(
                active.final_state,
                ticker=active.ticker,
                trade_date=active.trade_date,
                log_path=resolve_log_path(active.ticker, active.trade_date),
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
            default_store().mark_observed(
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


# Queue auto-advance must run even if the user navigated to 观察池 / 历史
# while a serial job was finishing; otherwise the queue stalls.
# Start the next job in-process (no start_analysis round-trip) so sidebar
# cannot clobber the handoff on the following rerun.
if tracker and not tracker.is_running and (tracker.is_complete or tracker.error):
    _consume_watchlist_refresh_pending(tracker)
    if tracker.is_complete:
        next_job = take_next_job(st.session_state, finished_ticker=tracker.ticker)
    else:
        next_job = take_next_job(
            st.session_state,
            finished_ticker=tracker.ticker,
            error=str(tracker.error),
        )
    if next_job is not None:
        try:
            tracker = _begin_analysis(next_job.to_start_request())
            st.rerun()
        except Exception as exc:  # noqa: BLE001
            prepend_job(st.session_state, next_job)
            st.session_state.pop("queue_advance_notice", None)
            st.error(f"启动队列下一只失败，已放回队列队首：{exc}")
    viewing_history = st.session_state.get("viewing_history")
    viewing_watchlist = bool(st.session_state.get("viewing_watchlist"))

queue_notice = st.session_state.pop("queue_advance_notice", None)
if queue_notice:
    st.info(queue_notice)

# State 0.5: Watchlist observation page
if viewing_watchlist and not (tracker and tracker.is_running):
    render_watch_page()

# State 1: Viewing a historical analysis
elif viewing_history:
    try:
        state = load_analysis(viewing_history)
        signal = extract_signal(state)
        ticker = Path(viewing_history).parent.parent.name
        trade_date = Path(viewing_history).stem.replace("full_states_log_", "")

        def _on_history_state_updated(updated: dict) -> None:
            # Report JSON already persisted by regenerate_section; keep session coherent.
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

# State 2: Analysis running — fragment polls progress; no sleep (keeps sidebar responsive)
elif tracker and tracker.is_running:
    render_running_progress()

# State 3: Analysis complete
elif tracker and tracker.is_complete:
    def _on_live_state_updated(updated: dict) -> None:
        tracker.final_state = updated
        st.session_state["tracker"] = tracker

    live_config = _build_config()
    live_log = (
        Path(live_config["results_dir"])
        / tracker.ticker.upper()
        / "TradingAgentsStrategy_logs"
        / f"full_states_log_{tracker.trade_date}.json"
    )
    render_report(
        tracker.final_state,
        tracker.ticker,
        tracker.trade_date,
        tracker.signal,
        elapsed=tracker.elapsed,
        log_path=str(live_log) if live_log.exists() else None,
        llm_config=live_config,
        on_state_updated=_on_live_state_updated,
    )

# State 4: Analysis errored
elif tracker and tracker.error:
    st.error(f"分析失败: {tracker.error}")
    st.caption("已完成阶段会保存在本地断点中；修复模型额度或配置后，可以继续未完成的部分。")
    if st.button("继续未完成任务", type="primary"):
        st.session_state["start_analysis"] = {
            "ticker": tracker.ticker,
            "trade_date": tracker.trade_date,
        }
        st.session_state["viewing_history"] = None
        st.rerun()

# State 0: Idle — welcome screen with tabs for single-stock and strategy scan
else:
    tab_single, tab_scan = st.tabs(["📈 单票分析", "📊 策略扫描"])
    with tab_single:
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

    with tab_scan:
        from web.components.value_swing_scanner import render_value_swing_scanner
        render_value_swing_scanner()
