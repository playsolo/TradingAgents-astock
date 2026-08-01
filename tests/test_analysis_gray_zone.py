"""灰区 LLM 软判解析与路由集成。"""

from __future__ import annotations

from datetime import date

from tradingagents.analysis.calibration import CalibrationStore
from tradingagents.analysis.gray_zone import (
    VOTE_FULL,
    VOTE_REUSE,
    VOTE_UNCERTAIN,
    GrayZoneVote,
    parse_gray_zone_response,
)
from tradingagents.analysis.mode_router import MODE_FULL, MODE_INCREMENTAL, resolve_analysis_mode
from tradingagents.archive.store import StockArchiveStore
from tradingagents.watchlist.models import Baseline


def _baseline(**kwargs) -> Baseline:
    data = dict(
        ticker="002648",
        trade_date="2026-07-13",
        market="CN",
        stance="Hold",
        position_pct=10.0,
        baseline_price=100.0,
        entry_price=98.0,
        stop_loss=90.0,
        thesis_summary="原逻辑仍在",
        major_risks=["解禁"],
        log_path="/tmp/x.json",
    )
    data.update(kwargs)
    return Baseline(**data)


def test_parse_gray_zone_reuse():
    vote = parse_gray_zone_response(
        '{"decision":"reuse","confidence":0.8,"reason":"波动正常"}'
    )
    assert vote.decision == VOTE_REUSE
    assert vote.confidence == 0.8
    assert vote.prefers_full is False


def test_parse_gray_zone_full_chinese():
    vote = parse_gray_zone_response('决策 {"decision":"全量","confidence":0.9,"reason":"催化失效"}')
    assert vote.decision == VOTE_FULL
    assert vote.prefers_full is True


def test_parse_gray_zone_uncertain_on_garbage():
    vote = parse_gray_zone_response("不是 json")
    assert vote.decision == VOTE_UNCERTAIN
    assert vote.prefers_full is True


def test_low_confidence_reuse_prefers_full():
    vote = GrayZoneVote(decision=VOTE_REUSE, confidence=0.4, reason="弱")
    assert vote.prefers_full is True


def test_gray_zone_without_llm_defaults_to_full(tmp_path):
    store = CalibrationStore(tmp_path / "a.json")
    store.save(_baseline())
    # 4% is between soft 3 and hard 5
    decision = resolve_analysis_mode(
        ticker="002648",
        trade_date="2026-07-14",
        calibration_store=store,
        archive_store=StockArchiveStore(tmp_path / "archives"),
        current_price=104.0,
        as_of=date(2026, 7, 14),
        seed_from_history=False,
        gray_zone_llm=None,
        price_soft_threshold_pct=3.0,
        price_threshold_pct=5.0,
    )
    assert decision.mode == MODE_FULL
    assert "gray_zone" in decision.reason


def test_gray_zone_llm_reuse_allows_incremental(tmp_path):
    store = CalibrationStore(tmp_path / "a.json")
    store.save(_baseline())

    class _LLM:
        def invoke(self, prompt):
            return type(
                "R",
                (),
                {"content": '{"decision":"reuse","confidence":0.85,"reason":"正常波动"}'},
            )()

    decision = resolve_analysis_mode(
        ticker="002648",
        trade_date="2026-07-14",
        calibration_store=store,
        archive_store=StockArchiveStore(tmp_path / "archives"),
        current_price=104.0,
        as_of=date(2026, 7, 14),
        seed_from_history=False,
        gray_zone_llm=_LLM(),
        price_soft_threshold_pct=3.0,
        price_threshold_pct=5.0,
    )
    assert decision.mode == MODE_INCREMENTAL
    assert decision.reason == "narrow_ok"


def test_hard_price_move_still_bypasses_gray(tmp_path):
    store = CalibrationStore(tmp_path / "a.json")
    store.save(_baseline())

    class _LLM:
        def invoke(self, prompt):
            raise AssertionError("should not call LLM on hard gate")

    decision = resolve_analysis_mode(
        ticker="002648",
        trade_date="2026-07-14",
        calibration_store=store,
        archive_store=StockArchiveStore(tmp_path / "archives"),
        current_price=106.0,
        as_of=date(2026, 7, 14),
        seed_from_history=False,
        gray_zone_llm=_LLM(),
        price_soft_threshold_pct=3.0,
        price_threshold_pct=5.0,
    )
    assert decision.mode == MODE_FULL
    assert decision.reason == "price_move"
