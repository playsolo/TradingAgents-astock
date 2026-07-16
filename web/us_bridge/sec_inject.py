"""Patch US TradingAgents graph to expose ``get_sec_filings`` (EDGAR).

Must run after the US package is on ``sys.path`` and before
``TradingAgentsGraph(...)`` is constructed.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def install_sec_filing_tools(get_sec_filings_tool: Any) -> bool:
    """Monkeypatch news/fundamentals analysts + ToolNodes to include EDGAR tool.

    Returns True if patches applied.
    """
    try:
        from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
        from langgraph.prebuilt import ToolNode
        from tradingagents.agents.analysts import fundamentals_analyst as fa_mod
        from tradingagents.agents.analysts import news_analyst as na_mod
        from tradingagents.agents.utils.agent_utils import (
            get_balance_sheet,
            get_cashflow,
            get_fundamentals,
            get_global_news,
            get_income_statement,
            get_instrument_context_from_state,
            get_language_instruction,
            get_macro_indicators,
            get_news,
            get_prediction_markets,
        )
        from tradingagents.graph.trading_graph import TradingAgentsGraph
    except Exception as exc:  # noqa: BLE001
        logger.warning("SEC tool patch skipped (import): %s", exc)
        return False

    def create_news_analyst(llm):
        def news_analyst_node(state):
            current_date = state["trade_date"]
            asset_type = state.get("asset_type", "stock")
            asset_label = "company" if asset_type == "stock" else "asset"
            instrument_context = get_instrument_context_from_state(state)

            tools = [
                get_news,
                get_global_news,
                get_macro_indicators,
                get_prediction_markets,
                get_sec_filings_tool,
            ]

            system_message = (
                f"You are a news researcher tasked with analyzing recent news and trends over the past week. "
                f"Please write a comprehensive report of the current state of the world that is relevant for trading and macroeconomics. "
                f"Use get_news for {asset_label}-specific news, get_global_news for macro news, "
                f"get_macro_indicators for FRED data, get_prediction_markets for event probabilities, "
                f"and get_sec_filings(ticker) for the latest SEC 10-K/10-Q/8-K filings and excerpts "
                f"(especially around earnings). Prefer SEC filings over secondary news when they conflict."
                + """ Make sure to append a Markdown table at the end of the report to organize key points in the report, organized and easy to read."""
                + get_language_instruction()
            )

            prompt = ChatPromptTemplate.from_messages(
                [
                    (
                        "system",
                        "You are a helpful AI assistant, collaborating with other assistants."
                        " Use the provided tools to progress towards answering the question."
                        " If you are unable to fully answer, that's OK; another assistant with different tools"
                        " will help where you left off. Execute what you can to make progress."
                        " If you or any other assistant has the FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** or deliverable,"
                        " prefix your response with FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** so the team knows to stop."
                        " You have access to the following tools: {tool_names}."
                        " Today's date is {current_date}; treat it as 'now' for all analysis and tool-call date ranges. {instrument_context}\n"
                        "{system_message}",
                    ),
                    MessagesPlaceholder(variable_name="messages"),
                ]
            )
            prompt = prompt.partial(system_message=system_message)
            prompt = prompt.partial(tool_names=", ".join([tool.name for tool in tools]))
            prompt = prompt.partial(current_date=current_date)
            prompt = prompt.partial(instrument_context=instrument_context)
            chain = prompt | llm.bind_tools(tools)
            result = chain.invoke(state["messages"])
            report = ""
            if len(result.tool_calls) == 0:
                report = result.content
            return {"messages": [result], "news_report": report}

        return news_analyst_node

    def create_fundamentals_analyst(llm):
        def fundamentals_analyst_node(state):
            current_date = state["trade_date"]
            instrument_context = get_instrument_context_from_state(state)
            tools = [
                get_fundamentals,
                get_balance_sheet,
                get_cashflow,
                get_income_statement,
                get_sec_filings_tool,
            ]
            system_message = (
                "You are a researcher tasked with analyzing fundamental information over the past week about a company. "
                "Please write a comprehensive report of the company's fundamental information such as financial documents, "
                "company profile, basic company financials, and company financial history. "
                "Always call get_sec_filings for the latest 10-K/10-Q/8-K excerpts when analyzing around earnings or filings; "
                "combine them with get_fundamentals / financial statements. "
                "Make sure to append a Markdown table at the end of the report."
                + get_language_instruction()
            )
            prompt = ChatPromptTemplate.from_messages(
                [
                    (
                        "system",
                        "You are a helpful AI assistant, collaborating with other assistants."
                        " Use the provided tools to progress towards answering the question."
                        " If you are unable to fully answer, that's OK; another assistant with different tools"
                        " will help where you left off. Execute what you can to make progress."
                        " If you or any other assistant has the FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** or deliverable,"
                        " prefix your response with FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** so the team knows to stop."
                        " You have access to the following tools: {tool_names}."
                        " Today's date is {current_date}; treat it as 'now' for all analysis and tool-call date ranges. {instrument_context}\n"
                        "{system_message}",
                    ),
                    MessagesPlaceholder(variable_name="messages"),
                ]
            )
            prompt = prompt.partial(system_message=system_message)
            prompt = prompt.partial(tool_names=", ".join([tool.name for tool in tools]))
            prompt = prompt.partial(current_date=current_date)
            prompt = prompt.partial(instrument_context=instrument_context)
            chain = prompt | llm.bind_tools(tools)
            result = chain.invoke(state["messages"])
            report = ""
            if len(result.tool_calls) == 0:
                report = result.content
            return {"messages": [result], "fundamentals_report": report}

        return fundamentals_analyst_node

    na_mod.create_news_analyst = create_news_analyst
    fa_mod.create_fundamentals_analyst = create_fundamentals_analyst

    # Patch every import site GraphSetup may have bound at import time.
    for mod_name in (
        "tradingagents.agents",
        "tradingagents.graph.setup",
    ):
        try:
            import importlib

            mod = importlib.import_module(mod_name)
            if hasattr(mod, "create_news_analyst"):
                setattr(mod, "create_news_analyst", create_news_analyst)
            if hasattr(mod, "create_fundamentals_analyst"):
                setattr(mod, "create_fundamentals_analyst", create_fundamentals_analyst)
        except Exception:  # noqa: BLE001
            pass

    _orig_nodes = TradingAgentsGraph._create_tool_nodes

    def _create_tool_nodes(self) -> dict[str, ToolNode]:
        nodes = _orig_nodes(self)
        # Rebuild news + fundamentals ToolNodes with SEC tool appended.
        try:
            from tradingagents.agents.utils.agent_utils import (
                get_balance_sheet as _bs,
                get_cashflow as _cf,
                get_fundamentals as _gf,
                get_global_news as _gg,
                get_income_statement as _gi,
                get_insider_transactions as _git,
                get_macro_indicators as _gm,
                get_news as _gn,
                get_prediction_markets as _gp,
            )

            news_tools = [_gn, _gg, _git, _gm, _gp, get_sec_filings_tool]
            fund_tools = [_gf, _bs, _cf, _gi, get_sec_filings_tool]
            nodes["news"] = ToolNode(news_tools)
            nodes["fundamentals"] = ToolNode(fund_tools)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ToolNode SEC patch partial failure: %s", exc)
        return nodes

    TradingAgentsGraph._create_tool_nodes = _create_tool_nodes
    return True


def prefetch_filings_into_context(ticker: str, past_context: str, get_report) -> str:
    """Prepend a SEC filings block so analysts see filings even before tool calls."""
    try:
        report = get_report(ticker, form_types="10-K,10-Q,8-K", limit=2, include_excerpts=True)
    except Exception as exc:  # noqa: BLE001
        report = f"(SEC prefetch failed: {exc})"
    block = (
        "=== LATEST SEC FILINGS (EDGAR, injected) ===\n"
        f"{report}\n"
        "=== END SEC FILINGS ===\n"
    )
    past = (past_context or "").strip()
    if past:
        return block + "\n" + past
    return block
