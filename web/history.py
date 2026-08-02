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
    """Sort key for signal groups: Buy > Overweight > Hold > Underweight > Sell > N/A."""
    s = signal.upper() if signal else ""
    if s == "BUY":
        return 0
    if s == "OVERWEIGHT":
        return 1
    if s == "HOLD":
        return 2
    if s == "UNDERWEIGHT":
        return 3
    if s == "SELL":
        return 4
    # Backward compat: substring match for legacy 3-tier signals
    if "BUY" in s:
        return 0
    if "SELL" in s:
        return 4
    if "HOLD" in s:
        return 2
    return 5


def group_history_by_signal(
    entries: list[dict[str, Any]],
    *,
    watch_signals: dict[str, dict[str, Any]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Group history entries by signal (Buy / Sell / Hold / N/A / WatchBuy).

    When *watch_signals* is provided (from ``read_signals()``), entries whose
    ticker has an active buy-zone trigger also appear in the ``"WatchBuy"``
    group alongside their original signal group (no dedupe — same entry may
    appear in both WatchBuy and its original group).
    """
    groups: dict[str, list[dict[str, Any]]] = {
        "Buy": [],
        "Overweight": [],
        "Hold": [],
        "Underweight": [],
        "Sell": [],
        "N/A": [],
        "WatchBuy": [],
    }
    for entry in entries:
        sig = entry.get("signal", "N/A")
        if sig in groups:
            groups[sig].append(entry)
        else:
            groups["N/A"].append(entry)
    # ── WatchBuy group: one entry per active signal, always the *latest*
    # history entry for that ticker. The buy-zone trigger is a price event
    # that should link to the most recent analysis, not necessarily the
    # one that originally defined the zone.
    if watch_signals:
        # Build a ticker → [entries] index for fast lookup
        by_ticker: dict[str, list[dict[str, Any]]] = {}
        for entry in entries:
            tk = (entry.get("ticker") or "").strip().upper()
            if tk:
                by_ticker.setdefault(tk, []).append(entry)
        for tk, ws in watch_signals.items():
            ticker_entries = by_ticker.get(tk)
            if not ticker_entries:
                continue
            # History is sorted newest-first by get_history(); pick the first
            # (most recent) entry as the canonical link.
            latest = ticker_entries[0]
            latest["_watch_signal"] = ws
            groups["WatchBuy"].append(latest)
    return groups


def signal_count_label(group: dict[str, list[dict[str, Any]]], total: int) -> str:
    """Build a compact summary line, e.g. '共 42 条 · 买入12 持有18 卖出8'."""
    parts = [f"共 {total} 条"]
    for sig, display in [
        ("Buy", "买入"),
        ("Overweight", "增持"),
        ("Hold", "持有"),
        ("Underweight", "减持"),
        ("Sell", "卖出"),
        ("WatchBuy", "关注-待买入"),
    ]:
        n = len(group.get(sig, []))
        if n:
            parts.append(f"{display}{n}")
    return "  ·  ".join(parts)


def history_stamp() -> tuple[int, int]:
    """Cheap filesystem stamp for sidebar auto-refresh: ``(count, max_mtime_ns)``.

    Does not open/parse JSON — safe to poll from a Streamlit fragment while a
    worker writes new ``full_states_log_*.json`` files out of process.
    """
    root = _results_dir()
    if not root.exists():
        return (0, 0)

    count = 0
    max_mtime_ns = 0
    for log_file in root.rglob("full_states_log_*.json"):
        match = re.search(r"full_states_log_(\d{4}-\d{2}-\d{2})\.json$", log_file.name)
        if not match:
            continue
        count += 1
        try:
            max_mtime_ns = max(max_mtime_ns, log_file.stat().st_mtime_ns)
        except OSError:
            continue
    return (count, max_mtime_ns)


def get_history() -> list[dict[str, Any]]:
    """Scan saved analysis logs and return a sorted list (newest first).

    Sorted by analysis completion time (log file mtime), not trade date.
    Each entry: {"ticker", "date", "path", "signal", "analyzed_at", "mtime"}
    """
    from datetime import datetime

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
        analyzed_at = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
        entries.append(
            (
                mtime,
                {
                    "ticker": ticker,
                    "date": date,
                    "path": str(log_file),
                    "signal": signal,
                    "analyzed_at": analyzed_at,
                    "mtime": mtime,
                },
            )
        )

    entries.sort(key=lambda item: item[0], reverse=True)
    return [entry for _, entry in entries]


def list_ticker_analysis_timeline(
    ticker: str,
    *,
    limit: int = 12,
) -> list[dict[str, Any]]:
    """Same-ticker analyses oldest→newest for report timeline (cap ``limit``).

    Each item: ticker, date, path, signal, analyzed_at, mtime.
    """
    rows = filter_history_by_ticker(get_history(), ticker)
    if not rows:
        return []
    # get_history is newest-first; timeline reads left→right old→new
    chronological = list(reversed(rows))
    if limit > 0 and len(chronological) > limit:
        chronological = chronological[-limit:]
    return chronological


def filter_history_by_ticker(
    entries: list[dict[str, Any]],
    ticker: str,
) -> list[dict[str, Any]]:
    """Keep history rows whose ticker matches ``ticker`` (case-insensitive).

    Empty ``ticker`` returns ``entries`` unchanged (no filter).
    """
    key = str(ticker or "").strip().upper()
    if not key:
        return list(entries)
    return [
        entry
        for entry in entries
        if str(entry.get("ticker") or "").strip().upper() == key
    ]


def _has_chinese(value: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in value)


def resolve_history_search_query(raw: str) -> tuple[str | None, str | None]:
    """Normalize sidebar history search input to a ticker code.

    Returns:
      ``(None, None)`` when blank — caller should show the full history list.
      ``(ticker, None)`` when resolved (A-share code or US symbol).
      ``(None, error)`` when the input cannot be resolved.
    """
    token = (raw or "").strip()
    if not token:
        return None, None

    from tradingagents.dataflows.utils import safe_ticker_component
    from web.stock_display import lookup_code_by_cached_name, remember_resolved_name

    cached = lookup_code_by_cached_name(token)
    if cached:
        return cached, None

    # 6-digit codes and Chinese names → A-share resolve path
    if (token.isdigit() and len(token) == 6) or _has_chinese(token):
        try:
            from tradingagents.dataflows.a_stock import resolve_ticker

            code = resolve_ticker(token)
            if _has_chinese(token):
                remember_resolved_name(code, token)
            return code, None
        except ValueError as exc:
            return None, str(exc)

    # US (and other Latin) tickers
    if any(ch.isspace() for ch in token):
        return None, "美股代码不能包含空格"
    try:
        return safe_ticker_component(token.upper()), None
    except ValueError as exc:
        return None, str(exc)


def _action_plan_summary_from_state(state: dict[str, Any]) -> dict[str, Any] | None:
    plan = state.get("action_plan")
    if not isinstance(plan, dict) or not plan.get("rating"):
        return None
    return {
        "rating": str(plan.get("rating") or "").strip(),
        "holders_action": str(plan.get("holders_action") or "").strip() or None,
        "non_holders_action": str(plan.get("non_holders_action") or "").strip() or None,
        "horizon": str(plan.get("horizon") or "").strip() or None,
        "summary": str(plan.get("summary") or "").strip() or None,
    }


def lookup_latest_action_plans(
    tickers: list[str] | tuple[str, ...] | set[str],
) -> dict[str, dict[str, Any]]:
    """为给定代码取「最近一次」完成分析的操作建议摘要（按日志 mtime）。

    有 ``action_plan`` 时返回结构化字段；否则至少回填 sidebar ``signal``。
    Key 为 uppercase ticker。
    """
    wanted = {str(t).strip().upper() for t in tickers if str(t).strip()}
    if not wanted:
        return {}

    found: dict[str, dict[str, Any]] = {}
    for entry in get_history():
        ticker = str(entry.get("ticker") or "").strip().upper()
        if ticker not in wanted or ticker in found:
            continue
        path = entry.get("path") or ""
        summary: dict[str, Any] = {
            "rating": None,
            "holders_action": None,
            "non_holders_action": None,
            "horizon": None,
            "summary": None,
            "signal": entry.get("signal") or "N/A",
            "date": entry.get("date"),
            "path": path,
        }
        try:
            with open(path, encoding="utf-8") as f:
                state = json.load(f)
            if isinstance(state, dict):
                plan = _action_plan_summary_from_state(state)
                if plan:
                    summary.update(plan)
        except (OSError, json.JSONDecodeError, TypeError):
            pass
        found[ticker] = summary
        if len(found) >= len(wanted):
            break
    return found


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
    resume_count: int | None = None,
) -> None:
    """Upsert a resumable task entry.

    ``resume_count`` tracks how many times the worker has auto-continued this
    ticker+date after an interrupt. When omitted, the previous value is kept
    (or 0 for a new row).
    """
    ticker = ticker.strip().upper()
    trade_date = trade_date.strip()
    if not ticker or not trade_date:
        return

    with _INCOMPLETE_TASKS_LOCK:
        prev_resume = 0
        entries = []
        for entry in _load_incomplete_index():
            if _completed_key(entry["ticker"], entry["trade_date"]) == _completed_key(
                ticker, trade_date
            ):
                try:
                    prev_resume = int(entry.get("resume_count", 0) or 0)
                except (TypeError, ValueError):
                    prev_resume = 0
                continue
            entries.append(entry)
        now = time.time()
        if resume_count is None:
            count = prev_resume
        else:
            try:
                count = max(0, int(resume_count))
            except (TypeError, ValueError):
                count = prev_resume
        entries.append(
            {
                "ticker": ticker,
                "trade_date": trade_date,
                "status": status,
                "error": error or "",
                "completed_stages": completed_stages or [],
                "resume_count": count,
                "updated_at": now,
            }
        )
        entries.sort(key=lambda e: float(e.get("updated_at", 0)), reverse=True)
        _save_incomplete_index(entries)


def clear_incomplete_task(ticker: str, trade_date: str) -> None:
    """Remove an incomplete task (success, stop/discard, or successful re-queue)."""
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

    Entries stay until ``clear_incomplete_task`` (successful finish / stop /
    identity waiting in the analysis queue after enqueue).
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
    """Extract the short signal (Buy/Sell/Hold) from a final state dict.

    Prefers post-analysis ``action_plan.rating`` when present, then explicit
    rating labels (``Rating:`` / ``最终评级``). Uses ``parse_rating`` for
    structured label detection only; bare keyword scans are limited to short
    snippets (<120 chars) so debate prose like 「主张买入」cannot override a
    labeled 「减持」.
    """
    plan = state.get("action_plan")
    if isinstance(plan, dict) and plan.get("rating"):
        from tradingagents.agents.utils.action_plan import rating_to_sidebar_signal

        return rating_to_sidebar_signal(str(plan["rating"]))

    from tradingagents.agents.utils.rating import parse_rating
    from tradingagents.agents.utils.action_plan import rating_to_sidebar_signal

    rating_map = {
        "BUY": "Buy",
        "OVERWEIGHT": "Overweight",
        "HOLD": "Hold",
        "UNDERWEIGHT": "Underweight",
        "SELL": "Sell",
    }
    # Long PM memos often recount bull/bear arguments; only scan bare keywords
    # on short decision lines (e.g. saved "HOLD" / "最终评级：卖出").
    # Exclude 减持 from unordered body scan — it appears in risk prose
    # ("无减持计划") far more often than as a standalone decision word.
    _body_cn_map = {"买入": "Buy", "加仓": "Buy", "增持": "Overweight",
                    "卖出": "Sell", "减仓": "Sell", "清仓": "Sell",
                    "减持": "Underweight",
                    "持有": "Hold", "观望": "Hold"}
    _short_snippet_max = 120

    _UNKNOWN = ""
    for field in (
        "final_trade_decision",
        "trader_investment_decision",
        "investment_plan",
        "trader_investment_plan",
    ):
        text = state.get(field, "")
        if not text:
            continue
        cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)

        # US bridge produces "**Action**: Hold" in trader_investment_decision
        # and "FINAL TRANSACTION PROPOSAL: **HOLD**" without Chinese labels.
        m = re.search(
            r"(?:\*\*)?(?:Action|FINAL\s+TRANSACTION\s+PROPOSAL)(?:\*\*)?\s*:\s*\*?\*?([A-Za-z]+)",
            cleaned,
            flags=re.IGNORECASE,
        )
        if m:
            mapped = rating_map.get(m.group(1).upper())
            if mapped:
                return mapped

        # Structured rating line — supports both English "Rating" and
        # Chinese "评级", with either ASCII or full-width colon:
        #   **评级：Hold**  /  **评级**：**Hold**  /  Rating: Hold
        m = re.search(
            r"(?:\*\*)?(?:Rating|评级)(?:\*\*)?\s*[：:]\s*\*?\*?([A-Za-z]+)",
            cleaned,
            flags=re.IGNORECASE,
        )
        if m:
            mapped = rating_map.get(m.group(1).upper())
            if mapped:
                return mapped

        # Allow markdown between label and value — matches both the legacy
        # "最终评级" label and the newer LLM-produced "最终裁决" variant.
        # Leading ** is optional (LLMs often bold these labels):
        #   **最终评级**：**减持（Underweight）**
        #   **最终裁决：维持 Hold 评级**
        m = re.search(r"(?:\*\*)?(?:最终评级|最终裁决)[\s\*]*[：:][\s\*]*([^\n*]+)", cleaned)
        if m:
            rating = parse_rating(m.group(1).strip(), default=_UNKNOWN)
            if rating:
                return rating_to_sidebar_signal(rating)

        # Bare keyword fallback — only on short snippets to avoid
        # debate prose poisoning (e.g. 「主张买入」in bull argument
        # should not override a labeled 「减持」).
        if len(cleaned) <= _short_snippet_max:
            for cn, en in _body_cn_map.items():
                if cn in cleaned:
                    return en

            for keyword in ("BUY", "SELL", "HOLD"):
                if keyword in cleaned.upper():
                    return keyword.capitalize()
    return "N/A"
