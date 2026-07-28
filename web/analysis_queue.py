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
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
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

# Queue JSON schema: v1 = waiting jobs only; v2 adds in-flight leases.
_QUEUE_VERSION = 2


@dataclass(frozen=True)
class AnalysisJob:
    ticker: str
    trade_date: str
    market: str  # "CN" | "US"
    fresh: bool = True
    resume_count: int = 0
    # Mode routing: auto (router decides) | full_reeval | pseudo_incremental
    force_full_reeval: bool = False
    analysis_mode: str = "auto"
    # Origin: manual sidebar vs strategy scan (scan may skip deep analysis).
    source: str = "manual"

    def to_start_request(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "trade_date": self.trade_date,
            "fresh": self.fresh,
            "market": self.market,
            "resume_count": self.resume_count,
            "force_full_reeval": self.force_full_reeval,
            "analysis_mode": self.analysis_mode,
            "source": self.source,
        }

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def identity(self) -> tuple[str, str, str]:
        return (self.market, self.ticker, self.trade_date)

    def with_resume(self, *, resume_count: int | None = None, fresh: bool = False) -> "AnalysisJob":
        """Return a copy suitable for auto-continue / graceful requeue."""
        count = self.resume_count if resume_count is None else int(resume_count)
        return replace(self, fresh=fresh, resume_count=max(0, count))

    @classmethod
    def from_mapping(cls, raw: Any) -> "AnalysisJob":
        if isinstance(raw, cls):
            return raw

        def _resume_count(obj: Any) -> int:
            try:
                if isinstance(obj, (dict, MutableMapping)):
                    return max(0, int(obj.get("resume_count", 0) or 0))
                return max(0, int(getattr(obj, "resume_count", 0) or 0))
            except (TypeError, ValueError):
                return 0

        def _mode_fields(obj: Any) -> tuple[bool, str, str]:
            if isinstance(obj, (dict, MutableMapping)):
                force = bool(obj.get("force_full_reeval", False))
                mode = str(obj.get("analysis_mode") or "auto").strip().lower() or "auto"
                source = str(obj.get("source") or "manual").strip().lower() or "manual"
            else:
                force = bool(getattr(obj, "force_full_reeval", False))
                mode = str(getattr(obj, "analysis_mode", None) or "auto").strip().lower() or "auto"
                source = str(getattr(obj, "source", None) or "manual").strip().lower() or "manual"
            if mode not in {"auto", "full_reeval", "pseudo_incremental"}:
                mode = "auto"
            if source not in {"manual", "scan", "filing"}:
                source = "manual"
            return force, mode, source

        # Streamlit hot-reload replaces this class; session may still hold instances
        # from the previous class object (isinstance fails, but attributes remain).
        if not isinstance(raw, (dict, MutableMapping)) and hasattr(raw, "ticker"):
            force, mode, source = _mode_fields(raw)
            return cls(
                ticker=str(getattr(raw, "ticker")),
                trade_date=str(getattr(raw, "trade_date", "")),
                market=str(getattr(raw, "market", None) or "CN"),
                fresh=bool(getattr(raw, "fresh", True)),
                resume_count=_resume_count(raw),
                force_full_reeval=force,
                analysis_mode=mode,
                source=source,
            )
        force, mode, source = _mode_fields(raw)
        return cls(
            ticker=str(raw["ticker"]),
            trade_date=str(raw["trade_date"]),
            market=str(raw.get("market") or "CN"),
            fresh=bool(raw.get("fresh", True)),
            resume_count=_resume_count(raw),
            force_full_reeval=force,
            analysis_mode=mode,
            source=source,
        )


