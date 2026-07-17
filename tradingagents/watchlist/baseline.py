"""从 full_states_log / final_state 抽取观察基准。"""

from __future__ import annotations

import re
from typing import Any

from tradingagents.agents.utils.rating import parse_rating
from tradingagents.watchlist.horizon import parse_horizon_trading_days
from tradingagents.watchlist.models import Baseline

_POS_RE = re.compile(
    r"(?:position\s*sizing|建议仓位|仓位)[\s*:：]+(\d+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)
_ENTRY_RE = re.compile(r"(?:entry\s*price|入场价|买入价)\s*[:：*]+\s*(\d+(?:\.\d+)?)", re.I)
_STOP_RE = re.compile(r"(?:stop\s*loss|止损)\s*[:：*]+\s*(\d+(?:\.\d+)?)", re.I)


def parse_position_pct(text: str) -> float | None:
    if not text:
        return None
    m = _POS_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _parse_float(pattern: re.Pattern[str], text: str) -> float | None:
    m = pattern.search(text or "")
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>\s*", "", text or "", flags=re.DOTALL).strip()


def _thesis_summary(state: dict[str, Any]) -> str:
    for key in ("final_trade_decision", "investment_plan", "trader_investment_decision"):
        raw = _strip_think(str(state.get(key) or ""))
        if not raw:
            continue
        for line in raw.splitlines():
            line = line.strip().lstrip("#").strip()
            if len(line) >= 12 and not line.lower().startswith(("rating", "**rating")):
                return line[:240]
    return ""


def extract_baseline(
    state: dict[str, Any],
    *,
    ticker: str,
    trade_date: str,
    price: float | None = None,
    log_path: str = "",
    market: str = "CN",
    major_risks: list[str] | None = None,
) -> Baseline:
    """从分析状态构建基准。优先 PM rating，仓位优先 trader markdown。"""
    pm = _strip_think(str(state.get("final_trade_decision") or ""))
    trader = _strip_think(
        str(
            state.get("trader_investment_decision")
            or state.get("trader_investment_plan")
            or ""
        )
    )
    plan = _strip_think(str(state.get("investment_plan") or ""))

    stance = parse_rating(pm or plan or trader, default="Hold")
    position = parse_position_pct(trader) or parse_position_pct(plan) or parse_position_pct(pm)

    horizon_raw, valid_days = _horizon_from_state(state)

    return Baseline(
        ticker=str(ticker).upper(),
        trade_date=trade_date,
        market=market,
        stance=stance,
        position_pct=position,
        baseline_price=price,
        entry_price=_parse_float(_ENTRY_RE, trader) or _parse_float(_ENTRY_RE, pm),
        stop_loss=_parse_float(_STOP_RE, trader) or _parse_float(_STOP_RE, pm),
        thesis_summary=_thesis_summary(state),
        major_risks=list(major_risks or []),
        log_path=log_path,
        horizon_raw=horizon_raw,
        valid_trading_days=valid_days,
    )


def _horizon_from_state(state: dict[str, Any]) -> tuple[str | None, int]:
    """从 ``action_plan.horizon`` 解析有效交易日数；缺省走默认回退。"""
    plan = state.get("action_plan")
    raw: str | None = None
    if isinstance(plan, dict):
        h = plan.get("horizon")
        if h is not None and str(h).strip():
            raw = str(h).strip()
    return raw, parse_horizon_trading_days(raw)
