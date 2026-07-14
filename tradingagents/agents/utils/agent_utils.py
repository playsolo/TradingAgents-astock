from langchain_core.messages import HumanMessage, RemoveMessage

# Import tools from separate utility files
from tradingagents.agents.utils.core_stock_tools import (
    get_stock_data
)
from tradingagents.agents.utils.technical_indicators_tools import (
    get_indicators
)
from tradingagents.agents.utils.fundamental_data_tools import (
    get_fundamentals,
    get_balance_sheet,
    get_cashflow,
    get_income_statement
)
from tradingagents.agents.utils.news_data_tools import (
    get_news,
    get_insider_transactions,
    get_global_news
)
from tradingagents.agents.utils.signal_data_tools import (
    get_profit_forecast,
    get_hot_stocks,
    get_northbound_flow,
    get_concept_blocks,
    get_fund_flow,
    get_dragon_tiger_board,
    get_lockup_expiry,
    get_industry_comparison,
)


def get_language_instruction() -> str:
    """Return a prompt instruction for the configured output language.

    Returns empty string when English (default), so no extra tokens are used.
    Only applied to user-facing agents (analysts, portfolio manager).
    Internal debate agents stay in English for reasoning quality.
    """
    from tradingagents.dataflows.config import get_config
    lang = get_config().get("output_language", "English")
    if lang.strip().lower() == "english":
        return ""
    return f" Write your entire response in {lang}."


def analysis_date_instruction(trade_date: str) -> str:
    """Force memo/report header dates to equal the analysis trade_date.

    Free-text Chinese models often invent D+1 as the document date (e.g. memo
    dated the next morning). They also treat incomplete intraday prints as
    a finished daily close. Anchor both problems to the graph's trade_date.
    """
    date = str(trade_date or "").strip()
    if not date:
        return ""
    return (
        f"\n\n**Analysis date (required)**: {date}. "
        "Any report header, memo date, decision date, or document date MUST equal "
        "this analysis date exactly. Do not invent the next calendar day. "
        f"When reports say 「今日」/「当日」, interpret them as {date}. "
        "Daily OHLCV often ends on the previous session during trading hours — "
        "if the last bar is before this analysis date, say so (数据截至上一交易日收盘) "
        "instead of inventing a completed close for the analysis date. "
        "Realtime quote / fund-flow figures for this analysis date are 盘中/intraday; "
        "do not label them as 收盘/closed day unless the source explicitly says the "
        "session has finished."
    )


def build_instrument_context(ticker: str) -> str:
    """Describe the exact instrument so agents preserve exchange-qualified tickers."""
    return (
        f"The instrument to analyze is `{ticker}`. "
        "Use this exact ticker in every tool call, report, and recommendation, "
        "preserving any exchange suffix (e.g. `.TO`, `.L`, `.HK`, `.T`). "
        "When a tool argument is named `ticker`, pass only this ticker value; "
        "do not pass company names, sectors, concepts, or search keywords."
    )

def create_msg_delete():
    def delete_messages(state):
        """Clear messages and add placeholder for Anthropic compatibility"""
        messages = state["messages"]

        # Remove all messages
        removal_operations = [RemoveMessage(id=m.id) for m in messages]

        # Add a minimal placeholder message
        placeholder = HumanMessage(content="Continue")

        return {"messages": removal_operations + [placeholder]}

    return delete_messages


        
