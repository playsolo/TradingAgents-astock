"""价值波段选股策略 — L0/L1/L2 三阶漏斗筛选。

使用方法::

    from tradingagents.strategies.value_swing import run_value_swing_scan
    result = run_value_swing_scan()
    for candidate in result["candidates"]:
        print(candidate["code"], candidate["signal_score"])
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any

import pandas as pd
import requests as _requests

from tradingagents.dataflows.a_stock import (
    _build_name_code_map,
    _code_to_name,
    _em_get,
    _mootdx_client_session,
    _normalize_ticker,
    _requests,
    _tencent_quote,
    get_dragon_tiger_board,
    get_fund_flow,
    get_northbound_flow,
)

logger = logging.getLogger(__name__)


def _is_stock_excluded_by_prefix(code: str) -> bool:
    """检查股票代码是否因所属市场被排除。

    当前排除：北交所 (8xxx)
    包含：科创板 (688xxx)、主板、创业板等
    """
    return code.startswith("8")


# ── Configuration ──────────────────────────────────────────────────────────

# L0: 流动性底线（万元）
_MIN_VOLUME_WAN: float = 5000.0

# L0: 上市最短月数（新股暂不分析）
_MIN_LISTED_MONTHS: int = 6

# L1: 估值条件（满足 N 项即通过）
_PE_HIST_PCT_THRESHOLD: float = 0.30  # PE 全市场历史分位 ≤ 30%
_PB_MAX: float = 2.5  # PB ≤ 2.0 可再给一点容错
_PEG_MAX: float = 1.5  # PEG ≤ 1.5

# L1: 财务安全守卫
_MAX_DEBT_RATIO: float = 0.65  # 资产负债率 ≤ 65%
_MAX_DEBT_RATIO_STAR: float = 0.45  # 科创板 ≤ 45%
_MIN_REVENUE_GROWTH: float = 0.0  # 营收同比 > 0%
_MIN_AMPLITUDE_20D: float = 0.03  # 20 日日均振幅 > 3%

# L2: 催化剂信号权重（每项 +1）
_MAX_CANDIDATES: int = 15  # L2 最终上限

# SINA 财报获取
_SINA_FINANCE_URL = (
    "https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022"
)

# 扫描结果存储
_SCAN_RESULTS_FILE = "~/.tradingagents/scan_results.json"


# ── Data structures ────────────────────────────────────────────────────────


@dataclass
class StockInfo:
    """一只股票在筛选过程中的全部中间信息。"""
    code: str
    name: str
    # L0
    price: float = 0.0
    volume_wan: float = 0.0  # 成交额（万元）
    listed_months: int = 999
    pe_ttm: float = 0.0
    pb: float = 0.0
    # L1
    pe_hist_pct: float | None = None  # PE 历史分位
    peg: float | None = None  # PEG（同花顺一致预期计算）
    debt_ratio: float | None = None  # 资产负债率
    revenue_growth: float | None = None  # 营收同比
    amplitude_20d: float | None = None  # 20 日日均振幅
    # L2
    northbound_net_3d: float | None = None  # 北向 3 日净流入
    fund_flow_main_3d: float | None = None  # 主力 3 日净流入
    dragon_tiger_inst_net: float | None = None  # 龙虎榜机构净买入
    sector_rank_pct: float | None = None  # 所属板块近 5 日涨幅分位
    above_ma20: bool = False  # 是否站上 20 日均线
    near_ma250: bool = False  # 是否靠近 250 日均线
    signal_score: int = 0  # L2 催化剂总分
    # 排除原因
    exclude_reason: str = ""


@dataclass
class ScanResult:
    """一次扫描的完整结果。"""
    scan_date: str
    total_stocks: int = 0
    l0_passed: int = 0
    l1_passed: int = 0
    l2_passed: int = 0
    candidates: list[StockInfo] = field(default_factory=list)
    duration_seconds: float = 0.0


# ── L0: 全市场种子 ────────────────────────────────────────────────────────


def _get_all_cn_codes() -> list[str]:
    """从 mootdx 获取全 A 股票代码列表（仅 000/002/300/301/600/601/603/605）。"""
    n2c, c2n = _build_name_code_map()
    # _build_name_code_map 内部已过滤为 ^[036]\d{5}$
    codes = list(c2n.keys())
    codes.sort()
    return codes


def _estimate_listed_months(code: str) -> int:
    """利用 mootdx F10 粗略估算上市月数。

    通达信 F10 的 listing_date 不一定在 finance snapshot 中，
    这里简化为：若 mootdx finance 能拿到 IPO 年份（financing code 等），
    无法获取时返回 999（不因此误杀）。
    """
    try:
        with _mootdx_client_session() as client:
            fin = client.finance(symbol=code)
        if fin is not None and not (isinstance(fin, pd.DataFrame) and fin.empty):
            if isinstance(fin, pd.Series):
                fin = fin.to_frame().T
            for col in fin.columns:
                col_s = str(col).lower()
                if "listed" in col_s or "ipo" in col_s:
                    val = fin.iloc[0][col]
                    if val and str(val).strip() not in ("0", "0.0", ""):
                        try:
                            y = int(str(val).strip()[:4])
                            if 1990 <= y <= 2026:
                                return (datetime.now().year - y) * 12
                        except (ValueError, IndexError):
                            continue
    except Exception:
        pass
    return 999


def _tencent_volume_wan(price: float, turnover_pct: float, mcap_yi: float) -> float:
    """估算成交额（万元）= 总市值 * 换手率。总市值需从 亿 → 万。"""
    if price <= 0 or turnover_pct <= 0 or mcap_yi <= 0:
        return 0.0
    return mcap_yi * 10000 * (turnover_pct / 100)


def run_l0_filter() -> list[StockInfo]:
    """L0：全量种子过滤。

    1. 从 mootdx 获取全 A 代码列表
    2. 腾讯批量报价（一次 HTTP 可获取 5000+ 只的 PE/PB/成交额等）
    3. 剔除 ST / 上市不足 6 个月 / PE ≤0 / 成交额不足 / 科创板
    """
    all_codes = _get_all_cn_codes()
    logger.info("L0: 全量种子 %d 只", len(all_codes))

    # 腾讯批量报价
    # 腾讯 qt.gtimg.cn 单次请求可接受大量代码，但 URL 长度有限制。
    # 安全起见分 800 只一批。
    batch_size = 800
    all_quotes: dict[str, dict] = {}
    for i in range(0, len(all_codes), batch_size):
        batch = all_codes[i : i + batch_size]
        try:
            quotes = _tencent_quote(batch)
            all_quotes.update(quotes)
        except Exception as e:
            logger.warning("L0 腾讯报价 batch %d 失败: %s", i // batch_size, e)
        time.sleep(0.3)  # 礼貌间隔

    logger.info("L0: 腾讯报价返回 %d 只", len(all_quotes))

    passed: list[StockInfo] = []
    n2c, c2n = _build_name_code_map()

    for code in all_codes:
        q = all_quotes.get(code)
        if q is None:
            continue

        name = c2n.get(code, "")
        price = q.get("price", 0)
        pe_ttm = q.get("pe_ttm", 0)
        pb = q.get("pb", 0)
        mcap_yi = q.get("mcap_yi", 0)
        turnover_pct = q.get("turnover_pct", 0)
        change_pct = q.get("change_pct", 0)

        # 估算成交额
        vol_wan = _tencent_volume_wan(price, turnover_pct, mcap_yi)

        info = StockInfo(
            code=code,
            name=name,
            price=price,
            volume_wan=vol_wan,
            pe_ttm=pe_ttm,
            pb=pb,
        )

        # 剔除条件
        # 1. ST
        if "ST" in name.upper() or "退" in name:
            info.exclude_reason = "ST/退市"
            continue

        # 2. PE <= 0（亏损股）
        if pe_ttm <= 0:
            info.exclude_reason = f"PE(TTM)={pe_ttm:.1f}≤0"
            continue

        # 3. 成交额不足
        if vol_wan < _MIN_VOLUME_WAN:
            info.exclude_reason = f"成交额{vol_wan:.0f}万<{_MIN_VOLUME_WAN:.0f}万"
            continue

        # 4. 北交所 (8xxx)
        if _is_stock_excluded_by_prefix(code):
            info.exclude_reason = "北交所暂不纳入"
            continue

        # 5. 上市时间过滤（放宽：仅明显新股）
        listed_mos = _estimate_listed_months(code)
        info.listed_months = listed_mos
        if listed_mos < _MIN_LISTED_MONTHS:
            info.exclude_reason = f"上市仅{listed_mos}个月"
            continue

        passed.append(info)

    logger.info("L0: 通过 %d 只", len(passed))
    return passed


# ── L1: 价值/估值筛选 ──────────────────────────────────────────────────────


def _get_sina_financial(code: str, report_type: str) -> pd.DataFrame:
    """从新浪财经获取财务报表，返回 DataFrame。"""
    paper_code = f"{'sh' if code.startswith('6') else 'sz'}{code}"
    source_map = {"利润表": "lrb", "资产负债表": "fzb"}
    source = source_map.get(report_type, "lrb")
    params = {
        "paperCode": paper_code,
        "source": source,
        "type": "0",
        "page": "1",
        "num": "20",
    }
    try:
        r = _requests.get(
            _SINA_FINANCE_URL,
            params=params,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        d = r.json()
        result = d.get("result", {}).get("data", {})
        if not isinstance(result, dict):
            return pd.DataFrame()
        items = result.get(source, [])
        if isinstance(items, list) and items:
            return pd.DataFrame(items)
        report_list = result.get("report_list", {})
        if not report_list:
            return pd.DataFrame()
        rows = []
        for date_str, vals in report_list.items():
            if isinstance(vals, dict):
                vals["报告日"] = date_str
                rows.append(vals)
        return pd.DataFrame(rows) if rows else pd.DataFrame()
    except Exception as e:
        logger.warning("新浪财报获取失败 %s/%s: %s", code, report_type, e)
        return pd.DataFrame()


def _calc_revenue_growth(code: str) -> float | None:
    """计算最新年报营收同比。"""
    df = _get_sina_financial(code, "利润表")
    if df.empty or "营业收入" not in df.columns:
        return None
    try:
        # 找最新两期的营业收入
        revenues = []
        for _, row in df.iterrows():
            try:
                rev = float(row["营业收入"])
                if rev > 0:
                    revenues.append(rev)
            except (TypeError, ValueError):
                continue
        if len(revenues) >= 2:
            return (revenues[0] - revenues[1]) / revenues[1]
    except Exception:
        pass
    return None


def _calc_debt_ratio(code: str) -> float | None:
    """计算最新资产负债率。"""
    df = _get_sina_financial(code, "资产负债表")
    if df.empty:
        return None
    try:
        for _, row in df.iterrows():
            try:
                total_assets = float(row.get("资产总计", 0))
                total_liab = float(row.get("负债合计", 0))
                if total_assets > 0:
                    return total_liab / total_assets
            except (TypeError, ValueError):
                continue
    except Exception:
        pass
    return None


def _calc_pe_hist_pct(pe_ttm: float) -> float | None:
    """估算 PE 历史分位。

    简化：使用全市场 PE 排序后分位，而非个股历史分位。
    因为个股历史分位需要大量 K 线数据，性价比不高。
    此处用腾讯全市场报价的 PE 排序来估算"相对便宜程度"。
    若 pe_ttm ≤ 0（已排）或数据不足则返回 None。
    """
    if pe_ttm <= 0:
        return None
    # 这是一个粗略替代：PE 低于 15 视为较低分位
    # 全市场 PE 中位数大约 25~35，这里简单估算
    # PE ≤ 15 -> ~20% 分位, PE ≤ 25 -> ~50% 分位
    # 简化版本直接返回 pe_ttm 对应的相对位置
    if pe_ttm <= 10:
        return 0.10
    elif pe_ttm <= 15:
        return 0.20
    elif pe_ttm <= 20:
        return 0.35
    elif pe_ttm <= 30:
        return 0.50
    elif pe_ttm <= 50:
        return 0.70
    else:
        return 0.85


def _calc_peg(code: str, pe_ttm: float, price: float) -> float | None:
    """通过同花顺一致预期计算 PEG。

    需要最近两期的一致预期 EPS 均值。
    """
    try:
        # 复用 a_stock 中的同花顺 EPS 解析
        url = f"https://basic.10jqka.com.cn/new/{code}/worth.html"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "Chrome/117.0.0.0 Safari/537.36"
            ),
            "Referer": "https://basic.10jqka.com.cn/",
        }
        r = _requests.get(url, headers=headers, timeout=15)
        r.encoding = "gbk"
        html = r.text or ""
        if not html.strip() or "<table" not in html.lower():
            return None

        dfs = pd.read_html(html)
        eps_df = None
        for df in dfs:
            cols = [str(c) for c in df.columns]
            if any("每股收益" in c or "均值" in c for c in cols):
                eps_df = df
                break
        if eps_df is None and dfs:
            eps_df = dfs[0]
        if eps_df is None or eps_df.empty:
            return None

        eps_vals = []
        for col in eps_df.columns:
            if "均值" in str(col) or "平均" in str(col):
                for v in eps_df[col]:
                    try:
                        ev = float(v)
                        if ev > 0:
                            eps_vals.append(ev)
                    except (TypeError, ValueError):
                        continue

        if len(eps_vals) >= 2:
            cur_eps = eps_vals[0]
            nxt_eps = eps_vals[1]
            if cur_eps > 0 and nxt_eps > 0 and price > 0:
                fwd_pe = price / nxt_eps
                growth = (nxt_eps - cur_eps) / cur_eps
                if growth > 0:
                    return fwd_pe / (growth * 100)
    except Exception as e:
        logger.debug("PEG 计算失败 %s: %s", code, e)
    return None


def _calc_amplitude_20d(code: str) -> float | None:
    """计算 20 日日均振幅（新浪 K 线）。

    返回 20 个交易日的平均振幅（绝对值）。
    """
    try:
        from tradingagents.dataflows.a_stock import _sina_kline_fallback

        df = _sina_kline_fallback(code)
        if df is None or df.empty or len(df) < 5:
            return None
        recent = df.tail(min(20, len(df)))
        amplitudes = []
        for _, row in recent.iterrows():
            try:
                o, h, l_ = float(row["Open"]), float(row["High"]), float(row["Low"])
                if o > 0:
                    amp = (h - l_) / o
                    amplitudes.append(amp)
            except (TypeError, ValueError):
                continue
        if amplitudes:
            return sum(amplitudes) / len(amplitudes)
    except Exception as e:
        logger.debug("振幅计算失败 %s: %s", code, e)
    return None


def run_l1_filter_impl(stocks: list[StockInfo]) -> list[StockInfo]:
    """L1 纯逻辑筛选（不发起网络调用）。

    StockInfo 字段已预填充（如 revenue_growth, debt_ratio, amplitude_20d,
    pe_hist_pct, peg），直接判定。字段为 None 表示数据不可获取，不触发排除。

    估值条件：PB/PE/PEG 三项中至少满足两项。
    """
    passed: list[StockInfo] = []
    for info in stocks:
        # 1. 营收增长
        rev_growth = info.revenue_growth
        if rev_growth is not None and rev_growth < _MIN_REVENUE_GROWTH:
            info.exclude_reason = (
                f"营收同比{rev_growth * 100:.1f}%<{_MIN_REVENUE_GROWTH * 100:.0f}%"
            )
            continue

        # 2. 资产负债率
        debt_ratio = info.debt_ratio
        is_star = info.code.startswith("688")
        max_debt = _MAX_DEBT_RATIO_STAR if is_star else _MAX_DEBT_RATIO
        if debt_ratio is not None and debt_ratio > max_debt:
            info.exclude_reason = f"负债率{debt_ratio * 100:.1f}%>{max_debt * 100:.0f}%"
            continue

        # 3. 振幅
        amp = info.amplitude_20d
        if amp is not None and amp < _MIN_AMPLITUDE_20D:
            info.exclude_reason = (
                f"20日振幅{amp * 100:.1f}%<{_MIN_AMPLITUDE_20D * 100:.0f}%"
            )
            continue

        # 综合估值判定：满足三项中的两项
        pe_ok = info.pe_hist_pct is not None and info.pe_hist_pct <= _PE_HIST_PCT_THRESHOLD
        pb_ok = info.pb <= _PB_MAX
        peg_ok = info.peg is not None and info.peg <= _PEG_MAX

        criteria_met = sum([pe_ok, pb_ok, peg_ok])
        if criteria_met < 2:
            details = []
            if not pe_ok:
                details.append(f"PE分位={info.pe_hist_pct}")
            if not pb_ok:
                details.append(f"PB={info.pb:.1f}")
            if not peg_ok:
                details.append(f"PEG={info.peg}")
            info.exclude_reason = (
                f"估值条件不足({criteria_met}/2): {', '.join(details)}"
            )
            continue

        passed.append(info)

    return passed


def run_l1_filter(stocks: list[StockInfo]) -> list[StockInfo]:
    """L1：价值/估值筛选（含网络调用，填充字段后交由 _impl 判定）。

    依次获取：营收增长、负债率、振幅、PE分位、PEG，然后综合判定。
    """
    for info in stocks:
        logger.info("L1: %s %s", info.code, info.name)

        # TODO: 后续可改为批量获取以提升性能
        info.revenue_growth = _calc_revenue_growth(info.code)
        info.debt_ratio = _calc_debt_ratio(info.code)
        info.amplitude_20d = _calc_amplitude_20d(info.code)
        info.pe_hist_pct = _calc_pe_hist_pct(info.pe_ttm)
        if info.price > 0 and info.pe_ttm > 0:
            info.peg = _calc_peg(info.code, info.pe_ttm, info.price)

    passed = run_l1_filter_impl(stocks)
    logger.info("L1: 通过 %d 只", len(passed))
    return passed


# ── L2: 催化剂信号 ────────────────────────────────────────────────────────


def _check_northbound_3d(code: str) -> float | None:
    """检查北向资金是否连续正向流入（近 3 日累计）。

    返回累计净流入（亿元），None 表示数据不可获取。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        text = get_northbound_flow(today, include_history=True)
        # 解析北向文本：找 "Total=xxx" 行
        total = None
        for line in text.split("\n"):
            line = line.strip()
            if "Total=" in line:
                try:
                    total = float(line.split("Total=")[1].split()[0])
                except (ValueError, IndexError):
                    continue
        return total
    except Exception as e:
        logger.debug("北向数据获取失败: %s", e)
    return None


