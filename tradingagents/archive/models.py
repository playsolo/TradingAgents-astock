"""Stock archive data models (JSON-serializable dataclasses)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from tradingagents.watchlist.models import Baseline


@dataclass
class ActivePlan:
    """Current trade plan for one ticker (opinion layer; versioned)."""

    ticker: str
    trade_date: str
    market: str
    stance: str
    position_pct: float | None
    baseline_price: float | None
    entry_price: float | None
    stop_loss: float | None
    thesis_summary: str
    major_risks: list[str]
    log_path: str
    invalidation: str = ""
    watch_items: list[str] = field(default_factory=list)
    plan_version: int = 1
    status: str = "active"  # active | stale | void
    source_mode: str = "full_reeval"
    horizon_raw: str | None = None
    valid_trading_days: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ActivePlan:
        raw_days = data.get("valid_trading_days")
        valid_days: int | None
        if raw_days is None or raw_days == "":
            valid_days = None
        else:
            try:
                valid_days = int(raw_days)
            except (TypeError, ValueError):
                valid_days = None
        horizon = data.get("horizon_raw")
        version = data.get("plan_version", 1)
        try:
            plan_version = int(version)
        except (TypeError, ValueError):
            plan_version = 1
        return cls(
            ticker=str(data["ticker"]).upper(),
            trade_date=str(data["trade_date"]),
            market=str(data.get("market", "CN")).upper(),
            stance=str(data.get("stance", "Hold")),
            position_pct=data.get("position_pct"),
            baseline_price=data.get("baseline_price"),
            entry_price=data.get("entry_price"),
            stop_loss=data.get("stop_loss"),
            thesis_summary=str(data.get("thesis_summary", "")),
            major_risks=list(data.get("major_risks") or []),
            log_path=str(data.get("log_path", "")),
            invalidation=str(data.get("invalidation") or ""),
            watch_items=[str(x) for x in (data.get("watch_items") or []) if str(x).strip()],
            plan_version=max(1, plan_version),
            status=str(data.get("status") or "active"),
            source_mode=str(data.get("source_mode") or "full_reeval"),
            horizon_raw=str(horizon).strip() if horizon else None,
            valid_trading_days=valid_days,
        )

    def to_baseline(self) -> Baseline:
        return Baseline(
            ticker=self.ticker,
            trade_date=self.trade_date,
            market=self.market,
            stance=self.stance,
            position_pct=self.position_pct,
            baseline_price=self.baseline_price,
            entry_price=self.entry_price,
            stop_loss=self.stop_loss,
            thesis_summary=self.thesis_summary,
            major_risks=list(self.major_risks),
            log_path=self.log_path,
            horizon_raw=self.horizon_raw,
            valid_trading_days=self.valid_trading_days,
        )

    @classmethod
    def from_baseline(
        cls,
        baseline: Baseline,
        *,
        invalidation: str = "",
        watch_items: list[str] | None = None,
        plan_version: int = 1,
        status: str = "active",
        source_mode: str = "full_reeval",
    ) -> ActivePlan:
        return cls(
            ticker=baseline.ticker,
            trade_date=baseline.trade_date,
            market=baseline.market,
            stance=baseline.stance,
            position_pct=baseline.position_pct,
            baseline_price=baseline.baseline_price,
            entry_price=baseline.entry_price,
            stop_loss=baseline.stop_loss,
            thesis_summary=baseline.thesis_summary,
            major_risks=list(baseline.major_risks or []),
            log_path=baseline.log_path,
            invalidation=invalidation,
            watch_items=list(watch_items or []),
            plan_version=max(1, int(plan_version)),
            status=status,
            source_mode=source_mode,
            horizon_raw=baseline.horizon_raw,
            valid_trading_days=baseline.valid_trading_days,
        )


@dataclass
class PlanDelta:
    """Rule-computed change summary vs active plan."""

    ticker: str
    market: str
    vs_plan_version: int
    current_price: float | None
    price_move_pct: float | None
    vs_stop: str  # ok | breached | unknown
    vs_entry: str  # below | at | above | unknown
    risk_flags: list[str] = field(default_factory=list)
    recommend_mode: str = ""
    computed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlanDelta:
        return cls(
            ticker=str(data.get("ticker", "")).upper(),
            market=str(data.get("market", "CN")).upper(),
            vs_plan_version=int(data.get("vs_plan_version") or 1),
            current_price=data.get("current_price"),
            price_move_pct=data.get("price_move_pct"),
            vs_stop=str(data.get("vs_stop") or "unknown"),
            vs_entry=str(data.get("vs_entry") or "unknown"),
            risk_flags=[str(x) for x in (data.get("risk_flags") or [])],
            recommend_mode=str(data.get("recommend_mode") or ""),
            computed_at=str(data.get("computed_at") or ""),
        )


@dataclass
class Lesson:
    """Resolved same-ticker decision + outcome reflection (append-only)."""

    ticker: str
    trade_date: str
    market: str
    rating: str
    raw_return: float
    alpha_return: float
    holding_days: int
    reflection: str
    resolved_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Lesson:
        return cls(
            ticker=str(data.get("ticker", "")).upper(),
            trade_date=str(data.get("trade_date", "")),
            market=str(data.get("market", "CN")).upper(),
            rating=str(data.get("rating") or "Hold"),
            raw_return=float(data.get("raw_return") or 0.0),
            alpha_return=float(data.get("alpha_return") or 0.0),
            holding_days=int(data.get("holding_days") or 0),
            reflection=str(data.get("reflection") or "").strip(),
            resolved_at=str(data.get("resolved_at") or ""),
        )
