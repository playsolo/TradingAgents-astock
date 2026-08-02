"""Build PM-facing prior text from archive plan + delta + same-ticker lessons."""

from __future__ import annotations

from tradingagents.archive.models import ActivePlan, Lesson, PlanDelta
from tradingagents.watchlist.models import Baseline
from tradingagents.watchlist.service import prior_context_from_baseline


def _format_lessons(lessons: list[Lesson]) -> str:
    lines = ["[同票已结算教训]"]
    for lesson in lessons:
        raw = f"{lesson.raw_return:+.1%}"
        alpha = f"{lesson.alpha_return:+.1%}"
        head = (
            f"- {lesson.trade_date} | {lesson.rating} | "
            f"raw {raw} / alpha {alpha} | {lesson.holding_days}d"
        )
        lines.append(head)
        if lesson.reflection:
            # Keep prompt compact — first 280 chars of reflection
            ref = lesson.reflection.replace("\n", " ").strip()
            if len(ref) > 280:
                ref = ref[:280] + "…"
            lines.append(f"  反思：{ref}")
    return "\n".join(lines)


def build_archive_prior(
    plan: ActivePlan,
    delta: PlanDelta | None = None,
    lessons: list[Lesson] | None = None,
) -> str:
    """Compact prior: plan + delta + same-ticker lessons. No cross-ticker."""
    parts: list[str] = []
    header = (
        f"[档案计划 | {plan.ticker} | 分析日 {plan.trade_date} | 立场 {plan.stance}"
        f" | v{plan.plan_version}"
    )
    if plan.position_pct is not None:
        header += f" | 仓位 {plan.position_pct:g}%"
    if plan.baseline_price is not None:
        header += f" | 基准价 {plan.baseline_price:g}"
    header += "]"
    parts.append(header)

    if plan.thesis_summary:
        parts.append(f"原逻辑：{plan.thesis_summary}")
    if plan.entry_price is not None:
        parts.append(f"入场价：{plan.entry_price:g}")
    if plan.stop_loss is not None:
        parts.append(f"止损：{plan.stop_loss:g}")
    if plan.invalidation:
        parts.append(f"失效条件：{plan.invalidation}")
    if plan.major_risks:
        parts.append("原重大风险：" + "；".join(plan.major_risks))
    if plan.watch_items:
        parts.append("关注清单：" + "；".join(plan.watch_items))

    if delta is not None:
        dlines = [f"[自上次以来的变更 | 对照计划 v{delta.vs_plan_version}]"]
        if delta.current_price is not None:
            dlines.append(f"现价：{delta.current_price:g}")
        if delta.price_move_pct is not None:
            dlines.append(f"相对基准价波动：{delta.price_move_pct:.2f}%")
        if delta.vs_stop != "unknown":
            dlines.append(f"止损状态：{delta.vs_stop}")
        if delta.vs_entry != "unknown":
            dlines.append(f"相对入场价：{delta.vs_entry}")
        if delta.risk_flags:
            dlines.append("风险标记：" + "；".join(delta.risk_flags))
        parts.append("\n".join(dlines))

    if lessons:
        parts.append(_format_lessons(lessons))

    parts.append(
        "请在以上档案计划与变更上做增量再评估，说明结论是否仍成立及需修正之处。"
        "（仅本票上下文，无跨票教训。）"
    )
    return "\n".join(parts)


def build_prior_for_baseline(
    baseline: Baseline,
    *,
    plan: ActivePlan | None = None,
    delta: PlanDelta | None = None,
) -> str:
    """Prefer archive prior when plan exists; else legacy calibration wording."""
    if plan is not None:
        return build_archive_prior(plan, delta)
    # Keep calibration label for pure-baseline fallback (tests / no archive yet)
    text = prior_context_from_baseline(baseline)
    lines = text.splitlines()
    if lines and lines[0].startswith("[观察池基准"):
        rest = lines[0].split("|", 1)[-1].strip().rstrip("]")
        lines[0] = f"[校准锚点 | {rest}]"
    else:
        lines.insert(
            0, f"[校准锚点 | {baseline.ticker} | 分析日 {baseline.trade_date}]"
        )
    return "\n".join(lines)
