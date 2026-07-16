"""独立后台分析守护进程（不依赖 Streamlit Web）。

用法：
  tradingagents-analyze            # 前台常驻，持续消费磁盘分析队列
  tradingagents-analyze --once     # 把当前队列耗尽后退出（适合 cron / 测试）

与 Web 通过 ``~/.tradingagents/analyze.worker.lock`` 互斥：同一时刻只允许一个
worker 消费队列。Web 端设 ``TRADINGAGENTS_ANALYSIS_EXECUTOR=worker`` 后只入队、
不在进程内跑分析。并发上限由 ``TRADINGAGENTS_MAX_PARALLEL``（默认 3）控制。

中断恢复（A+B+C）：
  - C: 出队改为 lease；心跳过期或启动时回收，``resume_count`` +1 后重新入队
  - B: SIGTERM 优雅停机时把 in-flight 原样写回队列（不增加 resume_count）
  - A: 启动时把仍为 running 且有 checkpoint 的孤儿任务自动入队
  - 自动续跑上限：``TRADINGAGENTS_MAX_AUTO_RESUME``（默认 2）
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TextIO

from tradingagents.analyze_worker.executor import RunFn, build_worker_config, run_one_job
from web.analysis_queue import AnalysisJob, AnalysisQueueStore, default_store

logger = logging.getLogger(__name__)

WORKER_LOCK_PATH = Path.home() / ".tradingagents" / "analyze.worker.lock"

_POLL_SECONDS = 2.0
_DEFAULT_LEASE_TTL = 180.0
_DEFAULT_MAX_AUTO_RESUME = 2


def _max_workers() -> int:
    try:
        return max(1, int(os.environ.get("TRADINGAGENTS_MAX_PARALLEL", "3")))
    except ValueError:
        return 3


def _max_auto_resume() -> int:
    try:
        return max(0, int(os.environ.get("TRADINGAGENTS_MAX_AUTO_RESUME", str(_DEFAULT_MAX_AUTO_RESUME))))
    except ValueError:
        return _DEFAULT_MAX_AUTO_RESUME


def _lease_ttl_seconds() -> float:
    try:
        return max(30.0, float(os.environ.get("TRADINGAGENTS_QUEUE_LEASE_TTL", str(_DEFAULT_LEASE_TTL))))
    except ValueError:
        return _DEFAULT_LEASE_TTL


def acquire_worker_lock() -> TextIO | None:
    """Non-blocking exclusive lock; returns None if another worker holds it."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX
        WORKER_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        return open(WORKER_LOCK_PATH, "a+", encoding="utf-8")

    WORKER_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fh = open(WORKER_LOCK_PATH, "a+", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    fh.seek(0)
    fh.truncate()
    fh.write(f"pid={os.getpid()}\n")
    fh.flush()
    return fh


def _default_list_active() -> list[dict[str, Any]]:
    from web.history import list_active_incomplete_tasks

    return list_active_incomplete_tasks()


def _default_mark_error(ticker: str, trade_date: str, message: str) -> None:
    from web.history import record_incomplete_task

    record_incomplete_task(
        ticker, trade_date, status="error", error=message, completed_stages=[]
    )


def _has_resumable_checkpoint(ticker: str, trade_date: str) -> bool:
    try:
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.graph.checkpointer import has_checkpoint

        return bool(has_checkpoint(DEFAULT_CONFIG["data_cache_dir"], ticker, trade_date))
    except Exception:  # noqa: BLE001
        logger.exception("checkpoint probe failed for %s", ticker)
        return False


@dataclass
class RecoveryStats:
    """Outcome of startup recovery (lease reclaim + orphan auto-resume)."""

    requeued: list[tuple[str, str, int]] = field(default_factory=list)
    exhausted: list[tuple[str, str]] = field(default_factory=list)
    marked_error: list[tuple[str, str]] = field(default_factory=list)


def reconcile_orphan_runs(
    *,
    list_active_fn: Callable[[], list[dict[str, Any]]] = _default_list_active,
    mark_error_fn: Callable[[str, str, str], None] = _default_mark_error,
    message: str = "worker 重启，上次运行未完成（已标记出错，可重新入队）",
) -> list[tuple[str, str]]:
    """Mark any ``running`` incomplete tasks as ``error`` on worker startup.

    Kept for backward-compatible unit tests. Prefer :func:`recover_interrupted_runs`
    which auto-requeues when under the resume limit.
    """
    orphaned: list[tuple[str, str]] = []
    for entry in list_active_fn():
        if entry.get("status") != "running":
            continue
        ticker = str(entry.get("ticker") or "")
        trade_date = str(entry.get("trade_date") or "")
        if not ticker or not trade_date:
            continue
        try:
            mark_error_fn(ticker, trade_date, message)
        except Exception:  # noqa: BLE001
            logger.exception("failed to reconcile orphan %s", ticker)
            continue
        orphaned.append((ticker, trade_date))
    if orphaned:
        logger.info("reconciled %d orphaned running task(s)", len(orphaned))
    return orphaned


def recover_interrupted_runs(
    store: AnalysisQueueStore,
    *,
    max_auto_resume: int | None = None,
    list_active_fn: Callable[[], list[dict[str, Any]]] | None = None,
    mark_error_fn: Callable[[str, str, str], None] | None = None,
    has_checkpoint_fn: Callable[[str, str], bool] | None = None,
) -> RecoveryStats:
    """Startup recovery: reclaim leases (C) + auto-resume orphans (A).

    Auto-resume only when a checkpoint exists and ``resume_count < max``.
    Exhausted or non-resumable orphans are marked ``error``.
    """
    limit = _max_auto_resume() if max_auto_resume is None else max(0, int(max_auto_resume))
    stats = RecoveryStats()
    list_active = list_active_fn or _default_list_active
    mark_error = mark_error_fn or _default_mark_error
    has_checkpoint = has_checkpoint_fn or _has_resumable_checkpoint

    # C: any leftover leases belong to a dead worker — reclaim with bump.
    reclaimed, lease_exhausted = store.reclaim_expired(
        ttl_seconds=0,
        max_auto_resume=limit,
    )
    for job in reclaimed:
        stats.requeued.append((job.ticker, job.trade_date, job.resume_count))
    for job in lease_exhausted:
        stats.exhausted.append((job.ticker, job.trade_date))
        mark_error(
            job.ticker,
            job.trade_date,
            f"已达自动续跑上限（{limit} 次），请手动从侧栏「未完成任务」续跑或重新分析",
        )

    if reclaimed:
        logger.info("reclaimed %d lease(s) into queue", len(reclaimed))
    if lease_exhausted:
        logger.warning("lease reclaim exhausted for %d job(s)", len(lease_exhausted))

    # A: disk ``running`` orphans not already queued/leased.
    for entry in list_active():
        if entry.get("status") != "running":
            continue
        ticker = str(entry.get("ticker") or "")
        trade_date = str(entry.get("trade_date") or "")
        market = str(entry.get("market") or "CN")
        if not ticker or not trade_date:
            continue
        try:
            resume_count = int(entry.get("resume_count", 0) or 0)
        except (TypeError, ValueError):
            resume_count = 0

        already = any(
            j.ticker == ticker and j.trade_date == trade_date
            for j in list(store.load()) + [lease.job for lease in store.load_leases()]
        )
        if already:
            continue

        if resume_count >= limit:
            mark_error(
                ticker,
                trade_date,
                f"已达自动续跑上限（{limit} 次），请手动从侧栏「未完成任务」续跑或重新分析",
            )
            stats.exhausted.append((ticker, trade_date))
            continue

        if not has_checkpoint(ticker, trade_date):
            mark_error(
                ticker,
                trade_date,
                "worker 重启，上次运行未完成且无可用断点（已标记出错，可重新入队）",
            )
            stats.marked_error.append((ticker, trade_date))
            continue

        next_count = resume_count + 1
        job = AnalysisJob(
            ticker=ticker,
            trade_date=trade_date,
            market=market if market in {"CN", "US"} else "CN",
            fresh=False,
            resume_count=next_count,
        )
        added = store.append_atomic([job])
        if added:
            stats.requeued.append((ticker, trade_date, next_count))
            logger.info(
                "auto-resume enqueue %s %s (resume_count=%d)",
                ticker,
                trade_date,
                next_count,
            )

    return stats


def _mark_exhausted(job: AnalysisJob, max_auto_resume: int) -> None:
    from web.history import record_incomplete_task

    record_incomplete_task(
        job.ticker,
        job.trade_date,
        status="error",
        error=(
            f"已达自动续跑上限（{max_auto_resume} 次），"
            "请手动从侧栏「未完成任务」续跑或重新分析"
        ),
        completed_stages=[],
        resume_count=job.resume_count,
    )


class AnalyzeWorker:
    """Consumes the disk analysis queue with a bounded thread pool."""

    def __init__(
        self,
        *,
        store: AnalysisQueueStore,
        config: dict[str, Any],
        max_workers: int = 3,
        run_fn: RunFn = run_one_job,
        lease_ttl_seconds: float | None = None,
        max_auto_resume: int | None = None,
    ) -> None:
        self.store = store
        self.config = config
        self.max_workers = max(1, int(max_workers))
        self.run_fn = run_fn
        self.lease_ttl_seconds = (
            _lease_ttl_seconds() if lease_ttl_seconds is None else float(lease_ttl_seconds)
        )
        self.max_auto_resume = (
            _max_auto_resume() if max_auto_resume is None else max(0, int(max_auto_resume))
        )
        self._stop = threading.Event()
        self._in_flight: dict[Future, AnalysisJob] = {}
        self._in_flight_lock = threading.Lock()

    def request_stop(self) -> None:
        self._stop.set()

    def _reclaim_stale(self) -> None:
        reclaimed, exhausted = self.store.reclaim_expired(
            ttl_seconds=self.lease_ttl_seconds,
            max_auto_resume=self.max_auto_resume,
        )
        for job in exhausted:
            try:
                _mark_exhausted(job, self.max_auto_resume)
            except Exception:  # noqa: BLE001
                logger.exception("failed to mark exhausted %s", job.ticker)
        if reclaimed:
            logger.info("reclaimed %d expired lease(s)", len(reclaimed))

    def _heartbeat_in_flight(self) -> None:
        with self._in_flight_lock:
            jobs = list(self._in_flight.values())
        for job in jobs:
            try:
                self.store.heartbeat(job.identity())
            except Exception:  # noqa: BLE001
                logger.exception("heartbeat failed for %s", job.ticker)

    def _cn_in_flight(self) -> int:
        """Count in-flight jobs whose market is CN."""
        with self._in_flight_lock:
            return sum(
                1 for job in self._in_flight.values()
                if (job.market or "CN").upper() == "CN"
            )

    def _us_in_flight(self) -> int:
        """Count in-flight jobs whose market is US."""
        with self._in_flight_lock:
            return sum(
                1 for job in self._in_flight.values()
                if (job.market or "CN").upper() == "US"
            )

    def _claim_batch(self, free_slots: int) -> list[AnalysisJob]:
        """Atomically claim up to ``free_slots`` jobs from the queue.

        CN and US jobs use separate parallel pools controlled by
        ``CN_MAX_PARALLEL`` and ``US_MAX_PARALLEL`` env vars (each defaults
        to 3).  The queue is scanned in FIFO order; a job is claimed only
        when its market has a free slot.  The total returned never exceeds
        ``free_slots``.

        CN slots are filled first (preserves approximate FIFO for mixed
        queues where CN jobs tend to be ahead of US jobs).
        """
        self._reclaim_stale()
        cn_cap = max(0, int(os.environ.get("CN_MAX_PARALLEL", "3")) - self._cn_in_flight())
        us_cap = max(0, int(os.environ.get("US_MAX_PARALLEL", "3")) - self._us_in_flight())
        batch: list[AnalysisJob] = []
        remaining = max(0, free_slots)
        # Phase 1: fill CN slots up to min(cn_cap, remaining).
        cn_quota = min(cn_cap, remaining)
        for _ in range(cn_quota):
            job = self.store.claim_next_by_market("CN")
            if job is None:
                break
            batch.append(job)
            remaining -= 1
        # Phase 2: fill US slots up to min(us_cap, remaining).
        us_quota = min(us_cap, remaining)
        for _ in range(us_quota):
            job = self.store.claim_next_by_market("US")
            if job is None:
                break
            batch.append(job)
            remaining -= 1
        return batch

    def _run_safe(self, job: AnalysisJob) -> None:
        try:
            self.run_fn(job, self.config)
        except Exception:  # noqa: BLE001 - run_fn should not raise; guard the pool
            logger.exception("run_fn crashed for %s", getattr(job, "ticker", "?"))
        finally:
            try:
                self.store.complete(job.identity())
            except Exception:  # noqa: BLE001
                logger.exception("lease complete failed for %s", job.ticker)

    def _reap(self, in_flight: set[Future]) -> set[Future]:
        alive: set[Future] = set()
        with self._in_flight_lock:
            for fut in in_flight:
                if fut.done():
                    self._in_flight.pop(fut, None)
                else:
                    alive.add(fut)
        return alive

    def _graceful_requeue(self) -> None:
        """B: put in-flight leases back on the waiting queue without bumping resume."""
        with self._in_flight_lock:
            jobs = list(self._in_flight.values())
            self._in_flight.clear()
        if not jobs:
            # Still release any leases this process may hold.
            leases = self.store.load_leases()
            idents = [
                lease.job.identity()
                for lease in leases
                if lease.owner_pid == os.getpid()
            ]
        else:
            idents = [job.identity() for job in jobs]
        if not idents:
            return
        requeued, _exhausted = self.store.release_to_queue(
            idents,
            bump_resume=False,
            max_auto_resume=self.max_auto_resume,
        )
        if requeued:
            logger.info(
                "graceful stop requeued %d in-flight job(s): %s",
                len(requeued),
                ", ".join(j.ticker for j in requeued),
            )

    def run_until_drained(self, *, poll_seconds: float = _POLL_SECONDS) -> None:
        """Run until the queue is empty and all in-flight jobs finish."""
        pool = ThreadPoolExecutor(max_workers=self.max_workers)
        in_flight: set[Future] = set()
        try:
            while not self._stop.is_set():
                in_flight = self._reap(in_flight)
                self._heartbeat_in_flight()
                free = self.max_workers - len(in_flight)
                batch = self._claim_batch(free) if free > 0 else []
                for job in batch:
                    fut = pool.submit(self._run_safe, job)
                    with self._in_flight_lock:
                        self._in_flight[fut] = job
                    in_flight.add(fut)
                if not in_flight and not batch:
                    break
                time.sleep(poll_seconds)
            if self._stop.is_set():
                self._graceful_requeue()
            else:
                # Normal drain: wait for the last batch to finish.
                for fut in list(in_flight):
                    try:
                        fut.result()
                    except Exception:  # noqa: BLE001
                        pass
        finally:
            pool.shutdown(wait=not self._stop.is_set(), cancel_futures=True)

    def run_forever(self, *, poll_seconds: float = _POLL_SECONDS) -> None:
        """Poll the queue forever, keeping up to ``max_workers`` jobs running."""
        logger.info(
            "analyze worker running — max_parallel=%d, poll=%.1fs, lease_ttl=%.0fs, max_auto_resume=%d",
            self.max_workers,
            poll_seconds,
            self.lease_ttl_seconds,
            self.max_auto_resume,
        )
        pool = ThreadPoolExecutor(max_workers=self.max_workers)
        in_flight: set[Future] = set()
        try:
            while not self._stop.is_set():
                in_flight = self._reap(in_flight)
                self._heartbeat_in_flight()
                free = self.max_workers - len(in_flight)
                batch = self._claim_batch(free) if free > 0 else []
                for job in batch:
                    logger.info(
                        "dispatch %s %s (fresh=%s resume_count=%d)",
                        job.ticker,
                        job.trade_date,
                        job.fresh,
                        job.resume_count,
                    )
                    fut = pool.submit(self._run_safe, job)
                    with self._in_flight_lock:
                        self._in_flight[fut] = job
                    in_flight.add(fut)
                self._stop.wait(poll_seconds)
            # B: requeue in-flight then exit promptly so systemd can restart.
            # Do not wait for running analyses — process exit aborts those threads;
            # checkpoints + requeued jobs let the next worker continue.
            self._graceful_requeue()
        finally:
            pool.shutdown(wait=False, cancel_futures=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="A股分析队列后台守护进程（不依赖 Streamlit Web）",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="把当前队列耗尽后退出（不常驻）",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="打印 DEBUG 日志",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        from dotenv import load_dotenv

        root = Path(__file__).resolve().parents[2]
        load_dotenv(root / ".env", override=True)
    except Exception:
        pass

    lock = acquire_worker_lock()
    if lock is None:
        logger.error(
            "无法启动：已有分析 worker 在运行。若确认无进程，可删除 %s 后重试。",
            WORKER_LOCK_PATH,
        )
        return 1

    worker: AnalyzeWorker | None = None

    def _handle_signal(signum, _frame):
        logger.info("received signal %s, stopping…", signum)
        if worker is not None:
            worker.request_stop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handle_signal)
        except (ValueError, OSError):  # pragma: no cover - non-main thread
            pass

    try:
        store = default_store()
        stats = recover_interrupted_runs(store)
        if stats.requeued:
            logger.info(
                "startup auto-resume queued %d job(s)",
                len(stats.requeued),
            )
        if stats.exhausted or stats.marked_error:
            logger.warning(
                "startup recovery exhausted=%d marked_error=%d",
                len(stats.exhausted),
                len(stats.marked_error),
            )
        worker = AnalyzeWorker(
            store=store,
            config=build_worker_config(),
            max_workers=_max_workers(),
        )
        if args.once:
            worker.run_until_drained()
            logger.info("队列已耗尽，退出")
        else:
            worker.run_forever()
        return 0
    finally:
        try:
            lock.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
