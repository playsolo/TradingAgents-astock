"""观察池数据模型（纯 dataclass，便于 JSON 序列化）。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Baseline:
    """加入观察池时冻结的分析基准。"""

    ticker: str
    trade_date: str
    market: str  # 一期仅 "CN"
    stance: str
    position_pct: float | None
    baseline_price: float | None
    entry_price: float | None
    stop_loss: float | None
    thesis_summary: str
    major_risks: list[str]
    log_path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Baseline:
        return cls(
            ticker=str(data["ticker"]).upper(),
            trade_date=str(data["trade_date"]),
            market=str(data.get("market", "CN")),
            stance=str(data.get("stance", "Hold")),
            position_pct=data.get("position_pct"),
            baseline_price=data.get("baseline_price"),
            entry_price=data.get("entry_price"),
            stop_loss=data.get("stop_loss"),
            thesis_summary=str(data.get("thesis_summary", "")),
            major_risks=list(data.get("major_risks") or []),
            log_path=str(data.get("log_path", "")),
        )


@dataclass
class MarketSnapshot:
    price: float
    change_pct: float
    name: str
    pe_ttm: float | None = None
    turnover_pct: float | None = None
    headlines: list[str] = field(default_factory=list)
    # 东财分钟资金流最新主力净流入（元）；未知/非交易时段为 None
    main_net_inflow: float | None = None


@dataclass
class Alert:
    kind: str  # stance | position | price | stop_loss | risk
    title: str
    detail: str
    observed_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Alert:
        return cls(
            kind=str(data.get("kind", "")),
            title=str(data.get("title", "")),
            detail=str(data.get("detail", "")),
            observed_at=str(data.get("observed_at", "")),
        )


LEAN_LABELS = {
    "optimistic": "乐观",
    "neutral": "中性",
    "pessimistic": "悲观",
}
LEAN_CHOICES = frozenset(LEAN_LABELS)


@dataclass
class ScenarioOutlook:
    view: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"view": self.view, "reason": self.reason}

    @classmethod
    def from_dict(cls, data: Any) -> ScenarioOutlook:
        if not isinstance(data, dict):
            return cls(view="", reason="")
        return cls(
            view=str(data.get("view") or "").strip(),
            reason=str(data.get("reason") or "").strip(),
        )


@dataclass
class ObservationBriefing:
    """一次观察的盘面小结 + 三情景 + 今日倾向（手动与定时共用）。"""

    market_brief: str
    lean: str  # optimistic | neutral | pessimistic
    lean_reason: str
    scenarios: dict[str, ScenarioOutlook] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "market_brief": self.market_brief,
            "lean": self.lean,
            "lean_reason": self.lean_reason,
            "scenarios": {k: v.to_dict() for k, v in self.scenarios.items()},
        }

    @classmethod
    def from_dict(cls, data: Any) -> ObservationBriefing | None:
        if not isinstance(data, dict):
            return None
        raw_scenarios = data.get("scenarios") or {}
        scenarios: dict[str, ScenarioOutlook] = {}
        if isinstance(raw_scenarios, dict):
            for key in ("optimistic", "neutral", "pessimistic"):
                scenarios[key] = ScenarioOutlook.from_dict(raw_scenarios.get(key))
        lean = str(data.get("lean") or "neutral").strip().lower()
        if lean not in LEAN_CHOICES:
            lean = "neutral"
        return cls(
            market_brief=str(data.get("market_brief") or "").strip(),
            lean=lean,
            lean_reason=str(data.get("lean_reason") or "").strip(),
            scenarios=scenarios,
        )


@dataclass
class WatchItem:
    baseline: Baseline
    enabled: bool = True
    alerts: list[Alert] = field(default_factory=list)
    last_observed_at: str | None = None
    last_slot_key: str | None = None
    last_summary: str | None = None
    last_briefing: ObservationBriefing | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline": self.baseline.to_dict(),
            "enabled": self.enabled,
            "alerts": [a.to_dict() for a in self.alerts],
            "last_observed_at": self.last_observed_at,
            "last_slot_key": self.last_slot_key,
            "last_summary": self.last_summary,
            "last_briefing": self.last_briefing.to_dict() if self.last_briefing else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WatchItem:
        return cls(
            baseline=Baseline.from_dict(data["baseline"]),
            enabled=bool(data.get("enabled", True)),
            alerts=[Alert.from_dict(a) for a in (data.get("alerts") or [])],
            last_observed_at=data.get("last_observed_at"),
            last_slot_key=data.get("last_slot_key"),
            last_summary=data.get("last_summary"),
            last_briefing=ObservationBriefing.from_dict(data.get("last_briefing")),
        )
