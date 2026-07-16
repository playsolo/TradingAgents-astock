from __future__ import annotations

from datetime import date, datetime

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


def clock_from_state(state: dict | None) -> datetime | None:
    """Parse frozen ``analysis_clock`` from graph state (Beijing ISO string)."""
    if not state:
        return None
    raw = state.get("analysis_clock")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


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
        f"When reports say 「今日」/「当日」 for market facts, interpret them as {date}. "
        "Daily OHLCV often ends on the previous session during trading hours — "
        "if the last bar is before this analysis date, say so (数据截至上一交易日收盘) "
        "instead of inventing a completed close for the analysis date. "
        "Realtime quote / fund-flow figures for this analysis date are 盘中/intraday; "
        "do not label them as 收盘/closed day unless the source explicitly says the "
        "session has finished."
    )


def catalyst_pricing_instruction() -> str:
    """Gate Buy ratings when positive news looks already priced in (利好兑现).

    Shared by news / bear / research manager / risk / portfolio manager so the
    rating cap stays consistent across the graph.
    """
    return (
        "\n\n**Catalyst pricing gate (利好兑现)**: "
        "Positive catalysts are often traded on expectation; confirmation can be "
        "a sell-the-news event. Explicitly classify pricing status as one of "
        "未定价 / 部分定价 / 已兑现, using evidence such as pre-event run-up, "
        "announcement vs rumor stage, volume divergence after a rally "
        "(放量滞涨), hot-money distribution/exit, and extreme consensus optimism. "
        "Rating cap: if status is 已兑现, Buy is forbidden — maximum rating is "
        "Overweight. If 已兑现 and (hot-money exit OR euphoric sentiment OR a "
        "sharp recent rally), prefer Hold or Underweight over Overweight. "
        "Momentum / limit-up arguments must not override this cap."
    )


def actionability_instruction(
    trade_date: str,
    now: datetime | None = None,
) -> str:
    """Inject A-share session so order verbs match the executable window.

    Live runs (``trade_date`` == Beijing today): wall-clock session phase.
    Retrospective (``trade_date`` before today): treat analysis day as closed;
    orders target the next trading day after that analysis date.
    """
    from tradingagents.watchlist.calendar import (
        CnSessionPhase,
        actionable_cn_trading_day,
        cn_session_phase,
        is_cn_trading_day,
        next_cn_trading_day,
        to_cn_datetime,
    )

    analysis_raw = str(trade_date or "").strip()
    now_cn = to_cn_datetime(now)
    today = now_cn.date()
    generated = now_cn.strftime("%Y-%m-%d %H:%M")

    analysis_date = None
    if analysis_raw:
        try:
            analysis_date = date.fromisoformat(analysis_raw[:10])
        except ValueError:
            analysis_date = None

    # Retrospective: analysis day already finished relative to generation clock
    if analysis_date is not None and analysis_date < today:
        actionable = next_cn_trading_day(analysis_date)
        actionable_iso = actionable.isoformat()
        analysis_iso = analysis_date.isoformat()
        return (
            f"\n\n**Execution timing (required)**:\n"
            f"- Generation time (Beijing): {generated}\n"
            f"- Mode: retrospective（分析日 {analysis_iso} 早于生成日）\n"
            f"- Market session: after_hours（按分析日已收盘处理）\n"
            f"- 分析日/事实日：{analysis_iso} — 「今日/当日」仅用于该日行情事实，"
            f"不得当作下单日。\n"
            f"- 可执行窗口：下一交易日（{actionable_iso}）开盘后。"
            f"禁止「今日追高/立即减仓」等当日下单措辞；"
            f"写「该日收盘后，于下一交易日（{actionable_iso}）开盘后…」。\n"
            f"- T+1「today」指买入发生的可执行交易日（{actionable_iso}）。"
        )

    # Forward-dated analysis: still no live fills until that session
    if analysis_date is not None and analysis_date > today:
        actionable = (
            analysis_date
            if is_cn_trading_day(analysis_date)
            else next_cn_trading_day(analysis_date)
        )
        actionable_iso = actionable.isoformat()
        analysis_iso = analysis_date.isoformat()
        return (
            f"\n\n**Execution timing (required)**:\n"
            f"- Generation time (Beijing): {generated}\n"
            f"- Mode: forward（分析日 {analysis_iso} 晚于生成日）\n"
            f"- Market session: pre_market\n"
            f"- 分析日/事实日：{analysis_iso} — 事实表述相对该日。\n"
            f"- 可执行窗口：分析日开盘后（可执行日 {actionable_iso}）；"
            f"勿按生成时刻的盘中/盘后写「今日立即」。\n"
            f"- T+1「today」指买入发生的可执行交易日（{actionable_iso}）。"
        )

    phase = cn_session_phase(now_cn)
    actionable = actionable_cn_trading_day(now_cn)
    actionable_iso = actionable.isoformat()
    analysis = analysis_raw or today.isoformat()

    if phase is CnSessionPhase.IN_SESSION:
        window = (
            f"当前连续竞价时段（可执行日 {actionable_iso}）。"
            "订单可用「今日/盘中/当前交易时段」表述。"
        )
    elif phase is CnSessionPhase.LUNCH_BREAK:
        window = (
            f"午休（可执行日仍为 {actionable_iso}）。"
            "订单写「午后开盘后/今日下午」，勿写仿佛此刻可立即成交的「立即下单」。"
        )
    elif phase is CnSessionPhase.PRE_MARKET:
        window = (
            f"盘前（可执行日 {actionable_iso}）。"
            "订单写「今日开盘后」，勿假设集合竞价/开盘前已可自由成交。"
        )
    elif phase is CnSessionPhase.AFTER_HOURS:
        window = (
            f"已收盘（下一可执行交易日 {actionable_iso}）。"
            "禁止把买入/卖出/减仓/追高写成「今日还可做」；"
            "勿写「切勿今日追高」「利用今日流动性立即减仓」等当日下单措辞；"
            f"一律改为「下一交易日（{actionable_iso}）开盘后」。"
        )
    else:
        window = (
            f"非交易日（下一可执行交易日 {actionable_iso}）。"
            f"订单一律指向「下一交易日（{actionable_iso}）开盘后」。"
        )

    return (
        f"\n\n**Execution timing (required)**:\n"
        f"- Generation time (Beijing): {generated}\n"
        f"- Market session: {phase.value}\n"
        f"- 分析日/事实日：{analysis} — 「今日/当日」仅用于该日行情事实"
        f"（如今日反弹、今日资金流），不得当作下单日。\n"
        f"- 可执行窗口：{window}\n"
        f"- T+1「today」指买入发生的可执行交易日（{actionable_iso}），"
        "若与分析日不同，勿混用。"
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


        
