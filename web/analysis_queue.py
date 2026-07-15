"""FIFO queue for sequential multi-ticker deep analysis.

In-process state lives in Streamlit ``session_state``; the waiting queue is also
persisted under ``~/.tradingagents/analysis_queue.json`` so a browser refresh
can restore it. Independent of the watchlist observation pool.
"""

from __future__ import annotations

import json
import os
import re
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, MutableMapping, Optional

QUEUE_SESSION_KEY = "analysis_queue"
SERIAL_QUEUE_SESSION_KEY = "serial_queue_session"
_HYDRATED_FLAG = "_analysis_queue_hydrated"

# Disk incomplete statuses that may still mean a background analysis is alive.
_BLOCKING_INCOMPLETE_STATUSES = frozenset({"running", "paused"})

# Split on whitespace / common list separators; keep Yahoo symbols like BRK.B intact.
_TICKER_SPLIT_RE = re.compile(r"[\s,，;；、]+")

_STORE_LOCK = threading.RLock()


@dataclass(frozen=True)
class AnalysisJob:
    ticker: str
    trade_date: str
    market: str  # "CN" | "US"
    fresh: bool = True

    def to_start_request(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "trade_date": self.trade_date,
            "fresh": self.fresh,
            "market": self.market,
        }

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def identity(self) -> tuple[str, str, str]:
        return (self.market, self.ticker, self.trade_date)

    @classmethod
    def from_mapping(cls, raw: Any) -> "AnalysisJob":
        if isinstance(raw, cls):
            return raw
        # Streamlit hot-reload replaces this class; session may still hold instances
        # from the previous class object (isinstance fails, but attributes remain).
        if not isinstance(raw, (dict, MutableMapping)) and hasattr(raw, "ticker"):
            return cls(
                ticker=str(getattr(raw, "ticker")),
                trade_date=str(getattr(raw, "trade_date", "")),
                market=str(getattr(raw, "market", None) or "CN"),
                fresh=bool(getattr(raw, "fresh", True)),
            )
        return cls(
            ticker=str(raw["ticker"]),
            trade_date=str(raw["trade_date"]),
            market=str(raw.get("market") or "CN"),
            fresh=bool(raw.get("fresh", True)),
        )


