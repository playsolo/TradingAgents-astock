"""Stance continuity gate: block unjustified rating flips on unchanged data.

A — material-change gate (when large flips are allowed)
B — clamp proposed rating back to archive stance when flip is not allowed
D — structured ``stance_continuity`` metadata for report disclosure
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from tradingagents.agents.utils.rating import RATINGS_5_TIER, parse_rating
from tradingagents.archive.delta import compute_plan_delta
from tradingagents.archive.lessons import infer_market
from tradingagents.archive.models import ActivePlan, PlanDelta
from tradingagents.archive.store import StockArchiveStore, default_archive_store

_TIER_INDEX = {r: i for i, r in enumerate(RATINGS_5_TIER)}
# Without material evidence, allow at most ±1 tier (e.g. Overweight↔Hold).
MAX_STEP_WITHOUT_MATERIAL = 1

_MATERIAL_ROUTE_REASONS = frozenset(
    {"stop_loss", "price_move", "watch_alert", "stale_calibration"}
)

_RATING_LINE_RE = re.compile(
    r"(?im)^(\s*(?:\*\*)?Rating(?:\*\*)?\s*[：:]\s*)([A-Za-z]+)"
)
_CN_RATING_LINE_RE = re.compile(
    r"(?im)^(\s*(?:最终评级|投资评级|评级)\s*[：:]\s*)([^\n]+)"
)


@dataclass
class StanceContinuityResult:
    prior_stance: str | None
    proposed_stance: str
    final_stance: str
    changed: bool
    flip_allowed: bool
    action: str  # kept | adjusted | flipped | clamped | no_prior
    material_reasons: list[str] = field(default_factory=list)
    tier_distance: int = 0
    delta_snapshot: dict[str, Any] = field(default_factory=dict)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_stance(value: str | None, default: str = "Hold") -> str:
    raw = (value or "").strip()
    if not raw:
        return default
    if raw in _TIER_INDEX:
        return raw
    # Title-case English or parse CN/prose
    titled = raw[:1].upper() + raw[1:].lower() if raw.isalpha() else raw
    if titled in _TIER_INDEX:
        return titled
    return parse_rating(raw, default=default)


def tier_distance(a: str, b: str) -> int:
    ia = _TIER_INDEX.get(normalize_stance(a))
    ib = _TIER_INDEX.get(normalize_stance(b))
    if ia is None or ib is None:
        return 99
    return abs(ia - ib)


def material_change_reasons(
    delta: PlanDelta | None,
    *,
    route_reason: str | None = None,
    price_hard_threshold_pct: float = 5.0,
    plan_trade_date: str | None = None,
    analysis_trade_date: str | None = None,
) -> list[str]:
    """Return human-readable reasons that unlock large stance flips.

    Continuity lock applies only when data is effectively unchanged.
    A newer analysis/trade date than the archive plan is treated as material
    (new session / new bar), even if price fetch failed.
    """
    reasons: list[str] = []
    plan_d = str(plan_trade_date or "").strip()[:10]
    analysis_d = str(analysis_trade_date or "").strip()[:10]
    if plan_d and analysis_d and analysis_d > plan_d:
        reasons.append(f"新交易日 {plan_d}→{analysis_d}")

    if delta is not None:
        if delta.vs_stop == "breached":
            reasons.append("止损已破")
        if (
            delta.price_move_pct is not None
            and float(delta.price_move_pct) > float(price_hard_threshold_pct)
        ):
            reasons.append(f"价格波动 {delta.price_move_pct:.1f}%")
        for flag in delta.risk_flags or []:
            if flag == "stop_loss_breached" and "止损已破" in reasons:
                continue
            if flag.startswith("price_move>") and any(
                r.startswith("价格波动") for r in reasons
            ):
                continue
            if flag == "plan_stale":
                reasons.append("档案计划已过期")
            elif flag not in reasons:
                reasons.append(flag)
    if route_reason:
        base = str(route_reason).split(":", 1)[0].strip().lower()
        if base in _MATERIAL_ROUTE_REASONS:
            reasons.append(f"路由硬闸:{route_reason}")
    # de-dupe preserve order
    out: list[str] = []
    for r in reasons:
        if r not in out:
            out.append(r)
    return out


def resolve_stance_continuity(
    *,
    prior_stance: str | None,
    proposed_stance: str,
    delta: PlanDelta | None = None,
    route_reason: str | None = None,
    price_hard_threshold_pct: float = 5.0,
    max_step_without_material: int = MAX_STEP_WITHOUT_MATERIAL,
    plan_trade_date: str | None = None,
    analysis_trade_date: str | None = None,
) -> StanceContinuityResult:
    """Decide final stance vs archive prior (A+B)."""
    proposed = normalize_stance(proposed_stance)
    if not prior_stance:
        return StanceContinuityResult(
            prior_stance=None,
            proposed_stance=proposed,
            final_stance=proposed,
            changed=False,
            flip_allowed=True,
            action="no_prior",
            note="无档案计划，本轮评级不受连续性约束。",
            delta_snapshot=_delta_snapshot(delta),
        )

    prior = normalize_stance(prior_stance)
    dist = tier_distance(prior, proposed)
    reasons = material_change_reasons(
        delta,
        route_reason=route_reason,
        price_hard_threshold_pct=price_hard_threshold_pct,
        plan_trade_date=plan_trade_date,
        analysis_trade_date=analysis_trade_date,
    )
    flip_allowed = bool(reasons) or dist <= max(0, int(max_step_without_material))

    if proposed == prior:
        return StanceContinuityResult(
            prior_stance=prior,
            proposed_stance=proposed,
            final_stance=prior,
            changed=False,
            flip_allowed=True,
            action="kept",
            material_reasons=reasons,
            tier_distance=0,
            delta_snapshot=_delta_snapshot(delta),
            note=f"维持档案立场 {prior}。",
        )

    if flip_allowed:
        action = "flipped" if reasons else "adjusted"
        note = (
            f"相对档案 {prior} → {proposed}"
            + (f"（重大变化：{'；'.join(reasons)}）" if reasons else "（邻近档微调）")
        )
        return StanceContinuityResult(
            prior_stance=prior,
            proposed_stance=proposed,
            final_stance=proposed,
            changed=True,
            flip_allowed=True,
            action=action,
            material_reasons=reasons,
            tier_distance=dist,
            delta_snapshot=_delta_snapshot(delta),
            note=note,
        )

    note = (
        f"无重大数据变化，拦截越级翻转 {prior} → {proposed}"
        f"（跨 {dist} 档），维持档案立场 {prior}。"
    )
    return StanceContinuityResult(
        prior_stance=prior,
        proposed_stance=proposed,
        final_stance=prior,
        changed=False,
        flip_allowed=False,
        action="clamped",
        material_reasons=reasons,
        tier_distance=dist,
        delta_snapshot=_delta_snapshot(delta),
        note=note,
    )


def patch_decision_rating(text: str, rating: str) -> str:
    """Rewrite the explicit Rating line; prepend continuity note once."""
    rating = normalize_stance(rating)
    body = text or ""
    if _RATING_LINE_RE.search(body):
        body = _RATING_LINE_RE.sub(rf"\g<1>{rating}", body, count=1)
    elif _CN_RATING_LINE_RE.search(body):
        cn = {
            "Buy": "买入",
            "Overweight": "增持",
            "Hold": "持有",
            "Underweight": "减持",
            "Sell": "卖出",
        }.get(rating, rating)
        body = _CN_RATING_LINE_RE.sub(rf"\g<1>{cn}", body, count=1)
    else:
        body = f"**Rating**: {rating}\n\n{body}"
    return body


def apply_stance_continuity_to_state(
    final_state: dict[str, Any],
    *,
    ticker: str,
    market: str | None = None,
    archive_store: StockArchiveStore | None = None,
    current_price: float | None = None,
    route_reason: str | None = None,
    price_hard_threshold_pct: float = 5.0,
    trade_date: str | None = None,
) -> StanceContinuityResult:
    """Load archive plan, resolve continuity, mutate final_state in place."""
    t = (ticker or str(final_state.get("company_of_interest") or "")).strip().upper()
    mkt = (market or infer_market(t)).upper()
    store = archive_store or default_archive_store()
    analysis_date = str(
        trade_date or final_state.get("trade_date") or ""
    ).strip()[:10]

    plan = store.get_active_plan(t, mkt)
    # Lazy migrate from calibration if archive empty
    if plan is None:
        try:
            from tradingagents.analysis.calibration import default_calibration_store

            anchor = default_calibration_store().get(t, mkt)
            if anchor is not None:
                plan = store.ensure_from_baseline(anchor)
        except Exception:  # noqa: BLE001
            plan = None

    price = current_price
    if price is None or float(price) <= 0:
        try:
            from tradingagents.watchlist.snapshot import fetch_snapshot

            snap = fetch_snapshot(t, market=mkt, max_headlines=0)
            if snap.price and float(snap.price) > 0:
                price = float(snap.price)
        except Exception:  # noqa: BLE001
            price = None

    delta = store.get_delta(t, mkt) if plan is not None else None
    if plan is not None:
        try:
            delta = compute_plan_delta(
                plan,
                current_price=price,
                price_hard_threshold_pct=price_hard_threshold_pct,
            )
            store.save_delta(delta)
        except Exception:  # noqa: BLE001
            pass

    action_plan = final_state.get("action_plan")
    if isinstance(action_plan, dict) and action_plan.get("rating"):
        proposed = normalize_stance(str(action_plan.get("rating")))
    else:
        proposed = parse_rating(str(final_state.get("final_trade_decision") or ""))

    result = resolve_stance_continuity(
        prior_stance=plan.stance if plan else None,
        proposed_stance=proposed,
        delta=delta,
        route_reason=route_reason,
        price_hard_threshold_pct=price_hard_threshold_pct,
        plan_trade_date=plan.trade_date if plan else None,
        analysis_trade_date=analysis_date or None,
    )

    if result.action == "clamped":
        decision = str(final_state.get("final_trade_decision") or "")
        patched = patch_decision_rating(decision, result.final_stance)
        banner = f"> **立场连续性**：{result.note}\n\n"
        if "立场连续性" not in patched:
            patched = banner + patched
        final_state["final_trade_decision"] = patched
        if isinstance(action_plan, dict):
            action_plan = dict(action_plan)
            action_plan["rating"] = result.final_stance
            summary = str(action_plan.get("summary") or "")
            prefix = f"[连续性钳制] {result.note}"
            action_plan["summary"] = (
                f"{prefix} {summary}".strip() if summary else prefix
            )
            final_state["action_plan"] = action_plan
        # Keep risk judge mirror loosely consistent when present
        try:
            rds = final_state.get("risk_debate_state")
            if isinstance(rds, dict) and rds.get("judge_decision"):
                rds = dict(rds)
                rds["judge_decision"] = patch_decision_rating(
                    str(rds.get("judge_decision") or ""), result.final_stance
                )
                final_state["risk_debate_state"] = rds
        except Exception:  # noqa: BLE001
            pass

    final_state["stance_continuity"] = result.to_dict()
    return result


def _delta_snapshot(delta: PlanDelta | None) -> dict[str, Any]:
    if delta is None:
        return {}
    return {
        "current_price": delta.current_price,
        "price_move_pct": delta.price_move_pct,
        "vs_stop": delta.vs_stop,
        "vs_entry": delta.vs_entry,
        "risk_flags": list(delta.risk_flags or []),
        "vs_plan_version": delta.vs_plan_version,
    }
