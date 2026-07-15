"""Manage completed and incomplete analysis history."""

from __future__ import annotations

import json
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from tradingagents.default_config import DEFAULT_CONFIG


_INCOMPLETE_TASKS_FILE = Path.home() / ".tradingagents" / "incomplete_tasks.json"
_INCOMPLETE_TASKS_LOCK = threading.Lock()


def _results_dir() -> Path:
    return Path.home() / ".tradingagents" / "logs"


def _lazy_signal(path: str) -> str:
    """Read a small portion of the log file to extract signal without loading full JSON."""
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
        return extract_signal(state)
    except Exception:
        return "N/A"


def _signal_group_key(signal: str) -> int:
    """Sort key for signal groups: Buy first, then Sell, then Hold, then N/A."""
    s = signal.upper() if signal else ""
    if "BUY" in s:
        return 0
    if "SELL" in s:
        return 1
    if "HOLD" in s:
        return 2
    return 3


def group_history_by_signal(
    entries: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Group history entries by signal (Buy / Sell / Hold / N/A)."""
    groups: dict[str, list[dict[str, Any]]] = {
        "Buy": [],
        "Sell": [],
        "Hold": [],
        "N/A": [],
    }
    for entry in entries:
        sig = entry.get("signal", "N/A")
        if sig in groups:
            groups[sig].append(entry)
        else:
            groups["N/A"].append(entry)
    return groups


def signal_count_label(group: dict[str, list[dict[str, Any]]], total: int) -> str:
    """Build a compact summary line, e.g. '共 42 条 · 买入12 持有18 卖出8'."""
    parts = [f"共 {total} 条"]
    for sig, display in [("Buy", "买入"), ("Sell", "卖出"), ("Hold", "持有")]:
        n = len(group.get(sig, []))
        if n:
            parts.append(f"{display}{n}")
    return "  ·  ".join(parts)


def get_history() -> list[dict[str, Any]]:
    """Scan saved analysis logs and return a sorted list (newest first).

    Sorted by analysis completion time (log file mtime), not trade date.
    Each entry: {"ticker": "300750", "date": "2026-05-12", "path": "/abs/path/...json",
                  "signal": "Buy" | "Sell" | "Hold" | "N/A"}
    """
    root = _results_dir()
    if not root.exists():
        return []

    entries: list[tuple[float, dict[str, Any]]] = []
    for log_file in root.rglob("full_states_log_*.json"):
        match = re.search(r"full_states_log_(\d{4}-\d{2}-\d{2})\.json$", log_file.name)
        if not match:
            continue
        try:
            mtime = log_file.stat().st_mtime
        except OSError:
            continue
        date = match.group(1)
        ticker = log_file.parent.parent.name
        signal = _lazy_signal(str(log_file))
        entries.append(
            (
                mtime,
                {
                    "ticker": ticker,
                    "date": date,
                    "path": str(log_file),
                    "signal": signal,
                },
            )
        )

    entries.sort(key=lambda item: item[0], reverse=True)
    return [entry for _, entry in entries]


def _completed_key(ticker: str, trade_date: str) -> tuple[str, str]:
    return ticker.upper(), trade_date


def _load_incomplete_index() -> list[dict[str, Any]]:
    if not _INCOMPLETE_TASKS_FILE.exists():
        return []

    try:
        with open(_INCOMPLETE_TASKS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []

    if not isinstance(data, list):
        return []

    entries: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        ticker = str(item.get("ticker", "")).strip().upper()
        trade_date = str(item.get("trade_date", "")).strip()
        if not ticker or not re.match(r"^\d{4}-\d{2}-\d{2}$", trade_date):
            continue
        item["ticker"] = ticker
        item["trade_date"] = trade_date
        entries.append(item)
    return entries


def _save_incomplete_index(entries: list[dict[str, Any]]) -> None:
    parent = _INCOMPLETE_TASKS_FILE.parent
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=parent,
        prefix=f"{_INCOMPLETE_TASKS_FILE.stem}.",
        suffix=".tmp",
        delete=False,
    ) as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
        tmp = Path(f.name)
    tmp.replace(_INCOMPLETE_TASKS_FILE)


def _checkpoint_step(ticker: str, trade_date: str) -> int | None:
    try:
        from tradingagents.graph.checkpointer import checkpoint_step

        return checkpoint_step(DEFAULT_CONFIG["data_cache_dir"], ticker, trade_date)
    except Exception:
        return None


def record_incomplete_task(
    ticker: str,
    trade_date: str,
    *,
    status: str,
    error: str | None = None,
    completed_stages: list[str] | None = None,
) -> None:
    """Upsert a resumable task entry."""
    ticker = ticker.strip().upper()
    trade_date = trade_date.strip()
    if not ticker or not trade_date:
        return

    with _INCOMPLETE_TASKS_LOCK:
        entries = [
            entry
            for entry in _load_incomplete_index()
            if _completed_key(entry["ticker"], entry["trade_date"])
            != _completed_key(ticker, trade_date)
        ]
        now = time.time()
        entries.append(
            {
                "ticker": ticker,
                "trade_date": trade_date,
                "status": status,
                "error": error or "",
                "completed_stages": completed_stages or [],
                "updated_at": now,
            }
        )
        entries.sort(key=lambda e: float(e.get("updated_at", 0)), reverse=True)
        _save_incomplete_index(entries)


def clear_incomplete_task(ticker: str, trade_date: str) -> None:
    """Remove an incomplete task once it completes successfully."""
    ticker = ticker.strip().upper()
    trade_date = trade_date.strip()
    with _INCOMPLETE_TASKS_LOCK:
        entries = [
            entry
            for entry in _load_incomplete_index()
            if _completed_key(entry["ticker"], entry["trade_date"])
            != _completed_key(ticker, trade_date)
        ]
        _save_incomplete_index(entries)


def get_incomplete_history() -> list[dict[str, Any]]:
    """Return unfinished tasks that can be resumed from their checkpoint.

    Entries stay until ``clear_incomplete_task`` (successful finish / stop).
    Report file mtime is intentionally ignored — section repair and same-day
    re-runs both rewrite ``full_states_log_*.json`` without ending the task.
    """
    active_entries: list[dict[str, Any]] = []

    with _INCOMPLETE_TASKS_LOCK:
        entries = _load_incomplete_index()
        for entry in entries:
            step = _checkpoint_step(entry["ticker"], entry["trade_date"])
            view = dict(entry)
            view["checkpoint_step"] = step
            active_entries.append(view)

        active_entries.sort(key=lambda e: float(e.get("updated_at", 0)), reverse=True)
    return active_entries


def list_active_incomplete_tasks() -> list[dict[str, Any]]:
    """Incomplete runs that should surface after a page refresh (no live tracker)."""
    active_statuses = {"running", "paused", "error"}
    return [
        entry
        for entry in get_incomplete_history()
        if entry.get("status") in active_statuses
    ]


def format_refresh_incomplete_notice(entries: list[dict[str, Any]]) -> str | None:
    """User-facing notice when session lost its ProgressTracker after refresh."""
    if not entries:
        return None
    parts: list[str] = []
    for entry in entries[:5]:
        status_label = {
            "error": "出错",
            "paused": "已暂停",
            "running": "进行中",
        }.get(str(entry.get("status")), "未完成")
        stages = entry.get("completed_stages") or []
        stage_bit = f"，已完成 {len(stages)} 阶段" if stages else ""
        parts.append(
            f"{entry['ticker']}（{status_label}{stage_bit}）"
        )
    joined = "、".join(parts)
    return (
        f"刷新后失去实时进度面板；检测到未完成任务：{joined}。"
        "侧栏「未完成任务」可从断点继续（勿与仍在后台跑的旧线程重叠时重复开跑）。"
    )


def load_analysis(path: str) -> dict[str, Any]:
    """Load a saved analysis JSON file."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def extract_signal(state: dict[str, Any]) -> str:
    """Extract the short signal (Buy/Sell/Hold) from a final state dict."""
    import re

    cn_map = {
        "买入": "Buy",
        "加仓": "Buy",
        "增持": "Buy",
        "卖出": "Sell",
        "减仓": "Sell",
        "清仓": "Sell",
        "持有": "Hold",
        "观望": "Hold",
    }
    rating_map = {
        "BUY": "Buy",
        "OVERWEIGHT": "Buy",
        "HOLD": "Hold",
        "UNDERWEIGHT": "Sell",
        "SELL": "Sell",
    }

    for field in (
        "final_trade_decision",
        "investment_plan",
        "trader_investment_decision",
        "trader_investment_plan",
    ):
        text = state.get(field, "")
        if not text:
            continue
        cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)

        # Structured English rating line first
        m = re.search(
            r"\*\*Rating\*\*\s*:\s*\*?\*?([A-Za-z]+)",
            cleaned,
            flags=re.IGNORECASE,
        )
        if m:
            mapped = rating_map.get(m.group(1).upper())
            if mapped:
                return mapped

        m = re.search(r"最终评级[：:]\s*\*?\*?\s*([^\n*]+)", cleaned)
        if m:
            label = m.group(1).strip()
            for cn, en in cn_map.items():
                if cn in label:
                    return en
            up = label.upper()
            for key, en in rating_map.items():
                if key in up:
                    return en

        for cn, en in cn_map.items():
            if cn in cleaned:
                return en

        for keyword in ("BUY", "SELL", "HOLD"):
            if keyword in cleaned.upper():
                return keyword.capitalize()
    return "N/A"
