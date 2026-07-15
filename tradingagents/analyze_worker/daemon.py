"""独立后台分析守护进程（不依赖 Streamlit Web）。

用法：
  tradingagents-analyze            # 前台常驻，持续消费磁盘分析队列
  tradingagents-analyze --once     # 把当前队列耗尽后退出（适合 cron / 测试）

与 Web 通过 ``~/.tradingagents/analyze.worker.lock`` 互斥：同一时刻只允许一个
worker 消费队列。Web 端设 ``TRADINGAGENTS_ANALYSIS_EXECUTOR=worker`` 后只入队、
不在进程内跑分析。并发上限由 ``TRADINGAGENTS_MAX_PARALLEL``（默认 3）控制。
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
from pathlib import Path
from typing import Any, Callable, TextIO

from tradingagents.analyze_worker.executor import RunFn, build_worker_config, run_one_job
from web.analysis_queue import AnalysisJob, AnalysisQueueStore, default_store

logger = logging.getLogger(__name__)

WORKER_LOCK_PATH = Path.home() / ".tradingagents" / "analyze.worker.lock"

_POLL_SECONDS = 2.0


def _max_workers() -> int:
    try:
        return max(1, int(os.environ.get("TRADINGAGENTS_MAX_PARALLEL", "3")))
    except ValueError:
        return 3


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


def reconcile_orphan_runs(
    *,
    list_active_fn: Callable[[], list[dict[str, Any]]] = _default_list_active,
    mark_error_fn: Callable[[str, str, str], None] = _default_mark_error,
    message: str = "worker 重启，上次运行未完成（已标记出错，可重新入队）",
) -> list[tuple[str, str]]:
    """Mark any ``running`` incomplete tasks as ``error`` on worker startup.

    In worker mode Web never writes ``running``; so at startup every ``running``
    entry is an orphan from a previous crash. Returns the reconciled identities.
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


class AnalyzeWorker:
    """Consumes the disk analysis queue with a bounded thread pool."""

    def __init__(
        self,
        *,
        store: AnalysisQueueStore,
        config: dict[str, Any],
        max_workers: int = 3,
        run_fn: RunFn = run_one_job,
    ) -> None:
        self.store = store
        self.config = config
        self.max_workers = max(1, int(max_workers))
        self.run_fn = run_fn
        self._stop = threading.Event()

    def request_stop(self) -> None:
        self._stop.set()

    def _claim_batch(self, free_slots: int) -> list[AnalysisJob]:
        """Atomically claim up to ``free_slots`` jobs from the queue (FIFO)."""
        batch: list[AnalysisJob] = []
        for _ in range(max(0, free_slots)):
            job = self.store.claim_next()
            if job is None:
                break
            batch.append(job)
        return batch

    def _run_safe(self, job: AnalysisJob) -> None:
        try:
            self.run_fn(job, self.config)
        except Exception:  # noqa: BLE001 - run_fn should not raise; guard the pool
            logger.exception("run_fn crashed for %s", getattr(job, "ticker", "?"))

    def _reap(self, in_flight: set[Future]) -> set[Future]:
        return {f for f in in_flight if not f.done()}

    def run_until_drained(self, *, poll_seconds: float = _POLL_SECONDS) -> None:
        """Run until the queue is empty and all in-flight jobs finish."""
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            in_flight: set[Future] = set()
            while not self._stop.is_set():
                in_flight = self._reap(in_flight)
                free = self.max_workers - len(in_flight)
                batch = self._claim_batch(free) if free > 0 else []
                for job in batch:
                    in_flight.add(pool.submit(self._run_safe, job))
                if not in_flight and not batch:
                    break
                time.sleep(poll_seconds)

    def run_forever(self, *, poll_seconds: float = _POLL_SECONDS) -> None:
        """Poll the queue forever, keeping up to ``max_workers`` jobs running."""
        logger.info(
            "analyze worker running — max_parallel=%d, poll=%.1fs",
            self.max_workers,
            poll_seconds,
        )
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            in_flight: set[Future] = set()
            while not self._stop.is_set():
                in_flight = self._reap(in_flight)
                free = self.max_workers - len(in_flight)
                batch = self._claim_batch(free) if free > 0 else []
                for job in batch:
                    logger.info("dispatch %s %s", job.ticker, job.trade_date)
                    in_flight.add(pool.submit(self._run_safe, job))
                self._stop.wait(poll_seconds)
            # Drain in-flight on shutdown so systemd stop is graceful.
            for f in in_flight:
                try:
                    f.result(timeout=0)
                except Exception:  # noqa: BLE001
                    pass


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
        reconcile_orphan_runs()
        worker = AnalyzeWorker(
            store=default_store(),
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
