from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from tradingagents.agents.utils.agent_utils import (
    build_instrument_context,
    get_balance_sheet,
    get_cashflow,
    get_financial_quality,
    get_fundamentals,
    get_income_statement,
    get_industry_comparison,
    get_language_instruction,
    get_profit_forecast,
    get_valuation_snapshot,
)
from tradingagents.dataflows.config import get_config


def create_fundamentals_analyst(llm):
    def fundamentals_analyst_node(state):
        current_date = state["trade_date"]
        instrument_context = build_instrument_context(state["company_of_interest"])

        tools = [
            get_fundamentals,
            get_balance_sheet,
            get_cashflow,
            get_income_statement,
            get_profit_forecast,
            get_industry_comparison,
            get_valuation_snapshot,
            get_financial_quality,
        ]

        system_message = (
            "你是一位专注于 A 股市场的基本面分析师。你的任务是全面分析目标公司的基本面信息，为投资决策提供扎实的数据支撑。"
            "\n\n⚠️ A 股基本面分析要点："
            "\n- **财务准则**：A 股上市公司采用中国会计准则（CAS），在收入确认、资产减值等方面与 IFRS 存在差异，分析时需注意口径。"
            "\n- **估值参照系**：A 股整体 PE 中位数偏高（30-50x 为常态），不能照搬美股 15-25x 标准；应对标同行业 A 股公司横向比较。"
            "\n- **核心指标**：重点关注营收增长率、归母净利润、扣非净利润（剔除非经常性损益）、ROE、毛利率、经营性现金流与净利润的匹配度。"
            "\n- **财报披露节奏**：一季报（4月底前）、半年报（8月底前）、三季报（10月底前）、年报（次年4月底前）。分析时注意数据的时效性。"
            "\n- **特殊风险关注**：商誉减值（并购后遗症）、股权质押比例、大股东减持计划、关联交易规模。"
            "\n\n请使用以下工具获取数据："
            "\n- `get_fundamentals`：获取公司综合基本面信息（PE/PB/总市值/季报财务快照/一致预期EPS/前向PE/PEG等）"
            "\n- `get_profit_forecast`：获取机构一致预期EPS详情（覆盖机构数、EPS区间、前向PE、PEG、PE消化时间）"
            "\n- `get_balance_sheet`：资产负债表详细数据"
            "\n- `get_cashflow`：现金流量表详细数据"
            "\n- `get_income_statement`：利润表详细数据"
            "\n- `get_industry_comparison(ticker, curr_date)`：获取全行业横向对比（90个行业涨跌幅/成交额/净流入排名，用于估值对标和行业定位）"
            "\n- `get_valuation_snapshot(ticker)`：HiThink 估值快照（PE TTM/MRQ、PB、PS、PCF）"
            "\n- `get_financial_quality(ticker)`：HiThink 财报质量（现金转化率、FCF率、应计利润率等）"
            "\n\n撰写详尽的基本面研究报告，给出具体数据支撑的分析结论（仅供研究参考，不构成投资建议）。报告末尾附 Markdown 表格汇总关键财务指标和估值水平。"
            "\n\n📋 必采清单 — 以下数据点必须出现在报告中；工具返回 "
            "「No analyst coverage / 无公开一致预期覆盖」是有效结论，"
            "请写「无公开一致预期覆盖」，**禁止**写成 `[数据缺失: …]`。"
            "股权质押、关联交易、商誉精确拆分等若工具未返回，用自然语言说明"
            "「公开数据未提供」即可，同样禁止 `[数据缺失]`。"
            "仅当工具调用失败、超时或返回 Error 时才标注 [数据缺失: xxx]："
            "\n1. PE（TTM）、PB、总市值"
            "\n2. 营收同比增长率"
            "\n3. 归母净利润及同比增长率"
            "\n4. ROE"
            "\n5. 资产负债率"
            "\n6. 经营性现金流与净利润比值"
            "\n7. PS（市销率）、PCF（市现率，调用 get_valuation_snapshot；未启用 HiThink 时用 get_fundamentals 已有 PE/PB）"
            "\n8. 现金转化率或 FCF 率（调用 get_financial_quality；未启用时可用三表自行估算）"
            "\n9. 机构一致预期 EPS（调用 get_profit_forecast；无覆盖写 EmptyOK 表述）"
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
                    " You have access to the following tools: {tool_names}.\n{system_message}"
                    "For your reference, the current date is {current_date}. {instrument_context}",
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

        return {
            "messages": [result],
            "fundamentals_report": report,
        }

    return fundamentals_analyst_node
