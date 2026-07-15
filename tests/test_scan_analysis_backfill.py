"""历史分析 → 扫描卡回填操作建议摘要。"""

from __future__ import annotations

import json
import os

from web import history


def _write_log(logs, ticker: str, date: str, state: dict, mtime: float):
    d = logs / ticker / "TradingAgentsStrategy_logs"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"full_states_log_{date}.json"
    path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def test_lookup_latest_action_plans_picks_newest_per_ticker(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    monkeypatch.setattr(history, "_results_dir", lambda: logs)

    _write_log(
        logs,
        "000651",
        "2026-07-10",
        {
            "action_plan": {
                "rating": "Hold",
                "holders_action": "持有",
                "non_holders_action": "观望",
                "horizon": "5日",
                "summary": "旧结论",
                "levels": {},
            }
        },
        1_700_000_000,
    )
    newer = _write_log(
        logs,
        "000651",
        "2026-07-14",
        {
            "action_plan": {
                "rating": "Buy",
                "holders_action": "持有或小幅加仓",
                "non_holders_action": "回踩可建仓",
                "horizon": "3-5个交易日",
                "summary": "估值仍具吸引力，波段可做。",
                "levels": {"stop_loss": 35.0},
            }
        },
        1_800_000_000,
    )
    _write_log(
        logs,
        "000002",
        "2026-07-14",
        {"final_trade_decision": "HOLD"},
        1_800_000_000,
    )

    plans = history.lookup_latest_action_plans(["000651", "000002", "600519"])
    assert set(plans) == {"000651", "000002"}
    assert plans["000651"]["rating"] == "Buy"
    assert plans["000651"]["horizon"] == "3-5个交易日"
    assert plans["000651"]["date"] == "2026-07-14"
    assert plans["000651"]["path"] == str(newer)
    assert "summary" in plans["000651"]
    # 无结构化 plan 时仍回填 signal
    assert plans["000002"]["rating"] is None
    assert plans["000002"]["signal"] == "Hold"


def test_attach_analysis_to_candidates_merges_by_code(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    monkeypatch.setattr(history, "_results_dir", lambda: logs)
    _write_log(
        logs,
        "000651",
        "2026-07-14",
        {
            "action_plan": {
                "rating": "Sell",
                "holders_action": "减仓",
                "non_holders_action": "不买入",
                "horizon": "1周",
                "summary": "风险加大",
                "levels": {},
            }
        },
        1_800_000_000,
    )
    from web.components.value_swing_scanner import attach_analysis_to_candidates

    rows = attach_analysis_to_candidates(
        [{"code": "000651", "signal_score": 5}, {"code": "600519", "signal_score": 3}]
    )
    assert rows[0]["analysis"]["rating"] == "Sell"
    assert "analysis" not in rows[1]