def _check_fund_flow_3d(code: str) -> float | None:
    """检查主力资金净流入（近 3 日累计）。

    返回主力净流入总额（万元），None 表示数据不可获取。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        text = get_fund_flow(code, today, include_history=True)
        # 找主力净流入
        total = None
        for line in text.split("\n"):
            line = line.strip()
            if "主力净流入=" in line:
                try:
                    # 格式: "主力净流入=xxx万元"
                    total = float(line.split("主力净流入=")[1].replace("万元", "").strip())
                except (ValueError, IndexError):
                    continue
        return total
    except Exception as e:
        logger.debug("资金流获取失败 %s: %s", code, e)
    return None


def _check_dragon_tiger_institution(code: str) -> float | None:
    """检查龙虎榜机构净买入。

    返回近 30 日机构净买入（万元），None 表示未上榜或无数据。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        text = get_dragon_tiger_board(code, today, look_back_days=30)
        for line in text.split("\n"):
            line = line.strip()
            if "机构买入" in line and "净额" in line:
                # 格式: "机构买入 xxx 万 | 卖出 yyy 万 | 净额 zzz 万"
                if "净额" in line:
                    try:
                        net = float(line.split("净额")[1].replace("万", "").strip())
                        if abs(net) < 100:  # <100 万忽略
                            return None
                        return net
                    except (ValueError, IndexError):
                        continue
    except Exception as e:
        logger.debug("龙虎榜获取失败 %s: %s", code, e)
    return None


