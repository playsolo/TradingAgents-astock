"""观察池：从分析状态抽取基准结论。"""

from tradingagents.watchlist.baseline import extract_baseline, parse_position_pct


def test_parse_position_pct_from_markdown():
    text = "**Position Sizing**: 12% of portfolio\n**Action**: Buy"
    assert parse_position_pct(text) == 12.0


def test_parse_position_pct_chinese():
    assert parse_position_pct("建议仓位：8%") == 8.0
    assert parse_position_pct("无仓位信息") is None


def test_extract_baseline_from_saved_state():
    state = {
        "final_trade_decision": "**Rating**: Overweight\n**Executive Summary**: 看好新能源",
        "trader_investment_decision": (
            "**Action**: Buy\n**Entry Price**: 50.2\n"
            "**Stop Loss**: 45.0\n**Position Sizing**: 10% of portfolio"
        ),
        "investment_plan": "**Recommendation**: Overweight\n",
        "action_plan": {
            "rating": "Overweight",
            "holders_action": "持有",
            "non_holders_action": "可买",
            "horizon": "3-5个交易日",
            "summary": "操作建议",
            "levels": {},
        },
    }
    base = extract_baseline(state, ticker="002648", trade_date="2026-07-13", price=51.0)
    assert base.ticker == "002648"
    assert base.stance == "Overweight"
    assert base.position_pct == 10.0
    assert base.entry_price == 50.2
    assert base.stop_loss == 45.0
    assert base.baseline_price == 51.0
    assert base.trade_date == "2026-07-13"
    assert base.horizon_raw == "3-5个交易日"
    assert base.valid_trading_days == 5


def test_extract_baseline_without_action_plan_uses_default_horizon():
    state = {
        "final_trade_decision": "**Rating**: Hold\n观望",
    }
    base = extract_baseline(state, ticker="300253", trade_date="2026-07-15")
    assert base.horizon_raw is None
    assert base.valid_trading_days == 3
