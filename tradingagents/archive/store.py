"""Per-ticker archive directory store."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from tradingagents.archive.models import ActivePlan, PlanDelta
from tradingagents.watchlist.models import Baseline

_LOCK = threading.RLock()

_DEFAULT_ROOT = Path(
    os.getenv(
        "TRADINGAGENTS_ARCHIVE_DIR",
        str(Path.home() / ".tradingagents" / "archives"),
    )
)


def _ticker_dir(root: Path, ticker: str, market: str) -> Path:
    m = (market or "CN").upper()
    t = (ticker or "").strip().upper()
    return root / m / t


class StockArchiveStore:
    """One directory per (market, ticker): active_plan + delta_latest (+ future files)."""

    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else _DEFAULT_ROOT

    def dir_for(self, ticker: str, market: str = "CN") -> Path:
        return _ticker_dir(self.root, ticker, market)

    def get_active_plan(self, ticker: str, market: str = "CN") -> ActivePlan | None:
        path = self.dir_for(ticker, market) / "active_plan.json"
        with _LOCK:
            if not path.exists():
                return None
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                return None
        if not isinstance(data, dict):
            return None
        try:
            plan = ActivePlan.from_dict(data)
        except (KeyError, TypeError, ValueError):
            return None
        if plan.status == "void":
            return None
        return plan

    def save_active_plan(self, plan: ActivePlan) -> None:
        d = self.dir_for(plan.ticker, plan.market)
        path = d / "active_plan.json"
        meta_path = d / "meta.json"
        with _LOCK:
            d.mkdir(parents=True, exist_ok=True)
            payload = plan.to_dict()
            tmp = path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            tmp.replace(path)
            meta = {
                "version": 1,
                "ticker": plan.ticker,
                "market": plan.market,
                "plan_version": plan.plan_version,
                "last_full_trade_date": plan.trade_date,
                "status": plan.status,
            }
            meta_tmp = meta_path.with_suffix(".tmp")
            with open(meta_tmp, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
            meta_tmp.replace(meta_path)

    def save_from_baseline(
        self,
        baseline: Baseline,
        *,
        invalidation: str = "",
        watch_items: list[str] | None = None,
        source_mode: str = "full_reeval",
        bump_version: bool = True,
    ) -> ActivePlan:
        """Write/replace active plan from a Baseline (full_reeval path)."""
        existing = self.get_active_plan(baseline.ticker, baseline.market)
        if bump_version and existing is not None:
            version = existing.plan_version + 1
        elif existing is not None:
            version = existing.plan_version
        else:
            version = 1
        plan = ActivePlan.from_baseline(
            baseline,
            invalidation=invalidation,
            watch_items=watch_items,
            plan_version=version,
            status="active",
            source_mode=source_mode,
        )
        self.save_active_plan(plan)
        return plan

    def ensure_from_baseline(self, baseline: Baseline) -> ActivePlan:
        """Lazy migrate: keep existing archive plan, else seed from baseline without bump."""
        existing = self.get_active_plan(baseline.ticker, baseline.market)
        if existing is not None:
            return existing
        return self.save_from_baseline(
            baseline, source_mode="migrated", bump_version=False
        )

    def get_delta(self, ticker: str, market: str = "CN") -> PlanDelta | None:
        path = self.dir_for(ticker, market) / "delta_latest.json"
        with _LOCK:
            if not path.exists():
                return None
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                return None
        if not isinstance(data, dict):
            return None
        try:
            return PlanDelta.from_dict(data)
        except (KeyError, TypeError, ValueError):
            return None

    def save_delta(self, delta: PlanDelta) -> None:
        d = self.dir_for(delta.ticker, delta.market)
        path = d / "delta_latest.json"
        with _LOCK:
            d.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(delta.to_dict(), f, ensure_ascii=False, indent=2)
            tmp.replace(path)

    def void_plan(self, ticker: str, market: str = "CN", reason: str = "") -> bool:
        plan = self.get_active_plan(ticker, market)
        if plan is None:
            # May already be missing; try raw file
            path = self.dir_for(ticker, market) / "active_plan.json"
            if not path.exists():
                return False
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                plan = ActivePlan.from_dict(data)
            except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
                return False
        plan.status = "void"
        if reason:
            note = f"void: {reason}"
            plan.invalidation = (
                f"{plan.invalidation}; {note}" if plan.invalidation else note
            )
        self.save_active_plan(plan)
        return True


def default_archive_store() -> StockArchiveStore:
    return StockArchiveStore()
