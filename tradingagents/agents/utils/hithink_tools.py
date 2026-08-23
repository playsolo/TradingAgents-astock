from langchain_core.tools import tool
from typing import Annotated

from tradingagents.dataflows.interface import route_to_vendor


@tool
def get_market_sentiment(
    ticker: Annotated[str, "A-stock code (e.g. 688017)"],
    curr_date: Annotated[str, "Date YYYY-MM-DD (header context)"] = "",
) -> str:
    """
    Retrieve structured market attention metrics from HiThink (同花顺).
    Includes hot-stock rank/heat, skyrocket list membership, today's anomaly
    tags/keywords, and optional 30-day hot-rank trend.
    Requires HITHINK_ENABLED. When disabled, returns guidance to use get_news.
    """
    return route_to_vendor("get_market_sentiment", ticker, curr_date)


@tool
def get_auction_signal(
    ticker: Annotated[str, "A-stock code"],
) -> str:
    """
    Retrieve opening auction strength from HiThink: auction pct, volume ratio,
    turnover, unmatched volume, plus short-term market benchmark sample.
    """
    return route_to_vendor("get_auction_signal", ticker)


@tool
def get_financial_quality(
    ticker: Annotated[str, "A-stock code"],
) -> str:
    """
    Retrieve financial quality ratios from HiThink quarterly statements:
    cash conversion, FCF margin, accrual ratio, receivable pressure, net cash
    ratio, plus official five-category financial indicators when available.
    """
    return route_to_vendor("get_financial_quality", ticker)


@tool
def get_short_term_structure(
    ticker: Annotated[str, "A-stock code"],
    trade_date: Annotated[str, "Date YYYY-MM-DD"] = "",
) -> str:
    """
    Retrieve short-term market structure from HiThink: market limit-up/down/break
    counts, stock limit-up pool status, dragon-tiger org vs hot-money nets,
    and today's anomaly tag for the ticker.
    """
    return route_to_vendor("get_short_term_structure", ticker, trade_date)


@tool
def get_valuation_snapshot(
    ticker: Annotated[str, "A-stock code"],
) -> str:
    """
    Retrieve HiThink valuation snapshot: PE TTM/MRQ, PB, PS TTM, PCF TTM.
    Complements get_fundamentals with PS/PCF fields.
    """
    return route_to_vendor("get_valuation_snapshot", ticker)


@tool
def get_market_regime(
    curr_date: Annotated[str, "Date YYYY-MM-DD, empty for today"] = "",
) -> str:
    """
    Retrieve market-wide regime from HiThink: limit-up/down/break counts,
    hot-stock top list, dragon-tiger aggregate org/hot-money nets.
    No ticker required — use for environment/情绪温度计.
    """
    return route_to_vendor("get_market_regime", curr_date)
