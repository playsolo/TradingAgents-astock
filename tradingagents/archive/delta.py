"""Rule-based plan delta (no LLM)."""

from __future__ import annotations

from datetime import datetime, timezone

from tradingagents.archive.models import ActivePlan, PlanDelta


def compute_plan_delta(
    plan: ActivePlan,
    *,
    current_price: float | None,
    price_hard_threshold_pct: float = 5.0,
    recommend_mode: str = "",
) -> PlanDelta:
    """Compare live price to active plan; produce a compact change summary."""
    move_pct: float | None = None
    risk_flags: list[str] = []

    if (
        current_price is not None
        and current_price > 0
        and plan.baseline_price is not None
        and float(plan.baseline_price) > 0
    ):
        move_pct = (
            abs(float(current_price) - float(plan.baseline_price))
            / float(plan.baseline_price)
            * 100.0
        )
        if move_pct > float(price_hard_threshold_pct):
            risk_flags.append(f"price_move>{price_hard_threshold_pct:g}%")

    vs_stop = "unknown"
    if current_price is not None and current_price > 0 and plan.stop_loss is not None:
        if float(current_price) < float(plan.stop_loss):
            vs_stop = "breached"
            risk_flags.append("stop_loss_breached")
        else:
            vs_stop = "ok"

    vs_entry = "unknown"
    if current_price is not None and current_price > 0 and plan.entry_price is not None:
        entry = float(plan.entry_price)
        px = float(current_price)
        if abs(px - entry) / entry <= 0.005:
            vs_entry = "at"
        elif px < entry:
            vs_entry = "below"
        else:
            vs_entry = "above"

    if plan.status == "stale":
        risk_flags.append("plan_stale")

    return PlanDelta(
        ticker=plan.ticker,
        market=plan.market,
        vs_plan_version=plan.plan_version,
        current_price=float(current_price) if current_price and current_price > 0 else None,
        price_move_pct=move_pct,
        vs_stop=vs_stop,
        vs_entry=vs_entry,
        risk_flags=risk_flags,
        recommend_mode=recommend_mode,
        computed_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
