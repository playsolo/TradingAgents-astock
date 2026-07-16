"""本机站内事件中心：分析完成 / 失败 / 警告 / 观察池告警。

持久化到 ``~/.tradingagents/inbox.json``，与 incomplete_tasks / watchlist 同级。
供 Web 侧栏未读角标与列表消费；观察调度与分析线程均可写入。
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

_INBOX_FILE = Path.home() / ".tradingagents" / "inbox.json"
_LOCK = threading.Lock()

MAX_EVENTS = 100

KIND_ANALYSIS_COMPLETE = "analysis.complete"
KIND_ANALYSIS_FAILED = "analysis.failed"
KIND_ANALYSIS_WARNING = "analysis.warning"
KIND_ANALYSIS_SKIPPED = "analysis.skipped"
KIND_WATCH_ALERT = "watch.alert"

_VALID_SEVERITIES = frozenset({"info", "warning", "error"})
_VALID_LINK_VIEWS = frozenset({"home", "history", "watch"})

_SIGNAL_CN = {
    "Buy": "买入",
    "Sell": "卖出",
    "Hold": "持有",
}


def _load() -> list[dict[str, Any]]:
    if not _INBOX_FILE.exists():
        return []
    try:
        with open(_INBOX_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, dict):
        events = data.get("events") or []
    elif isinstance(data, list):
        events = data
    else:
        return []
    return [e for e in events if isinstance(e, dict) and e.get("id")]


def _save(events: list[dict[str, Any]]) -> None:
    parent = _INBOX_FILE.parent
    parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "events": events}
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=parent,
        prefix=f"{_INBOX_FILE.stem}.",
        suffix=".tmp",
        delete=False,
    ) as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        tmp = Path(f.name)
    tmp.replace(_INBOX_FILE)


def emit(
    kind: str,
    title: str,
    *,
    severity: str = "info",
    detail: str = "",
    ticker: str | None = None,
    trade_date: str | None = None,
    link_view: str = "home",
    dedupe_key: str | None = None,
) -> dict[str, Any]:
    """Append an inbox event (newest first). Returns the stored event dict."""
    sev = severity if severity in _VALID_SEVERITIES else "info"
    view = link_view if link_view in _VALID_LINK_VIEWS else "home"
    title = (title or "").strip() or kind
    detail = (detail or "").strip()
    ticker_n = (ticker or "").strip().upper() or None
    date_n = (trade_date or "").strip() or None
    key = (dedupe_key or "").strip() or None

    with _LOCK:
        events = _load()
        if key:
            for existing in events:
                if (
                    not existing.get("read")
                    and existing.get("dedupe_key") == key
                ):
                    return existing

        event: dict[str, Any] = {
            "id": uuid.uuid4().hex,
            "kind": kind,
            "severity": sev,
            "title": title,
            "detail": detail,
            "ticker": ticker_n,
            "trade_date": date_n,
            "link_view": view,
            "dedupe_key": key,
            "created_at": time.time(),
            "read": False,
        }
        events.insert(0, event)
        del events[MAX_EVENTS:]
        _save(events)
        return event


def list_events(
    *,
    limit: int = 50,
    unread_only: bool = False,
) -> list[dict[str, Any]]:
    with _LOCK:
        events = _load()
    if unread_only:
        events = [e for e in events if not e.get("read")]
    if limit > 0:
        events = events[:limit]
    return events


def unread_count() -> int:
    with _LOCK:
        return sum(1 for e in _load() if not e.get("read"))


def mark_read(event_id: str) -> bool:
    eid = (event_id or "").strip()
    if not eid:
        return False
    with _LOCK:
        events = _load()
        found = False
        for event in events:
            if event.get("id") == eid and not event.get("read"):
                event["read"] = True
                found = True
                break
        if found:
            _save(events)
        return found


def mark_all_read() -> int:
    with _LOCK:
        events = _load()
        n = 0
        for event in events:
            if not event.get("read"):
                event["read"] = True
                n += 1
        if n:
            _save(events)
        return n


def emit_analysis_complete(
    ticker: str,
    trade_date: str,
    signal: str,
    *,
    warnings: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Emit completion (+ optional data-gap warning). Returns emitted events."""
    signal = (signal or "N/A").strip() or "N/A"
    signal_cn = _SIGNAL_CN.get(signal, signal)
    emitted: list[dict[str, Any]] = []
    emitted.append(
        emit(
            KIND_ANALYSIS_COMPLETE,
            f"{ticker} 分析完成",
            severity="info",
            detail=f"信号：{signal_cn} · {trade_date}",
            ticker=ticker,
            trade_date=trade_date,
            link_view="history",
        )
    )
    gaps = [w for w in (warnings or []) if w]
    if gaps:
        preview = "、".join(gaps[:4])
        if len(gaps) > 4:
            preview += f" 等{len(gaps)}项"
        emitted.insert(
            0,
            emit(
                KIND_ANALYSIS_WARNING,
                f"{ticker} 报告存在数据缺失",
                severity="warning",
                detail=preview,
                ticker=ticker,
                trade_date=trade_date,
                link_view="history",
            ),
        )
    return emitted


def emit_analysis_skipped(
    ticker: str,
    trade_date: str,
    *,
    reason: str = "",
    anchor_date: str = "",
    stance: str = "",
) -> dict[str, Any]:
    """Scan narrow-path: deep analysis skipped, reuse calibration anchor."""
    parts = ["沿用校准锚点，跳过深分析"]
    if anchor_date:
        parts.append(f"锚点日 {anchor_date}")
    if stance:
        parts.append(f"立场 {stance}")
    if reason:
        parts.append(reason)
    return emit(
        KIND_ANALYSIS_SKIPPED,
        f"{ticker} 扫描跳过深分析",
        severity="info",
        detail=" · ".join(parts),
        ticker=ticker,
        trade_date=trade_date,
        link_view="history",
        dedupe_key=f"skip:{ticker}:{trade_date}",
    )


def emit_analysis_failed(
    ticker: str,
    trade_date: str,
    error: str,
) -> dict[str, Any]:
    err = (error or "未知错误").strip()
    if len(err) > 200:
        err = err[:197] + "..."
    return emit(
        KIND_ANALYSIS_FAILED,
        f"{ticker} 分析失败",
        severity="error",
        detail=err,
        ticker=ticker,
        trade_date=trade_date,
        link_view="home",
    )


def emit_watch_alerts(ticker: str, alerts: list[Any]) -> list[dict[str, Any]]:
    """Mirror watchlist Alert objects into the inbox (one event each)."""
    emitted: list[dict[str, Any]] = []
    t = (ticker or "").strip().upper()
    if not t or not alerts:
        return emitted
    for alert in alerts:
        if isinstance(alert, dict):
            kind = str(alert.get("kind") or "alert")
            title = str(alert.get("title") or kind)
            detail = str(alert.get("detail") or "")
            observed_at = str(alert.get("observed_at") or "")
        else:
            kind = str(getattr(alert, "kind", None) or "alert")
            title = str(getattr(alert, "title", None) or kind)
            detail = str(getattr(alert, "detail", None) or "")
            observed_at = str(getattr(alert, "observed_at", None) or "")
        dedupe = f"watch:{t}:{kind}:{observed_at}" if observed_at else None
        emitted.append(
            emit(
                KIND_WATCH_ALERT,
                f"{t} {title}",
                severity="warning",
                detail=detail,
                ticker=t,
                link_view="watch",
                dedupe_key=dedupe,
            )
        )
    return emitted
