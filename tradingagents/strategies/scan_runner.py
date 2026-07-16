"""价值波段扫描任务：独立进程执行，结果落盘，可选入分析队列。

Web / cron 均可调用；``start_detached_scan`` 使用新会话，关 Streamlit 后仍继续。
"""

from __future__ import annotations

import inspect
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from tradingagents.strategies.scan_store import (
    SCAN_STATUS_RUNNING,
    STRATEGY_BOTH,
    STRATEGY_GROWTH_ACCEL,
    STRATEGY_VALUE_SWING,
    ValueSwingScanStore,
    default_store,
    expand_strategies,
    resolve_strategy,
)
from tradingagents.strategies.value_swing import run_value_swing_scan

logger = logging.getLogger(__name__)

ScanFn = Callable[..., Any]

# 进度落盘节流：同一秒内多次个股回调只写一次盘（阶段切换强制写）。
_PROGRESS_MIN_INTERVAL_S = 0.5


def _scan_fn_accepts_progress(fn: ScanFn) -> bool:
    """True if ``fn`` accepts a ``progress_cb`` keyword (or **kwargs)."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    if "progress_cb" in params:
        return True
    return any(p.kind == p.VAR_KEYWORD for p in params.values())


def is_process_alive(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def result_dict_from_scan(result: Any, *, strategy: str = STRATEGY_VALUE_SWING) -> dict[str, Any]:
    """Serialize ScanResult into the Web UI payload shape."""
    strategy = resolve_strategy(strategy)
    if strategy == STRATEGY_GROWTH_ACCEL:
        from tradingagents.strategies.growth_accel import (
            l2_factor_hits,
            l2_score_max,
            selection_rules_snapshot,
            why_selected_line,
        )

        score_max = l2_score_max()
        return {
            "ok": True,
            "strategy": STRATEGY_GROWTH_ACCEL,
            "scan_date": result.scan_date,
            "total_stocks": result.total_stocks,
            "l0_passed": result.l0_passed,
            "l1a_passed": result.l1a_passed,
            "l1b_passed": result.l1b_passed,
            "l2_passed": result.l2_passed,
            "rules": selection_rules_snapshot(),
            "score_max": score_max,
            "candidates": [
                {
                    "code": c.code,
                    "name": c.name,
                    "price": c.price,
                    "pe_ttm": c.pe_ttm,
                    "pb": c.pb,
                    "signal_score": c.signal_score,
                    "score_max": score_max,
                    "track": c.track,
                    "np_ttm": c.np_ttm,
                    "np_ttm_yoy": (
                        round(c.np_ttm_yoy * 100, 1) if c.np_ttm_yoy is not None else None
                    ),
                    "rev_ttm_yoy": (
                        round(c.rev_ttm_yoy * 100, 1) if c.rev_ttm_yoy is not None else None
                    ),
                    "used_deduct": c.used_deduct,
                    "no_nonrecurring": c.no_nonrecurring,
                    "ocf_ttm": c.ocf_ttm,
                    "ocf_score_delta": c.ocf_score_delta,
                    "turnaround": c.turnaround,
                    "loss_narrowed": c.loss_narrowed,
                    "revenue_accel": c.revenue_accel,
                    "profit_accel": c.profit_accel,
                    "growth_theme": c.growth_theme,
                    "high_liquidity": c.high_liquidity,
                    "exp_score_delta": c.exp_score_delta,
                    "exp_fwd_pe": round(c.exp_fwd_pe, 1) if c.exp_fwd_pe is not None else None,
                    "exp_implied_cagr": (
                        round(c.exp_implied_cagr * 100, 1)
                        if c.exp_implied_cagr is not None
                        else None
                    ),
                    "exp_analysts": c.exp_analysts,
                    "exp_low_coverage": c.exp_low_coverage,
                    "factor_hits": l2_factor_hits(c),
                    "why": why_selected_line(c),
                }
                for c in result.candidates
            ],
            "duration_seconds": result.duration_seconds,
        }

    from tradingagents.strategies.value_swing import (
        l2_factor_hits,
        l2_score_max,
        selection_rules_snapshot,
        why_selected_line,
    )

    score_max = l2_score_max()
    return {
        "ok": True,
        "strategy": STRATEGY_VALUE_SWING,
        "scan_date": result.scan_date,
        "total_stocks": result.total_stocks,
        "l0_passed": result.l0_passed,
        "l1a_passed": result.l1a_passed,
        "l1b_passed": result.l1b_passed,
        "l2_passed": result.l2_passed,
        "rules": selection_rules_snapshot(),
        "score_max": score_max,
        "candidates": [
            {
                "code": c.code,
                "name": c.name,
                "price": c.price,
                "pe_ttm": c.pe_ttm,
                "pb": c.pb,
                "signal_score": c.signal_score,
                "score_max": score_max,
                "debt_ratio": round(c.debt_ratio * 100, 1) if c.debt_ratio else None,
                "revenue_growth": (
                    round(c.revenue_growth * 100, 1) if c.revenue_growth else None
                ),
                "northbound_net_3d": c.northbound_net_3d,
                "above_ma20": c.above_ma20,
                "near_ma250": c.near_ma250,
                "news_found": c.news_found,
                "ret_5d": round(c.ret_5d, 4) if getattr(c, "ret_5d", None) is not None else None,
                "hot_topic_match": c.hot_topic_match,
                "concept_active": c.concept_active,
                "exp_score_delta": c.exp_score_delta,
                "exp_fwd_pe": round(c.exp_fwd_pe, 1) if c.exp_fwd_pe is not None else None,
                "exp_analysts": c.exp_analysts,
                "exp_low_coverage": c.exp_low_coverage,
                "factor_hits": l2_factor_hits(c),
                "why": why_selected_line(c),
            }
            for c in result.candidates
        ],
        "duration_seconds": result.duration_seconds,
    }


def _scan_fn_for_strategy(strategy: str) -> ScanFn:
    strategy = resolve_strategy(strategy)
    if strategy == STRATEGY_GROWTH_ACCEL:
        from tradingagents.strategies.growth_accel import run_growth_accel_scan

        return run_growth_accel_scan
    return run_value_swing_scan


def candidates_to_analysis_jobs(
    candidates: list[dict[str, Any]],
    *,
    trade_date: str,
):
    from web.analysis_queue import AnalysisJob

    jobs = []
    for c in candidates:
        code = str(c.get("code") or "").strip()
        if not code:
            continue
        jobs.append(
            AnalysisJob(
                ticker=code,
                trade_date=trade_date,
                market="CN",
                fresh=True,
            )
        )
    return jobs


def enqueue_scan_candidates(
    candidates: list[dict[str, Any]],
    *,
    trade_date: str,
    queue_store=None,
) -> int:
    """Append scan candidates to the persisted analysis queue. Returns added count."""
    from web.analysis_queue import AnalysisQueueStore, append_jobs

    store = queue_store if queue_store is not None else AnalysisQueueStore()
    jobs = candidates_to_analysis_jobs(candidates, trade_date=trade_date)
    if not jobs:
        return 0
    # Disk-first: hydrate a scratch session from existing queue, then append.
    session: dict[str, Any] = {"analysis_queue": store.load()}
    return append_jobs(session, jobs, store=store)


def _refuse_if_running(
    scan_store: ValueSwingScanStore,
    current_pid: int | None = None,
) -> None:
    """Raise if another *live* scan owns the store.

    ``current_pid`` lets the owning process (whose pid was pre-reserved by a
    parent launcher) proceed without tripping on its own running record.
    """
    record = scan_store.load()
    running_pid = record.get("pid")
    if (
        record.get("status") == SCAN_STATUS_RUNNING
        and is_process_alive(running_pid)
        and running_pid != current_pid
    ):
        raise RuntimeError(
            f"已有扫描在运行（pid={running_pid}）。请等待结束后再启动。"
        )


_FUNNEL_KEYS = ("stage", "l0_passed", "l1a_passed", "l1b_passed", "l2_passed")


def _make_progress_writer(store: ValueSwingScanStore) -> Callable[[dict[str, Any]], None]:
    """Throttled callback that persists progress snapshots into the store.

    Throttles high-frequency same-stage churn (percent / current stock) to at
    most one write per ``_PROGRESS_MIN_INTERVAL_S``, but always flushes
    immediately when a milestone changes (stage transition or any funnel count),
    so persisted progress never drops a count update.
    """
    state: dict[str, Any] = {"last_write": 0.0, "last_milestone": None}

    def _write(progress: dict[str, Any]) -> None:
        now = time.monotonic()
        milestone = tuple(progress.get(k) for k in _FUNNEL_KEYS)
        milestone_changed = milestone != state["last_milestone"]
        if not milestone_changed and (now - state["last_write"]) < _PROGRESS_MIN_INTERVAL_S:
            return
        state["last_write"] = now
        state["last_milestone"] = milestone
        try:
            store.update_progress(progress)
        except Exception:  # noqa: BLE001 — progress persistence is best-effort
            logger.debug("failed to persist scan progress", exc_info=True)

    return _write


def _scan_kwargs(
    fn: ScanFn,
    max_candidates: int,
    store: ValueSwingScanStore,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"max_candidates": max_candidates}
    if _scan_fn_accepts_progress(fn):
        kwargs["progress_cb"] = _make_progress_writer(store)
    return kwargs


def run_scan_job(
    *,
    max_candidates: int = 15,
    enqueue_on_success: bool = False,
    scan_store: ValueSwingScanStore | None = None,
    queue_store=None,
    scan_fn: ScanFn | None = None,
    pid: int | None = None,
    strategy: str = STRATEGY_VALUE_SWING,
) -> dict[str, Any]:
    """Execute one scan, persist status/result, optionally enqueue candidates."""
    strategy = resolve_strategy(strategy)
    store = scan_store or default_store(strategy)
    own_pid = pid if pid is not None else os.getpid()
    with store.exclusive():
        _refuse_if_running(store, current_pid=own_pid)
        store.mark_running(
            max_candidates=max_candidates,
            enqueue_on_success=enqueue_on_success,
            pid=own_pid,
        )
    fn = scan_fn or _scan_fn_for_strategy(strategy)
    # Only a failure of the scan itself is a failed run.
    try:
        result = fn(**_scan_kwargs(fn, max_candidates, store))
        payload = result_dict_from_scan(result, strategy=strategy)
    except Exception as exc:
        logger.exception("%s 扫描失败", strategy)
        return store.mark_failed(str(exc))

    # The scan succeeded — enqueue is best-effort and must never discard the
    # result (critical for overnight auto-enqueue runs).
    enqueued = 0
    if enqueue_on_success:
        trade_date = payload.get("scan_date") or datetime.now().strftime("%Y-%m-%d")
        try:
            enqueued = enqueue_scan_candidates(
                payload.get("candidates") or [],
                trade_date=trade_date,
                queue_store=queue_store,
            )
        except Exception:  # noqa: BLE001 — persist the scan regardless
            logger.exception("扫描候选入队失败（结果已保留）")
            enqueued = 0

    return store.mark_completed(payload, enqueued=enqueued)


def reconcile_stale_running(
    scan_store: ValueSwingScanStore | None = None,
) -> dict[str, Any]:
    """If status is running but pid is dead, mark failed."""
    store = scan_store or default_store()
    record = store.load()
    if record.get("status") == SCAN_STATUS_RUNNING and not is_process_alive(
        record.get("pid")
    ):
        return store.mark_failed(
            f"扫描进程已退出（pid={record.get('pid')}），无完整结果"
        )
    return record


def start_detached_scan(
    *,
    max_candidates: int = 15,
    enqueue_on_success: bool = False,
    status_path: Path | None = None,
    archive_dir: Path | None = None,
    log_path: Path | None = None,
    strategy: str = STRATEGY_BOTH,
) -> int:
    """Spawn an independent scan process that outlives the Web parent.

    ``strategy=both`` (default) runs value_swing then growth_accel in one child
    process; both status files are reserved with the same pid.
    """
    strategies = expand_strategies(strategy)
    cli_strategy = STRATEGY_BOTH if len(strategies) > 1 else strategies[0]

    stores: list[ValueSwingScanStore]
    if len(strategies) == 1 and (status_path is not None or archive_dir is not None):
        stores = [
            ValueSwingScanStore(
                path=status_path, archive_dir=archive_dir, strategy=strategies[0]
            )
        ]
    else:
        stores = [default_store(s) for s in strategies]

    env = os.environ.copy()
    default_log = Path.home() / ".tradingagents" / "strategy_scan.log"
    if cli_strategy == STRATEGY_GROWTH_ACCEL:
        default_log = Path.home() / ".tradingagents" / "growth_accel_scan.log"
        if status_path is not None:
            env["TRADINGAGENTS_GROWTH_ACCEL_SCAN_PATH"] = str(status_path)
        if archive_dir is not None:
            env["TRADINGAGENTS_GROWTH_ACCEL_SCANS_DIR"] = str(archive_dir)
    elif cli_strategy == STRATEGY_VALUE_SWING:
        default_log = Path.home() / ".tradingagents" / "value_swing_scan.log"
        if status_path is not None:
            env["TRADINGAGENTS_VALUE_SWING_SCAN_PATH"] = str(status_path)
        if archive_dir is not None:
            env["TRADINGAGENTS_VALUE_SWING_SCANS_DIR"] = str(archive_dir)

    argv = [
        sys.executable,
        "-m",
        "tradingagents.strategies.scan_cli",
        "--strategy",
        cli_strategy,
        "--max-candidates",
        str(int(max_candidates)),
    ]
    if enqueue_on_success:
        argv.append("--enqueue")

    log_file = log_path or default_log
    log_file.parent.mkdir(parents=True, exist_ok=True)

    # Nested exclusive: always value then growth to avoid lock-order deadlock.
    # Atomically refuse-if-running + spawn + reserve so cron/Web cannot race.
    def _spawn_and_reserve() -> int:
        for store in stores:
            _refuse_if_running(store)
        with open(log_file, "a", encoding="utf-8") as log_fh:
            proc = subprocess.Popen(
                argv,
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                env=env,
                close_fds=True,
            )
        for store in stores:
            store.mark_running(
                max_candidates=max_candidates,
                enqueue_on_success=enqueue_on_success,
                pid=proc.pid,
            )
        return int(proc.pid)

    if len(stores) == 1:
        with stores[0].exclusive():
            return _spawn_and_reserve()
    with stores[0].exclusive():
        with stores[1].exclusive():
            return _spawn_and_reserve()
