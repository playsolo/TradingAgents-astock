"""错杀/反转选股策略 — 事件驱动 + 技术确认六阶漏斗。

L0   → 基础安全过滤（ST/北交所/流动性/上市时长，允许亏损）
L0.5 → 错杀甄别（营收未崩塌、经营现金流为正、负债可控、PB有安全垫）
L1   → 利空出尽确认（近30天负面公告 + 公告后未崩盘）
L1.5 → 资金预警（地量后温和放量 + 主力买/散户卖、北向未减持）
L2   → 底部反转确认（W底形态 + 缩量筑底 + 放量突破 + 主力资金转向）
L3   → 催化剂评估（行业共振 / 资产重估 / 管理行动 → 仓位/持仓建议）

与现有两条策略的关系：
- 价值波段：PE>0 + 估值约束 → 排除亏损股
- 成长加速：利润增速 + 亏损拐点 → 排除营收下滑股
- 本策略：捕捉利空出尽后的底部反转窗口，与以上两条完全正交
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
import requests

from tradingagents.dataflows.a_stock import (
    _build_name_code_map,
    _em_get,
    _tencent_quote,
)
from tradingagents.strategies.value_swing import (
    LANE_ANALYZE,
    _MIN_LISTED_MONTHS,
    _TENCENT_BATCH_SIZE,
    _calc_ret_nd,
    _estimate_listed_months,
    _get_all_cn_codes,
    _is_stock_code,
    _is_stock_excluded_by_prefix,
    _safe_call,
    _tencent_volume_wan,
)

logger = logging.getLogger(__name__)

# ── 可调参数 ──────────────────────────────────────────────────────────────────

# L0: 基础安全
_MIN_VOLUME_WAN: float = 3000.0
_MAX_PB_L0: float = 3.0          # L0 粗筛 PB < 3（留一点余地，L0.5 收紧）

# L0.5: 错杀甄别
_MIN_REVENUE_YOY: float = -0.20   # 营收同比 > -20%（未崩塌）
_MAX_DEBT_RATIO: float = 0.80     # 资产负债率 < 80%
_MAX_PB_L0_5: float = 2.0         # PB < 2（有安全垫）
_PB_ASSET_PLAY: float = 1.0       # PB < 1 视为资产重估型加分项
_L0_5_PROCESS_LIMIT: int = 400    # L0.5 深度财务检测上限（覆盖 PB≤1 错杀股）

# L1: 利空出尽
_MAX_CRASH_3D: float = 0.15       # 公告后3日跌幅 < 15%
_L1_API_BYPASS_CONSECUTIVE: int = 5  # 连续N只无负面公告 → API 不可用 → 放行
_NEGATIVE_KEYWORDS = (
    "预亏", "亏损", "减值", "计提", "业绩预告", "终止",
    "资产减值", "商誉减值", "坏账", "信用减值",
)

# L1.5: 资金预警 — 地量检测 + 主力/散户背离
_VOLUME_BOTTOM_RATIO: float = 0.7  # 底部5日均量 < 20日均量 × 0.7
_VOLUME_WARM_RATIO: float = 1.3    # 近3日均量 > 最低量 × 1.3（温和回暖）
_SMART_MONEY_DAYS_MIN: int = 2     # 近5日中至少N天主力净流入+散户净流出

# L2: 底部反转技术确认
_W_BOTTOM_WINDOW: int = 60         # W底检测窗口（交易日）
_W_LOW_PRICE_CLOSE: float = 0.05   # 两个低点价格差 < 5%
_W_MIN_GAP_DAYS: int = 5           # 双底间隔至少5个交易日
_W_PEAK_MARGIN: float = 0.03       # 中间峰高于双底 ≥ 3%
_VOLUME_BREAKOUT_RATIO: float = 1.5 # 放量突破：量 > 20日均量 × 1.5
_MAIN_FORCE_TURN_DAYS: int = 3     # 近N日超大单累计净流入 > 0

# L3: 催化剂
_INDUSTRY_RESONANCE_MIN: int = 3   # 同行业 ≥ N 只近1月涨幅 > 10%
_INDUSTRY_RESONANCE_LOOKBACK: int = 20  # 行业共振回溯交易日
_INDUSTRY_RESONANCE_THRESHOLD: float = 0.10  # 单只涨幅阈值

_L3_FACTOR_SPECS: tuple[tuple[str, str, bool], ...] = (
    ("industry_resonance", "行业共振（同板块多只走强）", True),
    ("asset_revalue", "资产重估潜力（PB<1破净）", True),
    ("management_action", "管理层/大股东增持信号", True),
    ("smart_money_active", "资金面预警（主力吸筹）", True),
    ("w_bottom_found", "W底技术形态确认", True),
    ("volume_breakout", "放量突破确认", True),
)

_MAX_CANDIDATES: int = 15

ProgressCb = Callable[[dict[str, Any]], None]
ItemCb = Callable[[str, str, int, int], None]

_PCT_L0_START = 2
_PCT_L0_DONE = 10
_PCT_L0_5_DONE = 35
_PCT_L1_DONE = 50
_PCT_L1_5_DONE = 70
_PCT_L2_DONE = 90
_PCT_L3_DONE = 100

_STAGE_LABELS = {
    "L0": "全市场基础筛选（含亏损股）",
    "L0.5": "错杀甄别（财务+估值安全垫）",
    "L1": "利空出尽确认（负面公告+股价韧性）",
    "L1.5": "资金预警（地量+主力吸筹）",
    "L2": "底部反转技术确认（W底+放量突破）",
    "L3": "催化剂评估",
    "done": "完成",
}


# ── 数据结构 ──────────────────────────────────────────────────────────────────

@dataclass
class TurnaroundStockInfo:
    code: str
    name: str = ""
    price: float = 0.0
    volume_wan: float = 0.0
    listed_months: int = 999
    pe_ttm: float = 0.0
    pb: float = 0.0

    # L0.5 财务（后置填充）
    revenue_yoy: float | None = None
    ocf_ttm: float | None = None     # 经营现金流 TTM
    np_ttm: float | None = None      # 净利润 TTM
    debt_ratio: float | None = None
    pb_safe: bool = False            # PB 有安全垫（< 2）

    # L1 利空出尽
    has_negative_news: bool = False
    news_crash_3d: float | None = None  # 公告后3日跌幅
    news_resilient: bool = False        # 股价未崩盘

    # L1.5 资金预警
    volume_bottomed: bool = False       # 地量筑底
    volume_warming: bool = False        # 温和放量
    smart_money_days: int = 0           # 主力买+散户卖的天数
    smart_money_active: bool = False    # 聪明钱活跃

    # L2 技术反转
    w_bottom_found: bool = False
    w_bottom_low1: float | None = None
    w_bottom_low2: float | None = None
    volume_contracted: bool = False     # 底部缩量
    volume_breakout: bool = False       # 放量突破
    neckline_break: bool = False        # 突破颈线
    main_force_turned: bool = False     # 主力资金转向
    above_ma20: bool = False
    ret_5d: float | None = None

    # L3 催化剂
    industry_resonance: bool = False
    industry_count: int = 0
    asset_revalue: bool = False         # PB < 1 破净资产
    management_action: bool = False     # 增持信号

    # 综合评分与分道
    signal_score: int = 0
    lane: str = LANE_ANALYZE
    exclude_reason: str = ""


@dataclass
class ScanResult:
    scan_date: str
    total_stocks: int = 0
    l0_passed: int = 0
    l0_5_passed: int = 0
    l1_passed: int = 0
    l1_5_passed: int = 0
    l2_passed: int = 0
    l3_passed: int = 0
    candidates: list[TurnaroundStockInfo] = field(default_factory=list)
    duration_seconds: float = 0.0
    strategy: str = "turnaround"


# ── L0: 基础安全过滤（含亏损股）──────────────────────────────────────────────


def run_l0_filter() -> list[TurnaroundStockInfo]:
    """L0：全市场过滤 — ST/流动性/北交所/上市时长，允许 PE≤0。

    额外条件：PB < 3（极高估值的"反弹"不是我们想要的）。
    """
    all_codes = _get_all_cn_codes()
    logger.info("反转 L0: 全量种子 %d 只", len(all_codes))

    all_quotes: dict[str, dict] = {}
    for i in range(0, len(all_codes), _TENCENT_BATCH_SIZE):
        batch = all_codes[i: i + _TENCENT_BATCH_SIZE]
        try:
            all_quotes.update(_tencent_quote(batch))
        except Exception as e:
            logger.warning("反转 L0 腾讯报价 batch %d 失败: %s", i // _TENCENT_BATCH_SIZE, e)
        time.sleep(0.25)

    passed: list[TurnaroundStockInfo] = []
    _, c2n = _build_name_code_map()

    for code in all_codes:
        q = all_quotes.get(code)
        if q is None:
            continue
        name = c2n.get(code, "")
        price = q.get("price", 0)
        pe_ttm = q.get("pe_ttm", 0) or 0
        pb = q.get("pb", 0) or 0
        vol_wan = _tencent_volume_wan(price, q.get("turnover_pct", 0), q.get("mcap_yi", 0))

        info = TurnaroundStockInfo(
            code=code, name=name, price=price, volume_wan=vol_wan, pe_ttm=pe_ttm, pb=pb,
        )

        # Hard excludes
        if "ST" in name.upper() or "退" in name:
            continue
        if vol_wan < _MIN_VOLUME_WAN:
            continue
        if _is_stock_excluded_by_prefix(code):
            continue
        if not _is_stock_code(code):
            continue
        listed = _estimate_listed_months(code)
        info.listed_months = listed
        if listed < _MIN_LISTED_MONTHS:
            continue

        # Turnaround-specific: PB < 3 (keep valuation floor for beaten-down stocks)
        if pb > _MAX_PB_L0 or pb <= 0:
            continue

        passed.append(info)

    # Sort by PB ascending (cheapest first) — beaten-down stocks are our hunting ground
    passed.sort(key=lambda s: s.pb if s.pb > 0 else 999)
    logger.info("反转 L0: 通过 %d 只", len(passed))
    return passed


# ── L0.5: 错杀甄别（财务 + 估值安全垫）───────────────────────────────────────

_SINA_FIN_SESSION: requests.Session | None = None


def _sina_fin_session() -> requests.Session:
    global _SINA_FIN_SESSION
    if _SINA_FIN_SESSION is None:
        _SINA_FIN_SESSION = requests.Session()
        _SINA_FIN_SESSION.headers.update({"User-Agent": "Mozilla/5.0"})
    return _SINA_FIN_SESSION


def _get_sina_financial(code: str, report_type: str) -> pd.DataFrame:
    """从新浪财经获取财务报表。"""
    source_map = {"利润表": "lrb", "资产负债表": "fzb", "现金流量表": "llb"}
    source = source_map.get(report_type, "lrb")
    paper_code = f"{'sh' if code.startswith('6') else 'sz'}{code}"
    params = {
        "paperCode": paper_code,
        "source": source,
        "type": "0",
        "page": "1",
        "num": "10",
    }
    try:
        r = _sina_fin_session().get(
            "https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022",
            params=params,
            timeout=8,
        )
        d = r.json()
        result = d.get("result", {}).get("data", {})
        if not isinstance(result, dict):
            return pd.DataFrame()
        items = result.get(source, [])
        if isinstance(items, list) and items:
            return pd.DataFrame(items)
        return pd.DataFrame()
    except Exception as e:
        logger.debug("新浪财报失败 %s/%s: %s", code, report_type, e)
        return pd.DataFrame()


def _calc_revenue_yoy(code: str) -> float | None:
    """计算最新营业收入同比（净利润表）。"""
    df = _get_sina_financial(code, "利润表")
    if df.empty:
        return None
    try:
        rev_col = "营业收入" if "营业收入" in df.columns else None
        if rev_col is None:
            # Try to find it
            for col in df.columns:
                if "营业" in str(col) and "收入" in str(col):
                    rev_col = col
                    break
        if rev_col is None:
            return None
        revenues = []
        for _, row in df.iterrows():
            try:
                v = float(row[rev_col])
                if v > 0:
                    revenues.append(v)
            except (TypeError, ValueError, KeyError):
                continue
        if len(revenues) >= 2:
            return (revenues[0] - revenues[1]) / revenues[1]
    except Exception:
        pass
    return None


def _calc_debt_ratio(code: str) -> float | None:
    """计算资产负债率。"""
    df = _get_sina_financial(code, "资产负债表")
    if df.empty:
        return None
    try:
        asset_col = "资产总计" if "资产总计" in df.columns else None
        liab_col = "负债合计" if "负债合计" in df.columns else None
        if asset_col is None or liab_col is None:
            return None
        for _, row in df.iterrows():
            try:
                assets = float(row[asset_col])
                liab = float(row[liab_col])
                if assets > 0:
                    return liab / assets
            except (TypeError, ValueError):
                continue
    except Exception:
        pass
    return None


def _calc_ocf_ttm(code: str) -> float | None:
    """从现金流量表获取经营活动现金流净额（最新一期）。"""
    df = _get_sina_financial(code, "现金流量表")
    if df.empty:
        return None
    try:
        ocf_col = None
        for col in df.columns:
            col_str = str(col)
            if "经营" in col_str and "现金流" in col_str:
                ocf_col = col
                break
        if ocf_col is None:
            return None
        for _, row in df.iterrows():
            try:
                v = float(row[ocf_col])
                return v  # Return most recent
            except (TypeError, ValueError):
                continue
    except Exception:
        pass
    return None


def run_l0_5_filter(
    stocks: list[TurnaroundStockInfo],
    *,
    on_item: ItemCb | None = None,
) -> list[TurnaroundStockInfo]:
    """L0.5：错杀甄别 — 财务质量 + 估值安全垫。

    检查：营收未崩塌（> -20%）、经营现金流为正、资产负债率 < 80%、PB < 2。
    仅对前 _L0_5_PROCESS_LIMIT 只做深度财务（按 PB 从低到高）。
    """
    sample = stocks[:_L0_5_PROCESS_LIMIT]
    total = len(sample)
    passed: list[TurnaroundStockInfo] = []

    for idx, info in enumerate(sample, 1):
        if on_item is not None:
            on_item(info.code, info.name, idx, total)

        # Check PB safety pad first (cheapest to check)
        if info.pb > _MAX_PB_L0_5 or info.pb <= 0:
            info.exclude_reason = f"PB {info.pb:.2f} 无安全垫"
            continue
        info.pb_safe = True

        # Asset revalue flag: PB < 1
        if info.pb < _PB_ASSET_PLAY:
            info.asset_revalue = True

        # Financial checks (HTTP, fail-closed on error)
        try:
            info.revenue_yoy = _calc_revenue_yoy(info.code)
        except Exception:
            info.revenue_yoy = None

        if info.revenue_yoy is not None and info.revenue_yoy < _MIN_REVENUE_YOY:
            info.exclude_reason = f"营收同比 {info.revenue_yoy * 100:.1f}%（< {-_MIN_REVENUE_YOY * 100:.0f}%）"
            continue

        try:
            info.debt_ratio = _calc_debt_ratio(info.code)
        except Exception:
            info.debt_ratio = None

        if info.debt_ratio is not None and info.debt_ratio > _MAX_DEBT_RATIO:
            info.exclude_reason = f"负债率 {info.debt_ratio * 100:.1f}%（> {_MAX_DEBT_RATIO * 100:.0f}%）"
            continue

        try:
            info.ocf_ttm = _calc_ocf_ttm(info.code)
        except Exception:
            info.ocf_ttm = None

        if info.ocf_ttm is not None and info.ocf_ttm <= 0:
            info.exclude_reason = "经营现金流为负（现金失血）"
            continue

        # If financials are unavailable, be lenient (fail-open for missing data)
        passed.append(info)

    logger.info(
        "反转 L0.5: 检测 %d 只, 通过 %d 只",
        total,
        len(passed),
    )
    return passed


# ── L1: 利空出尽确认 ──────────────────────────────────────────────────────────

def _check_negative_announcement(code: str) -> tuple[bool, float | None]:
    """检查近30天是否有负面公告，并计算公告后3日股价跌幅。

    返回 (has_negative_news, crash_3d_pct)。
    通过东方财富个股公告接口检测。
    """
    today = datetime.now()
    start_date = (today - timedelta(days=30)).strftime("%Y-%m-%d")
    end_date = today.strftime("%Y-%m-%d")

    try:
        url = (
            "https://np-weblist.eastmoney.com/comm/web/getStockAnnouncement"
        )
        resp = _em_get(
            url,
            params={
                "stockCode": code,
                "pageIndex": "1",
                "pageSize": "20",
            },
            timeout=8,
        )
        data = resp.json()
        items = (data.get("data") or {}).get("list") or []
    except Exception:
        # Fallback: try search-api
        try:
            url2 = "https://search-api.eastmoney.com/bussiness/Web/GetCMSSearchResult"
            resp2 = _em_get(
                url2,
                params={
                    "type": "8196",
                    "pageindex": "1",
                    "pagesize": "5",
                    "keyword": code,
                    "name": "zixun",
                },
                timeout=8,
            )
            data2 = resp2.json()
            items = data2.get("Data") or []
        except Exception:
            return False, None

    has_negative = False
    for item in items:
        title = str(item.get("Title") or item.get("title") or "")
        for kw in _NEGATIVE_KEYWORDS:
            if kw in title:
                has_negative = True
                break
        if has_negative:
            break

    if not has_negative:
        return False, None

    # Check stock reaction after announcement
    # Simplified: check 5d return for overextension
    crash_3d = None
    try:
        ret_5d = _calc_ret_nd(code, n=5)
        if ret_5d is not None:
            # Use 5d return as proxy for post-announcement reaction
            crash_3d = max(0, -ret_5d) if ret_5d < 0 else 0.0
    except Exception:
        pass

    return has_negative, crash_3d


def run_l1_news_filter(
    stocks: list[TurnaroundStockInfo],
    *,
    on_item: ItemCb | None = None,
) -> list[TurnaroundStockInfo]:
    """L1：利空出尽确认 — 近30天负面公告 + 股价未崩盘。

    防公告 API 失效：若连续 N 只股票都无负面公告，判定 API 不可用，
    整段放行，交由 L1.5/L2 的技术面做真正筛选。
    """
    total = len(stocks)
    passed: list[TurnaroundStockInfo] = []
    bypass_triggered: bool = False
    consecutive_no_news: int = 0

    for idx, info in enumerate(stocks, 1):
        if on_item is not None:
            on_item(info.code, info.name, idx, total)

        has_neg, crash = _safe_call(
            lambda c: _check_negative_announcement(c),
            info.code,
            default=(False, None),
        )

        info.has_negative_news = bool(has_neg)
        info.news_crash_3d = crash

        if not info.has_negative_news:
            consecutive_no_news += 1
            # 连续 N 只无负面 + 零通过 → API 大概率挂了
            if consecutive_no_news >= _L1_API_BYPASS_CONSECUTIVE and len(passed) == 0:
                logger.warning(
                    "反转 L1: 连续 %d 只无负面公告，公告 API 不可用，L1 全部放行",
                    _L1_API_BYPASS_CONSECUTIVE,
                )
                bypass_triggered = True
                # 放行已检测的全部股票（包括被排除的）
                for prev in stocks[:idx]:
                    prev.exclude_reason = None
                    prev.has_negative_news = False
                    prev.news_crash_3d = None
                passed = list(stocks[:idx])
                # 剩余股票不再检查
                for later in stocks[idx:]:
                    later.has_negative_news = False
                    later.news_crash_3d = None
                passed = list(stocks)
                break
            info.exclude_reason = "无近期负面公告"
            continue

        # 找到有负面公告的股票，重置计数
        consecutive_no_news = 0

        if crash is not None and crash >= _MAX_CRASH_3D:
            info.exclude_reason = f"公告后跌幅 {crash * 100:.1f}%（≥ {_MAX_CRASH_3D * 100:.0f}%）"
            continue

        info.news_resilient = True
        passed.append(info)

    # 兜底：循环正常结束但 0 通过 → 也是 API 不可用
    if not bypass_triggered and len(passed) == 0 and total > 0:
        all_no_news = all(
            getattr(info, "exclude_reason", "") == "无近期负面公告"
            for info in stocks
        )
        if all_no_news:
            logger.warning(
                "反转 L1: 全部 %d 只均无负面公告，公告 API 不可用，L1 全部放行",
                total,
            )
            for info in stocks:
                info.exclude_reason = None
                info.has_negative_news = False
                info.news_crash_3d = None
            passed = list(stocks)

    logger.info("反转 L1: 检测 %d 只, 通过 %d 只", total, len(passed))
    return passed


# ── L1.5: 资金预警（地量+主力吸筹）───────────────────────────────────────────

_push2his_failed: bool = False  # push2his API 不可用时跳过资金流查询


def _check_fund_flow_smart(code: str) -> tuple[bool, bool, int]:
    """检查近5日是否呈现"主力吸筹"特征。

    返回 (volume_bottomed, volume_warming, smart_money_days)。
    """
    global _push2his_failed
    try:
        from tradingagents.dataflows.a_stock import _sina_kline_fallback

        df = _sina_kline_fallback(code)
        if df is None or df.empty or "Close" not in df.columns or "Volume" not in df.columns:
            return False, False, 0

        volumes = []
        for _, r in df.iterrows():
            try:
                volumes.append(float(r["Volume"]))
            except (TypeError, ValueError):
                continue

        if len(volumes) < 20:
            return False, False, 0

        recent_vol = volumes[-5:]
        ma20_vol = sum(volumes[-20:]) / 20
        avg5_vol = sum(recent_vol) / 5
        min_vol = min(volumes[-20:])

        bottomed = avg5_vol < ma20_vol * _VOLUME_BOTTOM_RATIO
        warming = avg5_vol > min_vol * _VOLUME_WARM_RATIO

    except Exception:
        return False, False, 0

    # Check fund flow for smart money pattern
    smart_days = 0
    if not _push2his_failed:
        try:
            # Use EastMoney fund flow
            url = (
                "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
            )
            resp = _em_get(
                url,
                params={
                    "secid": f"1.{code}",
                    "fields1": "f1,f2,f3,f7",
                    "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
                    "lmt": "5",
                },
                timeout=4,
            )
            data = resp.json()
            klines = (data.get("data") or {}).get("klines") or []

            for line in klines[-5:]:
                parts = line.split(",")
                if len(parts) < 6:
                    continue
                try:
                    main_net = float(parts[1])   # 主力净流入
                    retail_net = float(parts[7])  # 散户净流入（index may vary）
                    if main_net > 0 and retail_net < 0:
                        smart_days += 1
                except (ValueError, IndexError):
                    continue
        except Exception:
            _push2his_failed = True
            logger.warning("push2his 资金流 API 不可用，后续 L1.5 跳过资金流查询")

    return bottomed, warming, smart_days


def run_l1_5_money_filter(
    stocks: list[TurnaroundStockInfo],
    *,
    on_item: ItemCb | None = None,
) -> list[TurnaroundStockInfo]:
    """L1.5：资金预警 — 地量筑底 + 主力吸筹信号。"""
    total = len(stocks)
    passed: list[TurnaroundStockInfo] = []

    for idx, info in enumerate(stocks, 1):
        if on_item is not None:
            on_item(info.code, info.name, idx, total)

        bottomed, warming, smart_days = _safe_call(
            lambda c: _check_fund_flow_smart(c),
            info.code,
            default=(False, False, 0),
        )

        info.volume_bottomed = bool(bottomed)
        info.volume_warming = bool(warming)
        info.smart_money_days = int(smart_days)
        info.smart_money_active = smart_days >= _SMART_MONEY_DAYS_MIN

        if not info.volume_bottomed and not info.smart_money_active:
            info.exclude_reason = "无量价/资金预警信号"
            continue

        passed.append(info)

    logger.info("反转 L1.5: 检测 %d 只, 通过 %d 只", total, len(passed))
    return passed


# ── L2: 底部反转技术确认 ──────────────────────────────────────────────────────


def _detect_w_bottom(closes, *, window: int = _W_BOTTOM_WINDOW) -> tuple[bool, float | None, float | None, float | None]:
    """检测 W 双底形态。

    返回 (has_pattern, low1, low2, neckline_peak)。
    """
    if len(closes) < window:
        return False, None, None, None

    recent = list(closes[-window:])
    n = len(recent)

    # Find local minima (2 neighbors on each side)
    minima_indices = []
    for i in range(2, n - 2):
        if (
            recent[i] <= recent[i - 1]
            and recent[i] <= recent[i - 2]
            and recent[i] <= recent[i + 1]
            and recent[i] <= recent[i + 2]
        ):
            minima_indices.append(i)

    if len(minima_indices) < 2:
        return False, None, None, None

    # Take last two minima
    mi1, mi2 = minima_indices[-2], minima_indices[-1]
    low1, low2 = recent[mi1], recent[mi2]

    # Price closeness check
    if abs(low2 - low1) / max(low1, 0.01) > _W_LOW_PRICE_CLOSE:
        return False, None, None, None

    # Gap check
    if mi2 - mi1 < _W_MIN_GAP_DAYS:
        return False, None, None, None

    # Neckline: peak between the two minima
    between = recent[mi1:mi2 + 1]
    peak_idx = mi1
    peak_val = low1
    for j in range(len(between)):
        if between[j] > peak_val:
            peak_val = between[j]
            peak_idx = mi1 + j

    # Peak must be notably above both lows
    if peak_val < max(low1, low2) * (1 + _W_PEAK_MARGIN):
        return False, None, None, None

    return True, low1, low2, peak_val


def _check_technical_reversal(
    code: str,
    current_price: float,
) -> dict[str, Any]:
    """综合技术面检查：W底 + 缩量筑底 + 放量突破 + 主力转向。

    返回所有技术字段的字典。
    """
    result: dict[str, Any] = {
        "w_bottom_found": False,
        "w_bottom_low1": None,
        "w_bottom_low2": None,
        "volume_contracted": False,
        "volume_breakout": False,
        "neckline_break": False,
        "main_force_turned": False,
        "above_ma20": False,
        "ret_5d": None,
    }

    try:
        from tradingagents.dataflows.a_stock import _sina_kline_fallback

        df = _sina_kline_fallback(code)
        if df is None or df.empty:
            return result

        closes = []
        volumes = []
        highs = []
        for _, r in df.iterrows():
            try:
                closes.append(float(r["Close"]))
                volumes.append(float(r["Volume"]))
                highs.append(float(r["High"]))
            except (TypeError, ValueError):
                continue

        if len(closes) < 60:
            return result

        # W-bottom detection
        w_found, low1, low2, neckline = _detect_w_bottom(closes)
        result["w_bottom_found"] = w_found
        result["w_bottom_low1"] = low1
        result["w_bottom_low2"] = low2

        # Volume contraction at bottom
        if len(volumes) >= 20:
            recent_vol = volumes[-10:]
            earlier_vol = volumes[-30:-10]
            if sum(earlier_vol) > 0:
                result["volume_contracted"] = (
                    sum(recent_vol) / len(recent_vol)
                    < sum(earlier_vol) / len(earlier_vol) * _VOLUME_BOTTOM_RATIO
                )

        # Volume breakout: last day volume > 20-day avg * ratio
        if len(volumes) >= 20 and volumes[-1] > 0:
            ma20_vol = sum(volumes[-21:-1]) / 20
            result["volume_breakout"] = volumes[-1] > ma20_vol * _VOLUME_BREAKOUT_RATIO

        # Neckline break: current price > 20-day high
        if len(highs) >= 20:
            recent_high_20 = max(highs[-21:-1])  # Exclude today
            result["neckline_break"] = current_price > recent_high_20

        # MA20 check
        if len(closes) >= 20:
            ma20 = sum(closes[-20:]) / 20
            result["above_ma20"] = closes[-1] >= ma20 * 0.98

        # 5-day return
        if len(closes) > 5:
            base = closes[-6]
            if base > 0:
                result["ret_5d"] = (closes[-1] - base) / base

    except Exception as e:
        logger.debug("技术面检查失败 %s: %s", code, e)

    return result


def _check_main_force_turn(code: str) -> bool:
    """检查近3日主力资金是否转向（超大单累计净流入 > 0）。"""
    try:
        url = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
        resp = _em_get(
            url,
            params={
                "secid": f"1.{code}",
                "fields1": "f1,f2,f3,f7",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
                "lmt": str(_MAIN_FORCE_TURN_DAYS),
            },
            timeout=8,
        )
        data = resp.json()
        klines = (data.get("data") or {}).get("klines") or []
        total_super_large = 0.0
        for line in klines[-_MAIN_FORCE_TURN_DAYS:]:
            parts = line.split(",")
            if len(parts) >= 5:
                try:
                    total_super_large += float(parts[4])  # 超大单净额
                except (ValueError, IndexError):
                    continue
        return total_super_large > 0
    except Exception:
        return False


def run_l2_technical_filter(
    stocks: list[TurnaroundStockInfo],
    *,
    on_item: ItemCb | None = None,
) -> list[TurnaroundStockInfo]:
    """L2：底部反转技术确认 — W底 + 缩量筑底 + 放量突破 + 主力转向。"""
    total = len(stocks)
    passed: list[TurnaroundStockInfo] = []

    for idx, info in enumerate(stocks, 1):
        if on_item is not None:
            on_item(info.code, info.name, idx, total)

        tech = _safe_call(
            lambda c: _check_technical_reversal(c, info.price),
            info.code,
            default={},
        )

        info.w_bottom_found = bool(tech.get("w_bottom_found", False))
        info.w_bottom_low1 = tech.get("w_bottom_low1")
        info.w_bottom_low2 = tech.get("w_bottom_low2")
        info.volume_contracted = bool(tech.get("volume_contracted", False))
        info.volume_breakout = bool(tech.get("volume_breakout", False))
        info.neckline_break = bool(tech.get("neckline_break", False))
        info.above_ma20 = bool(tech.get("above_ma20", False))
        info.ret_5d = tech.get("ret_5d")

        # Main force turn check
        info.main_force_turned = _safe_call(
            _check_main_force_turn, info.code, default=False
        )

        # Must satisfy at least: W-bottom OR (neckline break + volume breakout)
        has_tech_signal = (
            info.w_bottom_found
            or (info.neckline_break and info.volume_breakout)
            or info.main_force_turned
        )
        if not has_tech_signal:
            info.exclude_reason = "无技术反转信号"
            continue

        passed.append(info)

    logger.info("反转 L2: 检测 %d 只, 通过 %d 只", total, len(passed))
    return passed


# ── L3: 催化剂评估 + 综合排序 ────────────────────────────────────────────────


def _check_industry_resonance(code: str, all_candidates: list[TurnaroundStockInfo]) -> tuple[bool, int]:
    """检查同行业是否有共振效应（多只同行业股票近月走强）。"""
    try:
        from tradingagents.dataflows.a_stock import _em_get as _get

        # Get industry for this stock
        resp = _get(
            "https://push2.eastmoney.com/api/qt/stock/get",
            params={
                "secid": f"1.{code}",
                "fields": "f100",
            },
            timeout=5,
        )
        data = resp.json()
        industry = (data.get("data") or {}).get("f100", "")
        if not industry:
            return False, 0

        # Count same-industry candidates with recent gains
        count = 0
        for info in all_candidates:
            if info.code == code:
                continue
            if info.ret_5d is not None and info.ret_5d > _INDUSTRY_RESONANCE_THRESHOLD:
                count += 1

        return count >= _INDUSTRY_RESONANCE_MIN, count
    except Exception:
        return False, 0


def _check_management_action(code: str) -> bool:
    """检查是否有管理层/大股东增持公告。"""
    try:
        url = "https://search-api.eastmoney.com/bussiness/Web/GetCMSSearchResult"
        resp = _em_get(
            url,
            params={
                "type": "8196",
                "pageindex": "1",
                "pagesize": "5",
                "keyword": f"{code} 增持",
                "name": "zixun",
            },
            timeout=8,
        )
        data = resp.json()
        items = data.get("Data") or []
        for item in items:
            title = str(item.get("Title") or "")
            if "增持" in title or "回购" in title:
                return True
    except Exception:
        pass
    return False


def compute_turnaround_score(info: TurnaroundStockInfo) -> int:
    """计算综合信号分（0-10+）。"""
    s = 0

    # L0.5 安全垫
    if info.pb_safe and info.pb < _PB_ASSET_PLAY:
        s += 2  # 破净资产重估潜力
    elif info.pb_safe:
        s += 1

    if info.revenue_yoy is not None and info.revenue_yoy > 0:
        s += 1  # 营收正增长（抗跌）

    if info.ocf_ttm is not None and info.ocf_ttm > 0:
        s += 1  # 现金流健康

    # L1 利空出尽
    if info.news_resilient:
        s += 1

    # L1.5 资金预警
    if info.volume_bottomed:
        s += 1
    if info.smart_money_active:
        s += 2  # 主力吸筹最强信号

    # L2 技术反转
    if info.w_bottom_found:
        s += 2  # W底形态
    if info.volume_breakout:
        s += 1
    if info.neckline_break:
        s += 1
    if info.main_force_turned:
        s += 1
    if info.above_ma20:
        s += 1

    # L3 催化剂
    if info.industry_resonance:
        s += 1
    if info.asset_revalue:
        s += 1  # PB < 1
    if info.management_action:
        s += 2  # 增持信号最强

    return s


def run_l3_catalyst_and_rank(
    stocks: list[TurnaroundStockInfo],
    max_candidates: int = _MAX_CANDIDATES,
    *,
    on_item: ItemCb | None = None,
) -> list[TurnaroundStockInfo]:
    """L3：催化剂评估 + 综合评分排序。"""
    total = len(stocks)

    for idx, info in enumerate(stocks, 1):
        if on_item is not None:
            on_item(info.code, info.name, idx, total)

        # Industry resonance
        resonance, count = _safe_call(
            lambda c: _check_industry_resonance(c, stocks),
            info.code,
            default=(False, 0),
        )
        info.industry_resonance = bool(resonance)
        info.industry_count = int(count)

        # Management action
        info.management_action = _safe_call(
            _check_management_action, info.code, default=False
        )

        # Compute score
        info.signal_score = compute_turnaround_score(info)

        # Assign lane: ret_5d >= 8% → watch
        from tradingagents.strategies.value_swing import assign_scan_lane
        info.lane = assign_scan_lane(info)

    # Sort by score descending
    scored = sorted(stocks, key=lambda s: (s.signal_score, s.pb or 999), reverse=False)
    # For signal_score, higher is better; for PB, lower is better
    scored = sorted(scored, key=lambda s: s.signal_score, reverse=True)

    result = scored[:max_candidates]
    logger.info("反转 L3: 候选 %d 只", len(result))
    return result


# ── 进度工具 ──────────────────────────────────────────────────────────────────


class _ScanProgress:
    def __init__(self, cb: ProgressCb | None, total_stocks: int):
        self._cb = cb
        self.total_stocks = total_stocks
        self.l0_passed = 0
        self.l0_5_passed = 0
        self.l1_passed = 0
        self.l1_5_passed = 0
        self.l2_passed = 0
        self.l3_passed = 0

    def emit(
        self,
        stage: str,
        percent: float,
        *,
        code: str = "",
        name: str = "",
        stage_index: int = 0,
        stage_total: int = 0,
    ) -> None:
        if self._cb is None:
            return
        snapshot = {
            "stage": stage,
            "stage_label": _STAGE_LABELS.get(stage, stage),
            "total_stocks": self.total_stocks,
            "l0_passed": self.l0_passed,
            "l0_5_passed": self.l0_5_passed,
            "l1_passed": self.l1_passed,
            "l1_5_passed": self.l1_5_passed,
            "l2_passed": self.l2_passed,
            "l3_passed": self.l3_passed,
            "current_code": code,
            "current_name": name,
            "stage_index": int(stage_index),
            "stage_total": int(stage_total),
            "percent": max(0, min(100, int(percent))),
        }
        try:
            self._cb(snapshot)
        except Exception:
            logger.debug("turnaround progress callback failed", exc_info=True)


def _interp(lo: float, hi: float, index: int, total: int) -> float:
    if total <= 0:
        return hi
    return lo + (hi - lo) * (index / total)


# ── 规则快照 ──────────────────────────────────────────────────────────────────


def l2_score_max(*, only_active: bool = True) -> int:
    """L3 展示满分。"""
    return sum(1 for _, _, active in _L3_FACTOR_SPECS if active or not only_active)


def selection_rules_snapshot() -> dict[str, Any]:
    return {
        "strategy": "turnaround",
        "name": "错杀反转",
        "l0": [
            "剔除 ST/退市标识",
            f"成交额 ≥ {_MIN_VOLUME_WAN:g} 万元",
            "允许 PE ≤ 0（亏损可入池）",
            f"排除北交所 8xxx",
            f"上市 ≥ {_MIN_LISTED_MONTHS} 个月",
            f"PB < {_MAX_PB_L0:g}（排除高估值反弹股）",
        ],
        "l0_5": [
            f"营收同比 > {_MIN_REVENUE_YOY * 100:g}%（未崩塌）",
            "经营现金流 > 0（非现金失血）",
            f"资产负债率 < {_MAX_DEBT_RATIO * 100:g}%",
            f"PB < {_MAX_PB_L0_5:g}（有估值安全垫）",
            f"深度检测上限 {_L0_5_PROCESS_LIMIT} 只（按 PB 升序）",
        ],
        "l1": [
            "近 30 天有负面公告（预亏/减值/计提等）",
            f"公告后 3 日跌幅 < {_MAX_CRASH_3D * 100:g}%（股价韧性）",
        ],
        "l1_5": [
            "地量筑底：底部5日均量 < 20日均量 × {:.0%}".format(_VOLUME_BOTTOM_RATIO),
            "温和放量：近3日均量 > 最低量 × {:.0%}".format(_VOLUME_WARM_RATIO),
            f"主力吸筹：近5日中 ≥ {_SMART_MONEY_DAYS_MIN} 天主力净流入+散户净流出",
        ],
        "l2": [
            f"W底形态：{_W_BOTTOM_WINDOW}日窗口内双底，价差 < {_W_LOW_PRICE_CLOSE * 100:g}%，"
            f"间隔 ≥ {_W_MIN_GAP_DAYS}天，峰高 > 底 × {1 + _W_PEAK_MARGIN:.2f}",
            f"放量突破：当日量 > 20日均量 × {_VOLUME_BREAKOUT_RATIO:g}",
            "突破颈线：收盘 > 近20日最高价",
            f"主力转向：近{_MAIN_FORCE_TURN_DAYS}日超大单累计净流入 > 0",
            "站上 MA20：收盘 ≥ MA20 × 0.98",
        ],
        "l3": [
            f"行业共振：同行业 ≥ {_INDUSTRY_RESONANCE_MIN} 只近月涨幅 > {_INDUSTRY_RESONANCE_THRESHOLD * 100:g}%",
            f"资产重估：PB < {_PB_ASSET_PLAY:g}（破净）",
            "管理层行动：增持/回购公告",
        ],
    }


def l2_factor_hits(candidate: TurnaroundStockInfo | dict[str, Any]) -> list[dict[str, Any]]:
    def _get(key: str, default=False):
        if isinstance(candidate, dict):
            return candidate.get(key, default)
        return getattr(candidate, key, default)

    return [
        {
            "key": key,
            "label": label,
            "active": active,
            "hit": bool(_get(key, False)),
        }
        for key, label, active in _L3_FACTOR_SPECS
    ]


def why_selected_line(candidate: TurnaroundStockInfo | dict[str, Any]) -> str:
    def _get(key: str, default=None):
        if isinstance(candidate, dict):
            return candidate.get(key, default)
        return getattr(candidate, key, default)

    bits: list[str] = []

    pb = _get("pb")
    try:
        pb_f = float(pb) if pb is not None else None
    except (TypeError, ValueError):
        pb_f = None
    if pb_f is not None and pb_f < _PB_ASSET_PLAY:
        bits.append(f"破净 PB {pb_f:.2f}")

    if _get("w_bottom_found"):
        bits.append("W底形态")
    if _get("volume_breakout"):
        bits.append("放量突破")
    if _get("main_force_turned"):
        bits.append("主力转向")
    if _get("smart_money_active"):
        bits.append("主力吸筹")
    if _get("news_resilient"):
        bits.append("利空出尽")
    if _get("industry_resonance"):
        bits.append("行业共振")
    if _get("management_action"):
        bits.append("增持/回购")
    if _get("asset_revalue"):
        bits.append("资产重估")

    score = _get("signal_score", 0)
    return " · ".join(bits) if bits else f"低分候选 (信号分 {score})"


# ── 扫描入口 ──────────────────────────────────────────────────────────────────


def run_turnaround_scan(
    max_candidates: int = _MAX_CANDIDATES,
    *,
    progress_cb: ProgressCb | None = None,
) -> ScanResult:
    """执行一轮完整「错杀反转」扫描（L0 → L0.5 → L1 → L1.5 → L2 → L3）。"""
    ts = time.time()
    scan_date = datetime.now().strftime("%Y-%m-%d")
    result = ScanResult(scan_date=scan_date)
    logger.info("═══ 错杀反转扫描 %s ═══", scan_date)

    result.total_stocks = len(_get_all_cn_codes())
    prog = _ScanProgress(progress_cb, result.total_stocks)
    prog.emit("L0", _PCT_L0_START)

    # L0
    l0 = run_l0_filter()
    result.l0_passed = len(l0)
    prog.l0_passed = len(l0)
    prog.emit("L0", _PCT_L0_DONE)

    # L0.5
    def _on_l0_5(code: str, name: str, idx: int, total: int) -> None:
        prog.emit(
            "L0.5",
            _interp(_PCT_L0_DONE, _PCT_L0_5_DONE, idx, total),
            code=code,
            name=name,
            stage_index=idx,
            stage_total=total,
        )

    l0_5 = run_l0_5_filter(l0, on_item=_on_l0_5)
    result.l0_5_passed = len(l0_5)
    prog.l0_5_passed = len(l0_5)
    prog.emit("L0.5", _PCT_L0_5_DONE)

    # L1
    def _on_l1(code: str, name: str, idx: int, total: int) -> None:
        prog.emit(
            "L1",
            _interp(_PCT_L0_5_DONE, _PCT_L1_DONE, idx, total),
            code=code,
            name=name,
            stage_index=idx,
            stage_total=total,
        )

    l1 = run_l1_news_filter(l0_5, on_item=_on_l1)
    result.l1_passed = len(l1)
    prog.l1_passed = len(l1)
    prog.emit("L1", _PCT_L1_DONE)

    # L1.5
    def _on_l1_5(code: str, name: str, idx: int, total: int) -> None:
        prog.emit(
            "L1.5",
            _interp(_PCT_L1_DONE, _PCT_L1_5_DONE, idx, total),
            code=code,
            name=name,
            stage_index=idx,
            stage_total=total,
        )

    l1_5 = run_l1_5_money_filter(l1, on_item=_on_l1_5)
    result.l1_5_passed = len(l1_5)
    prog.l1_5_passed = len(l1_5)
    prog.emit("L1.5", _PCT_L1_5_DONE)

    # L2
    def _on_l2(code: str, name: str, idx: int, total: int) -> None:
        prog.emit(
            "L2",
            _interp(_PCT_L1_5_DONE, _PCT_L2_DONE, idx, total),
            code=code,
            name=name,
            stage_index=idx,
            stage_total=total,
        )

    l2 = run_l2_technical_filter(l1_5, on_item=_on_l2)
    result.l2_passed = len(l2)
    prog.l2_passed = len(l2)
    prog.emit("L2", _PCT_L2_DONE)

    # L3
    def _on_l3(code: str, name: str, idx: int, total: int) -> None:
        prog.emit(
            "L3",
            _interp(_PCT_L2_DONE, _PCT_L3_DONE, idx, total),
            code=code,
            name=name,
            stage_index=idx,
            stage_total=total,
        )

    l3 = run_l3_catalyst_and_rank(l2, max_candidates=max_candidates, on_item=_on_l3)
    result.l3_passed = len(l3)
    result.candidates = l3
    prog.l3_passed = len(l3)
    prog.emit("done", _PCT_L3_DONE)

    result.duration_seconds = time.time() - ts
    logger.info(
        "═══ 反转 %d→%d→%d→%d→%d→%d 候选, %.0fs ═══",
        result.l0_passed,
        result.l0_5_passed,
        result.l1_passed,
        result.l1_5_passed,
        result.l2_passed,
        result.l3_passed,
        result.duration_seconds,
    )
    return result