class AnalysisQueueStore:
    """JSON persistence for the waiting analysis queue."""

    def __init__(self, path: Path | None = None):
        if path is not None:
            self.path = Path(path)
        else:
            override = os.getenv("TRADINGAGENTS_ANALYSIS_QUEUE_PATH", "").strip()
            self.path = (
                Path(override)
                if override
                else Path.home() / ".tradingagents" / "analysis_queue.json"
            )

    def load(self) -> list[AnalysisJob]:
        with _STORE_LOCK:
            if not self.path.exists():
                return []
            try:
                with open(self.path, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError, TypeError):
                return []
            if not isinstance(data, dict):
                return []
            jobs: list[AnalysisJob] = []
            for raw in data.get("jobs") or []:
                if not isinstance(raw, dict):
                    continue
                try:
                    jobs.append(AnalysisJob.from_mapping(raw))
                except (KeyError, TypeError, ValueError):
                    continue
            return jobs

    def save(self, jobs: list[AnalysisJob]) -> None:
        with _STORE_LOCK:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "jobs": [job.to_dict() for job in jobs],
            }
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            tmp.replace(self.path)

    # ── Cross-process atomic ops (Web appends, worker claims) ─────────────
    #
    # ``save`` uses tmp+replace which swaps the inode, so flock must live on a
    # separate sidecar file (``.lock``) rather than the data file itself.
    # These guard the whole read-modify-write so a Web append and a worker
    # claim on different processes never clobber each other.

    def _lock_path(self) -> Path:
        return self.path.with_suffix(".lock")

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        """Hold an exclusive inter-process lock around a read-modify-write.

        Falls back to a no-op file lock on platforms without ``fcntl``
        (e.g. Windows); the in-process ``_STORE_LOCK`` still applies.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            import fcntl
        except ImportError:  # pragma: no cover - non-POSIX fallback
            with _STORE_LOCK:
                yield
            return
        fh = open(self._lock_path(), "a+", encoding="utf-8")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            with _STORE_LOCK:
                yield
        finally:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            finally:
                fh.close()

    def claim_next(self) -> Optional[AnalysisJob]:
        """Atomically pop and return the head job, or None when empty."""
        with self.exclusive():
            jobs = self.load()
            if not jobs:
                return None
            head = jobs.pop(0)
            self.save(jobs)
            return head

    def append_atomic(
        self,
        new_jobs: list[AnalysisJob],
        *,
        exclude: Optional[set[tuple[str, str, str]]] = None,
    ) -> int:
        """Atomically append jobs to the on-disk queue, skipping duplicates.

        Returns the number of jobs actually added.

        Identities that end up in the waiting queue (newly added *or* already
        present) drop any matching incomplete-task row so the sidebar does not
        show both「队列中」and「出错/未完成」.
        """
        with self.exclusive():
            jobs = self.load()
            existing = {j.identity() for j in jobs}
            if exclude:
                existing |= exclude
            added = 0
            for job in new_jobs:
                ident = job.identity()
                if ident in existing:
                    continue
                jobs.append(job)
                existing.add(ident)
                added += 1
            if added:
                self.save(jobs)
            queued = {j.identity() for j in jobs}
        _clear_incomplete_for_queued(new_jobs, queued)
        return added

    def clear_atomic(self) -> None:
        """Atomically empty the on-disk queue."""
        with self.exclusive():
            self.save([])


def default_store() -> AnalysisQueueStore:
    return AnalysisQueueStore()


def _resolve_store(store: AnalysisQueueStore | None) -> AnalysisQueueStore:
    return store if store is not None else default_store()


def _persist(session: MutableMapping[str, Any], store: AnalysisQueueStore | None) -> None:
    _resolve_store(store).save(_coerce_list(session))


def parse_ticker_inputs(raw: str) -> list[str]:
    """Split multi-ticker text into ordered unique tokens (case-insensitive dedupe)."""
    tokens: list[str] = []
    seen: set[str] = set()
    for part in _TICKER_SPLIT_RE.split((raw or "").strip()):
        token = part.strip()
        if not token:
            continue
        key = token.casefold()
        if key in seen:
            continue
        seen.add(key)
        tokens.append(token)
    return tokens


def resolve_ticker_batch(
    raw_tickers: list[str],
    *,
    market: str,
    trade_date: str,
    resolve_cn: Callable[[str], str],
) -> tuple[list[AnalysisJob], list[str]]:
    """Resolve a batch for one market. Invalid entries become error strings; valid become jobs."""
    jobs: list[AnalysisJob] = []
    errors: list[str] = []
    seen: set[tuple[str, str, str]] = set()

    for raw in raw_tickers:
        token = (raw or "").strip()
        if not token:
            continue
        try:
            if market == "US":
                code = token.upper()
                if any(ch.isspace() for ch in code):
                    raise ValueError("美股代码不能包含空格")
                if code.isdigit() and len(code) == 6:
                    raise ValueError("看起来像 A 股代码；请切换到「A股」市场后再分析")
            else:
                code = resolve_cn(token)
            job = AnalysisJob(
                ticker=code,
                trade_date=trade_date,
                market=market,
                fresh=True,
            )
            ident = job.identity()
            if ident in seen:
                continue
            seen.add(ident)
            jobs.append(job)
        except ValueError as exc:
            errors.append(f"{token}: {exc}")

    return jobs, errors


def format_queue_job_caption(job: AnalysisJob, index: int) -> str:
    """Sidebar caption: ``N. code name · date · market`` (name when resolvable)."""
    from web.stock_display import format_list_ticker_label

    market_tag = "美股" if job.market == "US" else "A股"
    return f"{index}. {format_list_ticker_label(job.ticker, job.trade_date, market_tag)}"


def _coerce_list(session: MutableMapping[str, Any]) -> list[AnalysisJob]:
    raw = session.get(QUEUE_SESSION_KEY) or []
    jobs = [AnalysisJob.from_mapping(item) for item in raw]
    session[QUEUE_SESSION_KEY] = jobs
    return jobs


def mark_serial_queue_session(session: MutableMapping[str, Any], active: bool = True) -> None:
    session[SERIAL_QUEUE_SESSION_KEY] = bool(active)


def is_serial_queue_session(session: MutableMapping[str, Any]) -> bool:
    return bool(session.get(SERIAL_QUEUE_SESSION_KEY))


def hydrate_queue(
    session: MutableMapping[str, Any],
    *,
    store: AnalysisQueueStore | None = None,
) -> int:
    """Load disk queue into a fresh session once. Returns how many jobs were restored."""
    if session.get(_HYDRATED_FLAG):
        return 0
    session[_HYDRATED_FLAG] = True
    existing = _coerce_list(session)
    if existing:
        # Session already has jobs (e.g. same process rerun) — keep and sync disk.
        mark_serial_queue_session(session, True)
        _persist(session, store)
        return 0
    jobs = _resolve_store(store).load()
    session[QUEUE_SESSION_KEY] = list(jobs)
    if jobs:
        mark_serial_queue_session(session, True)
    return len(jobs)


def has_blocking_incomplete_run(
    incomplete_entries: list[Any] | None,
) -> bool:
    """True when disk still shows a run that may overlap a new analysis."""
    for entry in incomplete_entries or []:
        if isinstance(entry, MutableMapping):
            status = entry.get("status")
        else:
            status = getattr(entry, "status", None)
        if status in _BLOCKING_INCOMPLETE_STATUSES:
            return True
    return False


def format_restored_queue_blocked_notice(
    restored: int,
    *,
    tracker_running: bool,
    incomplete_entries: list[Any] | None = None,
    start_already_set: bool = False,
) -> str:
    """User-facing notice when hydrate restored jobs but auto-start was skipped."""
    base = f"已从本地恢复分析队列 {restored} 只"
    if tracker_running:
        return base + "（当前仍有分析在跑，恢复的队列将排队等候）"
    if has_blocking_incomplete_run(incomplete_entries):
        return (
            base
            + "（存在进行中/已暂停任务，不会自动开跑以免重叠；确认空闲后点侧栏「继续队列」）"
        )
    if start_already_set:
        return base + "（已有待启动任务，未重复自动开跑）"
    return base + "（队列为空或无法自动开跑；可点侧栏「继续队列」）"


def maybe_autostart_restored_queue(
    session: MutableMapping[str, Any],
    *,
    restored: int,
    tracker_running: bool,
    incomplete_entries: list[Any] | None = None,
    store: AnalysisQueueStore | None = None,
    before_commit: Callable[[AnalysisJob], None] | None = None,
) -> Optional[AnalysisJob]:
    """Pop and stage the next job when a refresh restored a queue while idle.

    ``before_commit`` runs after the idle gate passes but *before* the queue pop
    is persisted — use it to write a running incomplete marker so another Web
    session cannot also autostart.

    Returns the started job, or ``None`` when auto-start is skipped (overlap risk
    or nothing to restore). Callers should fall back to
    :func:`format_restored_queue_blocked_notice`.
    """
    if restored <= 0 or tracker_running:
        return None
    if has_blocking_incomplete_run(incomplete_entries):
        return None
    if session.get("start_analysis"):
        return None
    queue = _coerce_list(session)
    if not queue:
        return None
    head = queue[0]
    if before_commit is not None:
        before_commit(head)
    head = advance_queue(session, store=store)
    if head is None:
        return None
    mark_serial_queue_session(session, True)
    session["start_analysis"] = head.to_start_request()
    session["viewing_history"] = None
    session["viewing_watchlist"] = False
    session["queue_advance_notice"] = (
        f"已从本地恢复分析队列 {restored} 只，空闲故自动开始 {head.ticker}"
    )
    return head


def prepend_job(
    session: MutableMapping[str, Any],
    job: AnalysisJob,
    *,
    store: AnalysisQueueStore | None = None,
) -> None:
    """Put a job back at the front of the queue (e.g. after a failed handoff start)."""
    queue = _coerce_list(session)
    ident = job.identity()
    queue = [j for j in queue if j.identity() != ident]
    queue.insert(0, job)
    session[QUEUE_SESSION_KEY] = queue
    _persist(session, store)


def remove_job_identity(
    session: MutableMapping[str, Any],
    identity: tuple[str, str, str],
    *,
    store: AnalysisQueueStore | None = None,
) -> int:
    """Drop queued jobs matching ``(market, ticker, trade_date)``. Returns removed count."""
    queue = _coerce_list(session)
    kept = [j for j in queue if j.identity() != identity]
    removed = len(queue) - len(kept)
    if removed:
        session[QUEUE_SESSION_KEY] = kept
        _persist(session, store)
    return removed


def _clear_incomplete_for_queued(
    requested: list[AnalysisJob],
    queued_idents: set[tuple[str, str, str]],
) -> None:
    """Drop incomplete rows for request identities that are waiting in the queue."""
    if not requested or not queued_idents:
        return
    from web.history import clear_incomplete_task

    seen: set[tuple[str, str, str]] = set()
    for job in requested:
        ident = job.identity()
        if ident not in queued_idents or ident in seen:
            continue
        seen.add(ident)
        clear_incomplete_task(job.ticker, job.trade_date)


def append_jobs(
    session: MutableMapping[str, Any],
    jobs: list[AnalysisJob],
    *,
    exclude: Optional[set[tuple[str, str, str]]] = None,
    store: AnalysisQueueStore | None = None,
) -> int:
    """Append jobs to the session queue; skip duplicates already queued. Returns added count.

    ``exclude`` identities (e.g. the currently running job) are also treated as duplicates.
    Request identities that remain in the waiting queue clear matching incomplete tasks.
    """
    queue = _coerce_list(session)
    existing = {j.identity() for j in queue}
    if exclude:
        existing |= exclude
    added = 0
    for job in jobs:
        if job.identity() in existing:
            continue
        queue.append(job)
        existing.add(job.identity())
        added += 1
    session[QUEUE_SESSION_KEY] = queue
    if added:
        mark_serial_queue_session(session, True)
    _persist(session, store)
    _clear_incomplete_for_queued(jobs, {j.identity() for j in queue})
    return added


def advance_queue(
    session: MutableMapping[str, Any],
    *,
    store: AnalysisQueueStore | None = None,
) -> Optional[AnalysisJob]:
    """Pop and return the next job, or None if empty."""
    queue = _coerce_list(session)
    if not queue:
        return None
    nxt = queue.pop(0)
    session[QUEUE_SESSION_KEY] = queue
    _persist(session, store)
    return nxt


def clear_queue(
    session: MutableMapping[str, Any],
    *,
    store: AnalysisQueueStore | None = None,
) -> None:
    session[QUEUE_SESSION_KEY] = []
    mark_serial_queue_session(session, False)
    _persist(session, store)


def queue_snapshot(session: MutableMapping[str, Any]) -> list[AnalysisJob]:
    return list(_coerce_list(session))


def take_next_job(
    session: MutableMapping[str, Any],
    *,
    finished_ticker: str,
    error: str | None = None,
    store: AnalysisQueueStore | None = None,
) -> Optional[AnalysisJob]:
    """Legacy single-next-job interface for backward compat.

    In parallel mode this tries to fill empty slots via the registered hook.
    When the hook is not set (e.g. unit tests), falls back to plain dequeue.
    Returns the *first* started job (or None).
    """
    fn = _make_begin_fn(store)
    if fn is not None:
        from web.parallel_runs import pop_and_start_queued_jobs

        started = pop_and_start_queued_jobs(session, begin_analysis_fn=fn)
        if started:
            remaining = len(queue_snapshot(session))
            suffix = f"（队列剩余 {remaining}）" if remaining else ""
            first = started[0]
            if error:
                session["queue_advance_notice"] = (
                    f"{finished_ticker} 失败（{error}），已跳过{'，新启动 ' + ', '.join(t.ticker for t in started) + suffix}"
                )
            else:
                session["queue_advance_notice"] = (
                    f"{finished_ticker} 已完成，新启动 "
                    + ', '.join(t.ticker for t in started)
                    + suffix
                )
            session["viewing_history"] = None
            session["viewing_watchlist"] = False
            return first

    # Fallback: plain dequeue (works without hook, e.g. unit tests).
    next_job = advance_queue(session, store=store)
    if next_job is None:
        if is_serial_queue_session(session):
            mark_serial_queue_session(session, False)
            session["viewing_history"] = None
            session["viewing_watchlist"] = False
            if error:
                session["queue_advance_notice"] = (
                    f"{finished_ticker} 失败（{error}）；分析队列已全部结束"
                )
            else:
                session["queue_advance_notice"] = (
                    f"{finished_ticker} 已完成；分析队列已全部结束"
                )
        return None
    remaining = len(queue_snapshot(session))
    suffix = f"（队列剩余 {remaining}）" if remaining else ""
    if error:
        session["queue_advance_notice"] = (
            f"{finished_ticker} 失败（{error}），已跳过并开始下一只 "
            f"{next_job.ticker}{suffix}"
        )
    else:
        session["queue_advance_notice"] = (
            f"{finished_ticker} 已完成，开始下一只 {next_job.ticker}{suffix}"
        )
    session["viewing_history"] = None
    session["viewing_watchlist"] = False
    return next_job


def _make_begin_fn(store: AnalysisQueueStore | None):
    """Return a callable(AnalysisJob) -> ProgressTracker when the hook is registered."""
    return _begin_analysis_hook


_begin_analysis_hook: Callable[[AnalysisJob], ProgressTracker] | None = None


def set_begin_analysis_hook(fn: Callable[[AnalysisJob], ProgressTracker]) -> None:
    """Set the hook that starts one analysis run (called by ``web.app`` on import)."""
    global _begin_analysis_hook
    _begin_analysis_hook = fn
