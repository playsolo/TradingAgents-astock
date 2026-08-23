"""Backtest scan archives: forward returns, excess vs CSI 300, factor attribution."""

from __future__ import annotations

import json
import logging
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

from tradingagents.evaluation.price_fetcher import (
    BENCHMARK_CODE,
    DEFAULT_HORIZONS,
    forward_returns,
    sina_close_fetcher,
)
from tradingagents.strategies.scan_store import (
    STRATEGY_VALUE_SWING,
    ValueSwingScanStore,
    default_store,
    resolve_strategy,
)

logger = logging.getLogger(__name__)

DROP_THRESHOLD_5D = -0.08
CloseFetcher = Callable[[str, str, int], list[float] | None]


def list_scan_archives(
    strategy: str = STRATEGY_VALUE_SWING,
    *,
    archive_dir: Path | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Load completed scan archive records (newest first)."""
    store = default_store(strategy) if archive_dir is None else ValueSwingScanStore(
        archive_dir=archive_dir, strategy=strategy
    )
    root = store.archive_dir
    if not root.exists():
        return []
    paths = sorted(root.glob("*.json"), reverse=True)
    if limit is not None:
        paths = paths[: int(limit)]
    records: list[dict[str, Any]] = []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        if data.get("status") != "completed":
            continue
        if not (data.get("result") or {}).get("candidates"):
            continue
        records.append(data)
    return records


def _scan_trade_date(record: dict[str, Any]) -> str | None:
    result = record.get("result") or {}
    scan_date = str(result.get("scan_date") or "").strip()
    if scan_date:
        return scan_date
    finished = str(record.get("finished_at") or "")[:10]
    return finished or None


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    return float(statistics.median(values))


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return float(statistics.mean(values))


def _rank(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    for r, idx in enumerate(order, start=1):
        ranks[idx] = float(r)
    return ranks


def spearman_ic(x: list[float], y: list[float]) -> float | None:
    if len(x) != len(y) or len(x) < 3:
        return None
    rx, ry = _rank(x), _rank(y)
    n = len(x)
    mx = sum(rx) / n
    my = sum(ry) / n
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    den_x = sum((rx[i] - mx) ** 2 for i in range(n)) ** 0.5
    den_y = sum((ry[i] - my) ** 2 for i in range(n)) ** 0.5
    if den_x == 0 or den_y == 0:
        return None
    return num / (den_x * den_y)


def _evaluate_candidate_row(
    candidate: dict[str, Any],
    trade_date: str,
    horizons: Sequence[int],
    *,
    fetcher: CloseFetcher,
    bench_cache: dict[str, dict[int, float]],
) -> dict[str, Any] | None:
    code = str(candidate.get("code") or "").strip()
    if not code:
        return None
    stock_rets = forward_returns(code, trade_date, horizons, fetcher=fetcher)
    if not stock_rets:
        return None
    if trade_date not in bench_cache:
        bench_cache[trade_date] = forward_returns(
            BENCHMARK_CODE, trade_date, horizons, fetcher=fetcher
        ) or {}
    bench = bench_cache[trade_date]
    excess = {
        h: stock_rets[h] - bench.get(h, 0.0)
        for h in stock_rets
        if h in bench
    }
    return {
        "code": code,
        "signal_score": candidate.get("signal_score"),
        "returns": stock_rets,
        "excess": excess,
        "factor_hits": candidate.get("factor_hits") or [],
    }


def evaluate_scan_archives(
    archives: Sequence[dict[str, Any]],
    *,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    fetcher: CloseFetcher | None = None,
    drop_threshold_5d: float = DROP_THRESHOLD_5D,
) -> dict[str, Any]:
    """Evaluate a batch of scan archive records."""
    fetch = fetcher or sina_close_fetcher
    hs = tuple(int(h) for h in horizons)
    rows: list[dict[str, Any]] = []
    per_scan: list[dict[str, Any]] = []
    bench_cache: dict[str, dict[int, float]] = {}

    for record in archives:
        trade_date = _scan_trade_date(record)
        if not trade_date:
            continue
        candidates = (record.get("result") or {}).get("candidates") or []
        scan_rows: list[dict[str, Any]] = []
        for cand in candidates:
            row = _evaluate_candidate_row(
                cand, trade_date, hs, fetcher=fetch, bench_cache=bench_cache
            )
            if row:
                row["scan_date"] = trade_date
                row["scan_id"] = record.get("scan_id")
                scan_rows.append(row)
                rows.append(row)
        if scan_rows:
            ex20 = [r["excess"].get(20) for r in scan_rows if r["excess"].get(20) is not None]
            per_scan.append(
                {
                    "scan_id": record.get("scan_id"),
                    "scan_date": trade_date,
                    "candidates": len(candidates),
                    "settled": len(scan_rows),
                    "median_excess_20d": _median(ex20),
                }
            )

    aggregate: dict[str, dict[str, Any]] = {}
    for h in hs:
        rets = [r["returns"].get(h) for r in rows if r["returns"].get(h) is not None]
        excess = [r["excess"].get(h) for r in rows if r["excess"].get(h) is not None]
        cell: dict[str, Any] = {
            "n": len(rets),
            "median_return": _median(rets),
            "mean_return": _mean(rets),
            "median_excess": _median(excess),
            "mean_excess": _mean(excess),
            "hit_rate": (sum(1 for x in rets if x > 0) / len(rets)) if rets else None,
        }
        if h == 5:
            cell["drop_rate"] = (
                sum(1 for x in rets if x <= drop_threshold_5d) / len(rets) if rets else None
            )
        aggregate[str(h)] = cell

    scores: list[float] = []
    ex20: list[float] = []
    for r in rows:
        sc = r.get("signal_score")
        e = r["excess"].get(20)
        if sc is not None and e is not None:
            try:
                scores.append(float(sc))
                ex20.append(float(e))
            except (TypeError, ValueError):
                pass

    by_factor: dict[str, dict[str, Any]] = {}
    for r in rows:
        e20 = r["excess"].get(20)
        if e20 is None:
            continue
        for fh in r.get("factor_hits") or []:
            if not isinstance(fh, dict) or not fh.get("active"):
                continue
            key = str(fh.get("key") or "")
            if not key:
                continue
            bucket = by_factor.setdefault(
                key,
                {
                    "label": fh.get("label") or key,
                    "hit_excess": [],
                    "miss_excess": [],
                },
            )
            if fh.get("hit"):
                bucket["hit_excess"].append(float(e20))
            else:
                bucket["miss_excess"].append(float(e20))

    factor_summary: dict[str, Any] = {}
    for key, bucket in by_factor.items():
        factor_summary[key] = {
            "label": bucket["label"],
            "hit_n": len(bucket["hit_excess"]),
            "miss_n": len(bucket["miss_excess"]),
            "hit_median_excess_20d": _median(bucket["hit_excess"]),
            "miss_median_excess_20d": _median(bucket["miss_excess"]),
        }

    return {
        "horizons": list(hs),
        "evaluable_scans": len(per_scan),
        "total_candidate_rows": len(rows),
        "aggregate": aggregate,
        "score_excess_ic_20d": spearman_ic(scores, ex20),
        "by_factor": factor_summary,
        "recent_scans": per_scan[:20],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def evaluate_scan_history(
    strategy: str = STRATEGY_VALUE_SWING,
    *,
    archive_limit: int = 60,
    archive_dir: Path | None = None,
    since_scan_date: str | None = None,
    until_scan_date: str | None = None,
    fetcher: CloseFetcher | None = None,
) -> dict[str, Any]:
    """Evaluate archived scans for one strategy."""
    strategy = resolve_strategy(strategy)
    archives = list_scan_archives(strategy, archive_dir=archive_dir, limit=archive_limit)
    if since_scan_date:
        archives = [a for a in archives if (_scan_trade_date(a) or "") >= since_scan_date]
    if until_scan_date:
        archives = [a for a in archives if (_scan_trade_date(a) or "") <= until_scan_date]
    report = evaluate_scan_archives(archives, fetcher=fetcher)
    report["strategy"] = strategy
    report["archive_count"] = len(archives)
    return report


def compare_scan_periods(
    strategy: str = STRATEGY_VALUE_SWING,
    *,
    before_until: str,
    after_since: str,
    archive_limit: int = 120,
    fetcher: CloseFetcher | None = None,
) -> dict[str, Any]:
    """Compare median 20d excess before/after a rollout date (e.g. HiThink Phase 2)."""
    before = evaluate_scan_history(
        strategy,
        archive_limit=archive_limit,
        until_scan_date=before_until,
        fetcher=fetcher,
    )
    after = evaluate_scan_history(
        strategy,
        archive_limit=archive_limit,
        since_scan_date=after_since,
        fetcher=fetcher,
    )
    b_med = (before.get("aggregate") or {}).get("20", {}).get("median_excess")
    a_med = (after.get("aggregate") or {}).get("20", {}).get("median_excess")
    delta = None
    if b_med is not None and a_med is not None:
        delta = float(a_med) - float(b_med)
    return {
        "strategy": strategy,
        "before_until": before_until,
        "after_since": after_since,
        "before": before,
        "after": after,
        "median_excess_20d_delta": delta,
    }
