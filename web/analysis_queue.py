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
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, MutableMapping, Optional

QUEUE_SESSION_KEY = "analysis_queue"
SERIAL_QUEUE_SESSION_KEY = "serial_queue_session"
_HYDRATED_FLAG = "_analysis_queue_hydrated"

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
    def from_mapping(cls, raw: MutableMapping[str, Any] | AnalysisJob) -> "AnalysisJob":
        if isinstance(raw, AnalysisJob):
            return raw
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


def append_jobs(
    session: MutableMapping[str, Any],
    jobs: list[AnalysisJob],
    *,
    exclude: Optional[set[tuple[str, str, str]]] = None,
    store: AnalysisQueueStore | None = None,
) -> int:
    """Append jobs to the session queue; skip duplicates already queued. Returns added count.

    ``exclude`` identities (e.g. the currently running job) are also treated as duplicates.
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
    """Pop the next queued job and record a user-facing advance notice, if any.

    When a serial queue session drains (no next job), clear watch/history views so
    the finished report is visible, and leave a completion notice.
    """
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