@dataclass(frozen=True)
class QueueLease:
    """In-flight claim so a crashed worker can reclaim unfinished jobs."""

    job: AnalysisJob
    lease_id: str
    claimed_at: float
    heartbeat_at: float
    owner_pid: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "job": self.job.to_dict(),
            "lease_id": self.lease_id,
            "claimed_at": self.claimed_at,
            "heartbeat_at": self.heartbeat_at,
            "owner_pid": self.owner_pid,
        }

    @classmethod
    def from_mapping(cls, raw: Any) -> "QueueLease":
        if isinstance(raw, cls):
            return raw
        job = AnalysisJob.from_mapping(raw["job"] if isinstance(raw, dict) else raw.job)
        return cls(
            job=job,
            lease_id=str(raw["lease_id"] if isinstance(raw, dict) else raw.lease_id),
            claimed_at=float(
                raw["claimed_at"] if isinstance(raw, dict) else raw.claimed_at
            ),
            heartbeat_at=float(
                raw["heartbeat_at"] if isinstance(raw, dict) else raw.heartbeat_at
            ),
            owner_pid=int(raw["owner_pid"] if isinstance(raw, dict) else raw.owner_pid),
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

    def _parse_jobs(self, data: dict[str, Any]) -> list[AnalysisJob]:
        jobs: list[AnalysisJob] = []
        for raw in data.get("jobs") or []:
            if not isinstance(raw, dict):
                continue
            try:
                jobs.append(AnalysisJob.from_mapping(raw))
            except (KeyError, TypeError, ValueError):
                continue
        return jobs

    def _parse_leases(self, data: dict[str, Any]) -> list[QueueLease]:
        leases: list[QueueLease] = []
        for raw in data.get("leases") or []:
            if not isinstance(raw, dict):
                continue
            try:
                leases.append(QueueLease.from_mapping(raw))
            except (KeyError, TypeError, ValueError):
                continue
        return leases

    def _read_payload(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": _QUEUE_VERSION, "jobs": [], "leases": []}
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError, TypeError):
            return {"version": _QUEUE_VERSION, "jobs": [], "leases": []}
        if not isinstance(data, dict):
            return {"version": _QUEUE_VERSION, "jobs": [], "leases": []}
        return data

    def _load_state(self) -> tuple[list[AnalysisJob], list[QueueLease]]:
        """Load waiting jobs + in-flight leases. Caller must hold the store lock."""
        data = self._read_payload()
        return self._parse_jobs(data), self._parse_leases(data)

    def _save_state(self, jobs: list[AnalysisJob], leases: list[QueueLease]) -> None:
        """Persist waiting jobs + leases. Caller must hold the store lock."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": _QUEUE_VERSION,
            "jobs": [job.to_dict() for job in jobs],
            "leases": [lease.to_dict() for lease in leases],
        }
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        tmp.replace(self.path)

    def load(self) -> list[AnalysisJob]:
        with _STORE_LOCK:
            jobs, _leases = self._load_state()
            return jobs

    def load_leases(self) -> list[QueueLease]:
        with _STORE_LOCK:
            _jobs, leases = self._load_state()
            return leases

    def save(self, jobs: list[AnalysisJob]) -> None:
        """Replace waiting jobs; preserve any existing leases."""
        with _STORE_LOCK:
            _old_jobs, leases = self._load_state()
            self._save_state(jobs, leases)

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

    def claim_next(self, *, owner_pid: int | None = None) -> Optional[AnalysisJob]:
        """Atomically move the head waiting job into a lease and return it.

        Returns None when the waiting queue is empty. The job remains tracked
        under ``leases`` until :meth:`complete` or reclaim/release.
        """
        return self.claim_next_by_market(market=None, owner_pid=owner_pid)

    def claim_next_by_market(
        self,
        market: str | None = None,
        *,
        owner_pid: int | None = None,
    ) -> Optional[AnalysisJob]:
        """Atomically claim the first waiting job for *market* (or any market
        when *market* is ``None``).  Jobs for other markets are preserved in
        FIFO order.

        Returns None when no unclaimed job of the requested market exists.
        """
        pid = os.getpid() if owner_pid is None else int(owner_pid)
        with self.exclusive():
            jobs, leases = self._load_state()
            if not jobs:
                return None
            leased_idents = {lease.job.identity() for lease in leases}
            target_idx: int | None = None
            for i, job in enumerate(jobs):
                if job.identity() in leased_idents:
                    continue
                if market is None or (job.market or "CN").upper() == market.upper():
                    target_idx = i
                    break
            if target_idx is None:
                return None
            job = jobs.pop(target_idx)
            now = time.time()
            leases.append(
                QueueLease(
                    job=job,
                    lease_id=uuid.uuid4().hex,
                    claimed_at=now,
                    heartbeat_at=now,
                    owner_pid=pid,
                )
            )
            self._save_state(jobs, leases)
            return job

    def heartbeat(self, identity: tuple[str, str, str]) -> bool:
        """Refresh lease heartbeat. Returns False if no matching lease."""
        with self.exclusive():
            jobs, leases = self._load_state()
            updated: list[QueueLease] = []
            found = False
            now = time.time()
            for lease in leases:
                if lease.job.identity() == identity:
                    updated.append(
                        QueueLease(
                            job=lease.job,
                            lease_id=lease.lease_id,
                            claimed_at=lease.claimed_at,
                            heartbeat_at=now,
                            owner_pid=lease.owner_pid,
                        )
                    )
                    found = True
                else:
                    updated.append(lease)
            if found:
                self._save_state(jobs, updated)
            return found

    def complete(self, identity: tuple[str, str, str]) -> bool:
        """Drop a finished lease. Returns True when a lease was removed."""
        with self.exclusive():
            jobs, leases = self._load_state()
            kept = [lease for lease in leases if lease.job.identity() != identity]
            if len(kept) == len(leases):
                return False
            self._save_state(jobs, kept)
            return True

    def release_to_queue(
        self,
        identities: list[tuple[str, str, str]],
        *,
        bump_resume: bool = False,
        max_auto_resume: int = 2,
    ) -> tuple[list[AnalysisJob], list[AnalysisJob]]:
        """Move leased jobs back to the front of the waiting queue.

        ``bump_resume=False`` is for graceful stop (B): preserve resume_count.
        ``bump_resume=True`` is for crash reclaim (C): increment resume_count;
        jobs already at ``max_auto_resume`` are returned as exhausted and not
        requeued.

        Returns ``(requeued, exhausted)``.
        """
        wanted = set(identities)
        if not wanted:
            return [], []
        with self.exclusive():
            jobs, leases = self._load_state()
            requeued: list[AnalysisJob] = []
            exhausted: list[AnalysisJob] = []
            remaining_leases: list[QueueLease] = []
            for lease in leases:
                ident = lease.job.identity()
                if ident not in wanted:
                    remaining_leases.append(lease)
                    continue
                if bump_resume:
                    next_count = lease.job.resume_count + 1
                    if next_count > max_auto_resume:
                        exhausted.append(lease.job)
                        continue
                    job = lease.job.with_resume(resume_count=next_count, fresh=False)
                else:
                    job = lease.job.with_resume(
                        resume_count=lease.job.resume_count, fresh=False
                    )
                requeued.append(job)
            if not requeued and not exhausted:
                return [], []
            # Prepend requeued jobs; drop duplicates already waiting.
            waiting_idents = {j.identity() for j in jobs}
            prefix: list[AnalysisJob] = []
            for job in requeued:
                if job.identity() in waiting_idents:
                    continue
                prefix.append(job)
                waiting_idents.add(job.identity())
            self._save_state(prefix + jobs, remaining_leases)
        if prefix:
            _clear_incomplete_for_queued(prefix, {j.identity() for j in prefix})
        return requeued, exhausted

    def reclaim_expired(
        self,
        *,
        ttl_seconds: float,
        max_auto_resume: int = 2,
        now: float | None = None,
        owner_pid: int | None = None,
    ) -> tuple[list[AnalysisJob], list[AnalysisJob]]:
        """Reclaim stale leases (or all leases when ``ttl_seconds<=0`` on startup).

        Leases owned by ``owner_pid`` (current process) are never reclaimed while
        their heartbeat is still fresh — protects against double-run in the same
        worker. On startup pass ``ttl_seconds=0`` to reclaim every lease from a
        previous process.
        """
        ts = time.time() if now is None else float(now)
        pid = os.getpid() if owner_pid is None else int(owner_pid)
        with self.exclusive():
            jobs, leases = self._load_state()
            if not leases:
                return [], []
            stale_idents: list[tuple[str, str, str]] = []
            for lease in leases:
                age = ts - float(lease.heartbeat_at)
                if ttl_seconds <= 0:
                    # Startup: reclaim every leftover lease from a prior process.
                    stale_idents.append(lease.job.identity())
                    continue
                if lease.owner_pid == pid and age < ttl_seconds:
                    continue
                if age >= ttl_seconds:
                    stale_idents.append(lease.job.identity())
            if not stale_idents:
                return [], []

        return self.release_to_queue(
            stale_idents,
            bump_resume=True,
            max_auto_resume=max_auto_resume,
        )

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
        show both「队列中」and「出错/未完成」. Leased identities also count as
        duplicates so Web cannot double-enqueue an in-flight ticker.
        """
        with self.exclusive():
            jobs, leases = self._load_state()
            existing = {j.identity() for j in jobs}
            existing |= {lease.job.identity() for lease in leases}
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
                self._save_state(jobs, leases)
            queued = {j.identity() for j in jobs}
        _clear_incomplete_for_queued(new_jobs, queued)
        return added

    def clear_atomic(self) -> None:
        """Atomically empty the on-disk waiting queue (leases preserved)."""
        with self.exclusive():
            _jobs, leases = self._load_state()
            self._save_state([], leases)

    def remove_job_identity(self, identity: tuple[str, str, str]) -> int:
        """Atomically remove a specific job from the waiting queue by identity.

        ``identity`` is ``(market, ticker, trade_date)`` (matches
        ``AnalysisJob.identity()``). Leases are untouched — remove only
        affects jobs that haven't been claimed yet.

        Returns the number of jobs removed (0 or 1).
        """
        with self.exclusive():
            jobs, leases = self._load_state()
            kept = [j for j in jobs if j.identity() != identity]
            removed = len(jobs) - len(kept)
            if removed:
                self._save_state(kept, leases)
            return removed


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
            from tradingagents.watchlist.calendar import effective_trade_date_for_market

            job = AnalysisJob(
                ticker=code,
                trade_date=effective_trade_date_for_market(market, trade_date),
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


def _infer_token_market(token: str) -> str:
    """Return ``"CN"`` for 6-digit codes and Chinese names, ``"US"`` otherwise.

    Chinese company names must be CN even when the name→code cache misses;
    otherwise they are mis-routed to the US bridge and crash on
    ``safe_ticker_component`` (ASCII-only path components).
    """
    code = (token or "").strip()
    if code.isdigit() and len(code) == 6:
        return "CN"
    # Any CJK character → A-share name (e.g. 世纪华通 / 三七互娱).
    if any("\u4e00" <= ch <= "\u9fff" for ch in code):
        return "CN"
    from web.stock_display import lookup_code_by_cached_name
    if lookup_code_by_cached_name(code):
        return "CN"
    return "US"


def resolve_ticker_batch_mixed(
    raw_tickers: list[str],
    *,
    trade_date: str,
    resolve_cn: Callable[[str], str],
    force_full_reeval: bool = False,
    analysis_mode: str = "auto",
) -> tuple[list[AnalysisJob], list[str]]:
    """Resolve a batch of tickers that may mix CN and US markets.

    Each token is auto-classified by ``_infer_token_market``, then resolved
    with the appropriate market resolver. Returns jobs with per-token market
    tags alongside error messages for invalid entries.
    """
    jobs: list[AnalysisJob] = []
    errors: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    mode = (analysis_mode or "auto").strip().lower() or "auto"
    if mode not in {"auto", "full_reeval", "pseudo_incremental"}:
        mode = "auto"

    for raw in raw_tickers:
        token = (raw or "").strip()
        if not token:
            continue
        try:
            market = _infer_token_market(token)
            if market == "US":
                code = token.upper()
                if any(ch.isspace() for ch in code):
                    raise ValueError("美股代码不能包含空格")
            else:
                code = resolve_cn(token)
            from tradingagents.watchlist.calendar import effective_trade_date_for_market

            job = AnalysisJob(
                ticker=code,
                trade_date=effective_trade_date_for_market(market, trade_date),
                market=market,
                fresh=True,
                force_full_reeval=bool(force_full_reeval),
                analysis_mode=mode,
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
    tags = [market_tag]
    if getattr(job, "source", "manual") == "scan":
        tags.append("扫描")
    if getattr(job, "force_full_reeval", False):
        tags.append("强制全量")
    return f"{index}. {format_list_ticker_label(job.ticker, job.trade_date, *tags)}"


def partition_scan_jobs_for_enqueue(
    jobs: list[AnalysisJob],
    *,
    as_of: str | None = None,
) -> tuple[list[AnalysisJob], list[tuple[str, str]]]:
    """Split scan jobs into deep-analysis vs skip-reuse (narrow_ok).

    Returns ``(to_enqueue, skipped)`` where skipped is ``(ticker, reason)``.
    """
    from tradingagents.analysis.mode_router import (
        SOURCE_SCAN,
        resolve_analysis_mode,
    )

    keep: list[AnalysisJob] = []
    skipped: list[tuple[str, str]] = []
    for job in jobs:
        decision = resolve_analysis_mode(
            ticker=job.ticker,
            trade_date=job.trade_date,
            market=job.market,
            force_full_reeval=bool(job.force_full_reeval),
            analysis_mode=str(job.analysis_mode or "auto"),
            source=SOURCE_SCAN,
            fresh=bool(job.fresh),
            as_of=as_of or job.trade_date,
        )
        if decision.skips_deep_analysis:
            skipped.append((job.ticker, decision.reason))
        else:
            # Persist scan source on kept jobs.
            if getattr(job, "source", "manual") != "scan":
                job = replace(job, source="scan")
            keep.append(job)
    return keep, skipped


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
