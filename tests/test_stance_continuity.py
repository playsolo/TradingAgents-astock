"""Stance continuity gate (A+B) and disclosure helpers (D)."""

from __future__ import annotations

from tradingagents.archive.continuity import (
    apply_stance_continuity_to_state,
    material_change_reasons,
    normalize_stance,
    patch_decision_rating,
    resolve_stance_continuity,
    tier_distance,
)
from tradingagents.archive.models import ActivePlan, PlanDelta
from tradingagents.archive.store import StockArchiveStore
from tradingagents.watchlist.models import Baseline


def _delta(**kwargs) -> PlanDelta:
    data = dict(
        ticker="BE",
        market="US",
        vs_plan_version=1,
        current_price=201.56,
        price_move_pct=0.5,
        vs_stop="ok",
        vs_entry="at",
        risk_flags=[],
    )
    data.update(kwargs)
    return PlanDelta(**data)


def test_tier_distance_overweight_to_sell():
    assert tier_distance("Overweight", "Sell") == 3
    assert tier_distance("Overweight", "Hold") == 1


def test_adjacent_adjust_allowed_without_material():
    r = resolve_stance_continuity(
        prior_stance="Overweight",
        proposed_stance="Hold",
        delta=_delta(),
    )
    assert r.action == "adjusted"
    assert r.final_stance == "Hold"
    assert r.flip_allowed is True


def test_be_style_flip_clamped_without_material():
    """Same price / no stop breach: Overweight → Sell must be clamped."""
    r = resolve_stance_continuity(
        prior_stance="Overweight",
        proposed_stance="Sell",
        delta=_delta(price_move_pct=0.2, vs_stop="ok"),
    )
    assert r.action == "clamped"
    assert r.final_stance == "Overweight"
    assert r.proposed_stance == "Sell"
    assert r.flip_allowed is False
    assert "拦截" in r.note


def test_stop_breach_allows_large_flip():
    r = resolve_stance_continuity(
        prior_stance="Overweight",
        proposed_stance="Sell",
        delta=_delta(vs_stop="breached", risk_flags=["stop_loss_breached"]),
    )
    assert r.action == "flipped"
    assert r.final_stance == "Sell"
    assert r.material_reasons


def test_price_move_allows_large_flip():
    reasons = material_change_reasons(
        _delta(price_move_pct=8.0, risk_flags=["price_move>5%"])
    )
    assert any("价格波动" in x or "price_move" in x for x in reasons)
    r = resolve_stance_continuity(
        prior_stance="Buy",
        proposed_stance="Sell",
        delta=_delta(price_move_pct=8.0),
    )
    assert r.final_stance == "Sell"
    assert r.action == "flipped"


def test_patch_decision_rating_english_and_banner():
    text = "**Rating**: Sell\n\n**Executive Summary**: 减仓"
    patched = patch_decision_rating(text, "Overweight")
    assert "**Rating**: Overweight" in patched
    assert "Sell" not in patched.splitlines()[0]


def test_apply_mutates_state_and_persists_meta(tmp_path):
    store = StockArchiveStore(tmp_path / "archives")
    store.save_from_baseline(
        Baseline(
            ticker="BE",
            trade_date="2026-07-31",
            market="US",
            stance="Overweight",
            position_pct=None,
            baseline_price=201.56,
            entry_price=201.56,
            stop_loss=178.72,
            thesis_summary="分批增持",
            major_risks=[],
            log_path="/tmp/x.json",
        ),
        bump_version=False,
    )
    state = {
        "company_of_interest": "BE",
        "final_trade_decision": "**Rating**: Sell\n\n减仓执行",
        "action_plan": {"rating": "Sell", "summary": "减仓"},
    }
    result = apply_stance_continuity_to_state(
        state,
        ticker="BE",
        market="US",
        archive_store=store,
        current_price=201.56,
    )
    assert result.action == "clamped"
    assert state["action_plan"]["rating"] == "Overweight"
    assert state["stance_continuity"]["final_stance"] == "Overweight"
    assert "**Rating**: Overweight" in state["final_trade_decision"]
    assert "立场连续性" in state["final_trade_decision"]


def test_stance_continuity_html_clamped():
    from web.components.report_viewer import stance_continuity_html

    html = stance_continuity_html(
        {
            "prior_stance": "Overweight",
            "proposed_stance": "Sell",
            "final_stance": "Overweight",
            "action": "clamped",
            "note": "拦截越级翻转",
            "material_reasons": [],
            "delta_snapshot": {"price_move_pct": 0.2, "vs_stop": "ok"},
        }
    )
    assert "立场连续性" in html
    assert "已拦截越级翻转" in html
    assert "Overweight" in html
    assert "Sell" in html


def test_normalize_stance_cn():
    assert normalize_stance("卖出") == "Sell"
    assert normalize_stance("增持") == "Overweight"