def _check_sector_rank(code: str) -> float | None:
    """检查所属板块近 5 日涨幅排名（分位 0~1，越高越好）。

    完整实现需要行业横向对比接口，这里先简化。
    若无法获取则返回 None（不扣分也不加分）。
    """
    # 简化版：标记为不可用
    # 完整版会用 get_industry_comparison 实现
    return None


def _check_ma_support(code: str) -> tuple[bool, bool]:
    """检查均线状态。

    返回 (above_ma20, near_ma250)。
    """
    try:
        from tradingagents.dataflows.a_stock import _sina_kline_fallback

        df = _sina_kline_fallback(code)
        if df is None or df.empty or len(df) < 20:
            return (False, False)

        closes = pd.to_numeric(df["Close"], errors="coerce").dropna().values
        if len(closes) < 20:
            return (False, False)

        current_price = float(closes[-1])
        ma20 = float(closes[-20:].mean())

        above_ma20 = current_price >= ma20 * 0.98  # 允许 2% 误差

        near_ma250 = False
        if len(closes) >= 250:
            ma250 = float(closes[-250:].mean())
            if ma250 > 0:
                distance = abs(current_price - ma250) / ma250
                near_ma250 = distance < 0.08  # <8% 视为接近

        return (above_ma20, near_ma250)
    except Exception as e:
        logger.debug("均线检查失败 %s: %s", code, e)
    return (False, False)


