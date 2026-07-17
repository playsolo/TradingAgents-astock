"""成长加速扫描：序列化与落盘路径。"""

from __future__ import annotations

from pathlib import Path

from tradingagents.strategies.growth_accel import GrowthStockInfo, ScanResult
from tradingagents.strategies.scan_runner import result_dict_from_scan, run_scan_job
from tradingagents.strategies.scan_store import (
    SCAN_STATUS_COMPLETED,
    STRATEGY_GROWTH_ACCEL,
    ValueSwingScanStore,
)


def _fake_growth_scan(*, max_candidates: int = 15) -> ScanResult:
    return ScanResult(
        scan_date="2026-07-15",
        total_stocks=100,
        l0_passed=50,
        l1a_passed=30,
        l1b_passed=10,
        l2_passed=2,
        candidates=[
            GrowthStockInfo(
                code="300502",
                name="新易盛",
                price=200,
                pe_ttm=40,
                pb=8,
                signal_score=5,
                track="profit",
                np_ttm=2e9,
                np_ttm_yoy=1.2,
                growth_theme=True,
            ),
            GrowthStockInfo(
                code="300308",
                name="中际旭创",
                price=300,
                pe_ttm=50,
                pb=10,
                signal_score=4,
                track="profit",
                np_ttm=1e10,
                np_ttm_yoy=0.9,
            ),
        ][:max_candidates],
        duration_seconds=2.0,
    )


def test_growth_result_dict_shape():
    d = result_dict_from_scan(_fake_growth_scan(), strategy=STRATEGY_GROWTH_ACCEL)
    assert d["strategy"] == STRATEGY_GROWTH_ACCEL
    assert d["candidates"][0]["code"] == "300502"
    assert d["candidates"][0]["np_ttm_yoy"] == 120.0
    assert d["candidates"][0]["track"] == "profit"
    assert "why" in d["candidates"][0]
    assert "lane" in d["candidates"][0]
    assert "overextend_delta" in d["candidates"][0]
    assert "ret_5d" in d["candidates"][0]
    assert d["rules"]["strategy"] == STRATEGY_GROWTH_ACCEL


def test_growth_scan_job_uses_separate_store(tmp_path: Path):
    store = ValueSwingScanStore(
        path=tmp_path / "growth_accel_scan.json",
        archive_dir=tmp_path / "growth_accel_scans",
        strategy=STRATEGY_GROWTH_ACCEL,
    )
    record = run_scan_job(
        max_candidates=15,
        enqueue_on_success=False,
        scan_store=store,
        scan_fn=_fake_growth_scan,
        pid=4242,
        strategy=STRATEGY_GROWTH_ACCEL,
    )
    assert record["status"] == SCAN_STATUS_COMPLETED
    assert record["result"]["candidates"][0]["code"] == "300502"
    assert store.path.name == "growth_accel_scan.json"
