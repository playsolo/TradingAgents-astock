"""Refresh ``~/.tradingagents/stock_names.json`` from authoritative sources.

Writes only come from ``resolve_stock_name`` (A-share 腾讯 / US yfinance / aliases).
Report extraction never updates this file.

Usage::

    python -m web.refresh_stock_names --fix-junk
    python -m web.refresh_stock_names --codes INFQ,AMPX
    tradingagents-refresh-names --fix-junk --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any


def refresh_stock_names(
    *,
    path: Path | None = None,
    codes: list[str] | None = None,
    fix_junk: bool = False,
    dry_run: bool = False,
    seed_aliases: bool = False,
) -> list[dict[str, Any]]:
    """Re-resolve selected (or junk) cache entries; return change records.

    Each record: ``{code, old, new}``. ``new`` is None when resolve fails and the
    junk entry is removed.
    """
    from web import stock_display as sd

    cache_path = Path(path) if path else (Path.home() / ".tradingagents" / "stock_names.json")
    cache = sd.StockNameCache(cache_path)
    sd._NAME_CACHE = cache
    sd.resolve_stock_name.cache_clear()
    try:
        sd._yfinance_name.cache_clear()
    except Exception:
        pass
    try:
        sd._tencent_name.cache_clear()
    except Exception:
        pass

    existing = cache.items()
    targets: list[str] = []
    if codes:
        targets.extend(str(c).strip().upper() for c in codes if str(c).strip())
    if fix_junk:
        for code, name in existing.items():
            if sd.is_junk_stock_name(name) or not sd._is_cache_worthy_name(name, code):
                targets.append(code)
    # de-dupe preserve order
    seen: set[str] = set()
    ordered: list[str] = []
    for code in targets:
        if code not in seen:
            seen.add(code)
            ordered.append(code)

    changes: list[dict[str, Any]] = []
    for code in ordered:
        old = cache.get(code)
        if dry_run:
            # Preview what resolve would do without mutating yet for junk check
            sd.resolve_stock_name.cache_clear()
            # Temporarily hide cache hit
            prev = cache.delete(code) if old is not None else None
            try:
                new = sd.resolve_stock_name(code)
            finally:
                if prev is not None:
                    cache.set(code, prev)
                sd.resolve_stock_name.cache_clear()
            if new != old:
                changes.append({"code": code, "old": old, "new": new})
            continue

        if old is not None:
            cache.delete(code)
        sd.resolve_stock_name.cache_clear()
        new = sd.resolve_stock_name(code)
        # resolve_stock_name writes on success; if None, leave deleted
        cached = cache.get(code)
        if cached != old:
            changes.append({"code": code, "old": old, "new": cached})
        elif new is None and old is not None:
            changes.append({"code": code, "old": old, "new": None})

    if seed_aliases and not dry_run:
        for code, alias in sd._US_CN_ALIASES.items():
            cur = cache.get(code)
            if cur != alias:
                if cur is None or sd.is_junk_stock_name(cur) or not sd._is_cache_worthy_name(
                    cur, code
                ):
                    cache.set(code, alias)
                    changes.append({"code": code, "old": cur, "new": alias})

    return changes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Refresh stock_names.json from 腾讯 / yfinance / CN aliases."
    )
    parser.add_argument(
        "--path",
        type=Path,
        default=None,
        help="Cache file path (default: ~/.tradingagents/stock_names.json)",
    )
    parser.add_argument(
        "--codes",
        type=str,
        default="",
        help="Comma-separated tickers to force-refresh (e.g. INFQ,AMPX)",
    )
    parser.add_argument(
        "--fix-junk",
        action="store_true",
        help="Re-resolve entries that fail junk / cache-worthy checks",
    )
    parser.add_argument(
        "--seed-aliases",
        action="store_true",
        help="Ensure _US_CN_ALIASES are present for missing/junk US tickers",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show planned changes without writing",
    )
    args = parser.parse_args(argv)

    codes = [c for c in (args.codes or "").split(",") if c.strip()]
    if not codes and not args.fix_junk and not args.seed_aliases:
        parser.error("specify --fix-junk and/or --codes and/or --seed-aliases")

    changes = refresh_stock_names(
        path=args.path,
        codes=codes or None,
        fix_junk=args.fix_junk,
        dry_run=args.dry_run,
        seed_aliases=args.seed_aliases,
    )
    if not changes:
        print("No changes.")
        return 0
    prefix = "[dry-run] " if args.dry_run else ""
    for row in changes:
        print(f"{prefix}{row['code']}: {row['old']!r} -> {row['new']!r}")
    print(f"{prefix}{len(changes)} entr{'y' if len(changes) == 1 else 'ies'} updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