def run_l2_filter_impl(
    stocks: list[StockInfo], max_candidates: int = _MAX_CANDIDATES,
) -> list[StockInfo]:
    """L2 纯逻辑评分（不发起网络调用）。

    使用 StockInfo 已预填的字段评分：
    - northbound_net_3d > 0  → +1
    - fund_flow_main_3d > 0  → +1
    - dragon_tiger_inst_net > 0 → +1（忽略 < 100 万）
    - sector_rank_pct > 0.5  → +1（预留）
    - above_ma20 → +1
    - near_ma250 → +1

    按分数降序排列，取前 max_candidates 只。
    """
    scored: list[StockInfo] = []
    for info in stocks:
        score = 0

        if info.northbound_net_3d is not None and info.northbound_net_3d > 0:
            score += 1

        if info.fund_flow_main_3d is not None and info.fund_flow_main_3d > 0:
            score += 1

        if info.dragon_tiger_inst_net is not None and info.dragon_tiger_inst_net > 0:
            if info.dragon_tiger_inst_net >= 100:
                score += 1

        if info.sector_rank_pct is not None and info.sector_rank_pct > 0.5:
            score += 1

        if info.above_ma20:
            score += 1

        if info.near_ma250:
            score += 1

        info.signal_score = score
        scored.append(info)

    scored.sort(key=lambda s: s.signal_score, reverse=True)
    return scored[:max_candidates]


def run_l2_filter(stocks: list[StockInfo], max_candidates: int = _MAX_CANDIDATES) -> list[StockInfo]:
    """L2：催化剂信号检测（含网络调用）。

    对每只 L1 通过个股，批量检测各项催化剂信号并评分。
    最终交由 _impl 计算分数并排序。
    """
    for info in stocks:
        logger.info("L2: %s %s", info.code, info.name)

        info.northbound_net_3d = _check_northbound_3d(info.code)
        info.fund_flow_main_3d = _check_fund_flow_3d(info.code)
        info.dragon_tiger_inst_net = _check_dragon_tiger_institution(info.code)
        info.sector_rank_pct = _check_sector_rank(info.code)
        above_ma20, near_ma250 = _check_ma_support(info.code)
        info.above_ma20 = above_ma20
        info.near_ma250 = near_ma250

    top = run_l2_filter_impl(stocks, max_candidates=max_candidates)

    for info in top:
        logger.info(
            "L2 候选: %s %s score=%d PE=%.1f PB=%.1f",
            info.code, info.name, info.signal_score, info.pe_ttm, info.pb,
        )

    logger.info("L2: 最终候选 %d 只", len(top))
    return top


# ── 扫描入口 ──────────────────────────────────────────────────────────────


def run_value_swing_scan(max_candidates: int = _MAX_CANDIDATES) -> ScanResult:
    """执行一完整的价值波段扫描。

    Args:
        max_candidates: L2 最终输出上限。

    Returns:
        ScanResult 包含分级结果。
    """
    start_ts = time.time()
    scan_date = datetime.now().strftime("%Y-%m-%d")
    result = ScanResult(scan_date=scan_date)

    logger.info("═══ 价值波段扫描 %s ═══", scan_date)

    # L0: 全市场种子
    l0_stocks = run_l0_filter()
    result.total_stocks = len(l0_stocks)
    result.l0_passed = len(l0_stocks)

    # L1: 价值筛选
    l1_stocks = run_l1_filter(l0_stocks)
    result.l1_passed = len(l1_stocks)

    if not l1_stocks:
        logger.info("L1 无候选, 提前结束")
        result.duration_seconds = time.time() - start_ts
        return result

    # L2: 催化剂评分
    l2_stocks = run_l2_filter(l1_stocks, max_candidates=max_candidates)
    result.l2_passed = len(l2_stocks)
    result.candidates = l2_stocks

    result.duration_seconds = time.time() - start_ts
    logger.info(
        "═══ 扫描完成: %d→%d→%d 候选, 耗时 %.0fs ═══",
        result.l0_passed, result.l1_passed, result.l2_passed,
        result.duration_seconds,
    )

    return result


# ── 推荐映射 ──────────────────────────────────────────────────────────────


SCAN_RECOMMENDATION_STRONG = "强烈推荐"
SCAN_RECOMMENDATION_BUY = "推荐"
SCAN_RECOMMENDATION_WATCH = "关注"


def map_scan_recommendation(pm_rating: str, signal_score: int) -> str:
    """将 PM 评级 + L2 信号分映射为扫描展示的中文推荐。"""
    rating_upper = pm_rating.strip().upper()

    if rating_upper in ("BUY",) and signal_score >= 4:
        return SCAN_RECOMMENDATION_STRONG
    if rating_upper in ("BUY", "OVERWEIGHT") and signal_score >= 2:
        return SCAN_RECOMMENDATION_BUY
    if rating_upper == "HOLD" and signal_score >= 3:
        return SCAN_RECOMMENDATION_WATCH
    return ""


def build_scan_summary_row(info: StockInfo, recommendation: str) -> dict[str, Any]:
    """构建扫描结果展示行。"""
    return {
        "code": info.code,
        "name": info.name,
        "recommendation": recommendation,
        "signal_score": info.signal_score,
        "pe_ttm": round(info.pe_ttm, 1),
        "pb": round(info.pb, 2),
        "peg": round(info.peg, 2) if info.peg else None,
        "price": info.price,
        "debt_ratio": round(info.debt_ratio * 100, 1) if info.debt_ratio else None,
        "revenue_growth": round(info.revenue_growth * 100, 1) if info.revenue_growth else None,
        "northbound_net_3d": round(info.northbound_net_3d, 2) if info.northbound_net_3d else None,
        "fund_flow_main_3d": round(info.fund_flow_main_3d) if info.fund_flow_main_3d else None,
        "dragon_tiger_inst_net": round(info.dragon_tiger_inst_net) if info.dragon_tiger_inst_net else None,
        "above_ma20": info.above_ma20,
        "near_ma250": info.near_ma250,
    }
