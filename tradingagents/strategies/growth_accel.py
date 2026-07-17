"""成长加速选股策略 — 与价值波段并行的第二套漏斗。

L0 流动性（允许亏损）→ L1a 成交额预筛 → L1b 扣非优先利润/亏损拐点 + 现金流质量
→ L2 偏进攻排序（加速提权），输出 top 15。

不设 PE/PB 硬顶；主排序键为扣非（否则归母）净利 TTM 增速。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd

from tradingagents.dataflows.a_stock import _build_name_code_map, _em_get
from tradingagents.strategies.expectation_gate import (
    apply_gate_fields,
    fetch_consensus_snapshot,
    score_growth_expectation,
)
from tradingagents.strategies.value_swing import (
    LANE_ANALYZE,
    _MIN_LISTED_MONTHS,
    _OVEREXTEND_HARD,
    _OVEREXTEND_SOFT,
    _TENCENT_BATCH_SIZE,
    _calc_ret_nd,
    _estimate_listed_months,
    _get_all_cn_codes,
    _is_stock_code,
    _is_stock_excluded_by_prefix,
    _load_hot_stocks,
    _safe_call,
    _tencent_volume_wan,
    assign_scan_lane,
    overextend_score_delta,
)

logger = logging.getLogger(__name__)

# ── 可调参数（与 PRD 定稿对齐）──────────────────────────────────────────────

_MIN_VOLUME_WAN: float = 3000.0
_L1_PROCESS_LIMIT: int = 120          # 仅对成交额前 N 只做财报深挖
_MAX_CANDIDATES: int = 15

_MIN_NP_YOY: float = 0.50             # 已盈利：扣非/归母 TTM YoY ≥ 50%
_STRONG_NP_YOY: float = 1.00          # ≥100% 强信号
_MIN_NP_ABS: float = 1e8              # 利润 TTM ≥ 1 亿元

_MIN_REV_YOY_LOSS: float = 0.30       # 亏损轨：营收 TTM YoY ≥ 30%
_MIN_REV_ABS_LOSS: float = 1e9        # 亏损轨：营收 TTM ≥ 10 亿元
_MIN_LOSS_NARROW: float = 0.30        # 亏损额同比收窄 ≥ 30%

# 质量：扣非缺口 / OCF
_NONRECURRING_GAP: float = 0.30       # 扣非 YoY < 归母 YoY − 30pct → 硬剔
_OCF_RATIO_OK: float = 0.30           # OCF/利润 ≥ 0.3 不加分减分
_ACCEL_EXTRA_SCORE: int = 2           # 近季加速（偏进攻）

_GROWTH_THEME_KEYWORDS = (
    "算力", "光通信", "光模块", "人工智能", "AI", "半导体", "芯片",
    "机器人", "液冷", "服务器", "先进封装", "CPO", "新能源", "创新药",
)

ProgressCb = Callable[[dict[str, Any]], None]
ItemCb = Callable[[str, str, int, int], None]

_PCT_L0_START = 2
_PCT_L0_DONE = 15
_PCT_L1A_DONE = 25
_PCT_L1B_DONE = 80
_PCT_L2_DONE = 100

_STAGE_LABELS = {
    "L0": "全市场流动性/ST 筛选（含亏损）",
    "L1a": "成交额预筛",
    "L1b": "利润增速 / 亏损拐点",
    "L2": "前景轻加权排序",
    "done": "完成",
}


@dataclass
class GrowthStockInfo:
    code: str
    name: str = ""
    price: float = 0.0
    volume_wan: float = 0.0
    listed_months: int = 999
    pe_ttm: float = 0.0
    pb: float = 0.0
    # 用于入池/排序的利润 TTM（扣非优先，否则归母）
    np_ttm: float | None = None
    np_ttm_prior: float | None = None
    np_ttm_yoy: float | None = None
    parent_np_ttm_yoy: float | None = None
    deduct_np_ttm_yoy: float | None = None
    used_deduct: bool = False
    no_nonrecurring: bool = False  # 无扣非字段，回退归母
    rev_ttm: float | None = None
    rev_ttm_prior: float | None = None
    rev_ttm_yoy: float | None = None
    ocf_ttm: float | None = None
    ocf_ttm_prior: float | None = None
    ocf_score_delta: int = 0
    # 分类与加权
    track: str = ""  # "profit" | "loss"
    turnaround: bool = False
    loss_narrowed: bool = False
    revenue_accel: bool = False  # 兼容旧字段：同 profit_accel
    profit_accel: bool = False
    growth_theme: bool = False
    high_liquidity: bool = False
    # 一致预期质量闸门
    exp_score_delta: int = 0
    exp_hit: bool = False
    exp_label: str = ""
    exp_low_coverage: bool = False
    exp_fwd_pe: float | None = None
    exp_implied_cagr: float | None = None
    exp_analysts: int = 0
    # 与价值波段对齐的超涨分道
    ret_5d: float | None = None
    overextend_delta: int = 0
    lane: str = LANE_ANALYZE
    signal_score: int = 0
    exclude_reason: str = ""


@dataclass
class ScanResult:
    scan_date: str
    total_stocks: int = 0
    l0_passed: int = 0
    l1a_passed: int = 0
    l1b_passed: int = 0
    l2_passed: int = 0
    candidates: list[GrowthStockInfo] = field(default_factory=list)
    duration_seconds: float = 0.0
    strategy: str = "growth_accel"


def selection_rules_snapshot() -> dict[str, Any]:
    return {
        "strategy": "growth_accel",
        "name": "成长加速",
        "l0": [
            "剔除 ST/退市标识",
            f"成交额 ≥ {_MIN_VOLUME_WAN:g} 万元",
            "允许 PE≤0（亏损可入池）",
            "不设 PE/PB 上限",
            "排除北交所 8xxx",
            f"上市 ≥ {_MIN_LISTED_MONTHS} 个月",
        ],
        "l1a": [
            f"按成交额取前 {_L1_PROCESS_LIMIT} 只做财报深挖",
        ],
        "l1": [
            f"已盈利：扣非净利 TTM YoY ≥ {_MIN_NP_YOY * 100:g}%（无扣非则归母）"
            f"且绝对额 ≥ {_MIN_NP_ABS / 1e8:g} 亿",
            f"扣非 YoY < 归母 YoY − {_NONRECURRING_GAP * 100:g}pct → 剔（一次性利润）",
            f"强信号：利润 TTM YoY ≥ {_STRONG_NP_YOY * 100:g}%",
            f"亏损成长：营收 TTM YoY ≥ {_MIN_REV_YOY_LOSS * 100:g}% 且营收 ≥ {_MIN_REV_ABS_LOSS / 1e8:g} 亿，"
            f"且亏损收窄 ≥ {_MIN_LOSS_NARROW * 100:g}% 或由亏转盈",
            f"OCF 质量：利润轨 OCF/利润<{_OCF_RATIO_OK:g} 降权；缺失不踢（偏进攻）",
            "指标缺失 → 不入池（fail-closed）",
        ],
        "l1b": [
            "同 L1 增长核 + 扣非/OCF 质量（见左列）",
        ],
        "l2": {
            "score_max": l2_score_max(),
            "active": [
                "净利TTM高增",
                "强增速≥100%",
                "亏损拐点/收窄",
                "近季加速(+2)",
                "成长题材且未超涨",
                "高流动性",
                "扣非/OCF质量调整",
                "一致预期差/透支闸门(±1)",
                "近5日超涨扣分/分道",
            ],
            "dormant": [],
            "top_n": _MAX_CANDIDATES,
            "note": (
                "偏进攻：加速+2；主排序信号分→利润TTM增速；不设估值硬顶；"
                "覆盖≥3 家时：实际≫隐含 +1，预期透支 −1；"
                f"成长题材仅在近5日涨幅<{_OVEREXTEND_SOFT * 100:.0f}% 时计分；"
                f"近5日≥{_OVEREXTEND_SOFT * 100:.0f}% 扣1分、"
                f"≥{_OVEREXTEND_HARD * 100:.0f}% 扣2分并标 watch；"
                "自动入队默认仅 analyze 道"
            ),
        },
    }


def l2_score_max(*, only_active: bool = True) -> int:
    # 强增速3 + 加速2 + 题材1 + 流动性1 + 预期闸门1 = 8；亏损轨 OCF+1 可达 9
    return 9


# ── TTM 计算（国内报表为累计 YTD）────────────────────────────────────────────


def _lookup_ytd(series: pd.Series, year: int, month: int) -> float | None:
    for ts, val in series.items():
        if int(ts.year) == year and int(ts.month) == month:
            try:
                return float(val)
            except (TypeError, ValueError):
                return None
    return None


def compute_ttm_pair(
    ytd: pd.Series,
) -> tuple[float | None, float | None, float | None]:
    """从累计口径季报序列计算 (TTM, 上年同期 TTM, YoY)。

    桥接：最新为 Q 月时，TTM = 上年年报 + 本年 YTD(Q) − 上年 YTD(Q)。
    prior≤0 时 YoY 记为 None（由增长轨判定转正/收窄）。
    """
    if ytd is None or ytd.empty:
        return None, None, None
    s = ytd.dropna().sort_index(ascending=False)
    if s.empty:
        return None, None, None

    latest_ts = pd.Timestamp(s.index[0])
    latest_val = float(s.iloc[0])
    y, m = int(latest_ts.year), int(latest_ts.month)

    if m == 12:
        ttm = latest_val
        prior = _lookup_ytd(s, y - 1, 12)
    else:
        fy_prev = _lookup_ytd(s, y - 1, 12)
        ytd_prev = _lookup_ytd(s, y - 1, m)
        if fy_prev is None or ytd_prev is None:
            return None, None, None
        ttm = fy_prev + latest_val - ytd_prev
        fy_prev2 = _lookup_ytd(s, y - 2, 12)
        ytd_prev2 = _lookup_ytd(s, y - 2, m)
        if fy_prev2 is None or ytd_prev2 is None:
            prior = None
        else:
            prior = fy_prev2 + ytd_prev - ytd_prev2

    if ttm is None:
        return None, None, None

    yoy: float | None
    if prior is None or prior <= 0:
        yoy = None
    else:
        yoy = (ttm - prior) / prior
    return ttm, prior, yoy


def resolve_profit_metrics(
    *,
    parent_ttm: float | None,
    parent_prior: float | None,
    parent_yoy: float | None,
    deduct_ttm: float | None,
    deduct_prior: float | None,
    deduct_yoy: float | None,
) -> dict[str, Any]:
    """扣非优先；无扣非则回退归母并标记 no_nonrecurring。"""
    if deduct_ttm is not None:
        return {
            "np_ttm": deduct_ttm,
            "np_prior": deduct_prior,
            "np_yoy": deduct_yoy,
            "used_deduct": True,
            "no_nonrecurring": False,
            "parent_yoy": parent_yoy,
            "deduct_yoy": deduct_yoy,
        }
    return {
        "np_ttm": parent_ttm,
        "np_prior": parent_prior,
        "np_yoy": parent_yoy,
        "used_deduct": False,
        "no_nonrecurring": parent_ttm is not None,
        "parent_yoy": parent_yoy,
        "deduct_yoy": None,
    }


def is_nonrecurring_gap(
    parent_yoy: float | None,
    deduct_yoy: float | None,
    *,
    gap: float = _NONRECURRING_GAP,
) -> bool:
    """扣非增速明显低于归母 → 一次性利润嫌疑。"""
    if parent_yoy is None or deduct_yoy is None:
        return False
    return deduct_yoy < parent_yoy - gap


def ocf_quality_score_delta(
    *,
    track: str,
    np_ttm: float | None,
    ocf_ttm: float | None,
    ocf_prior: float | None,
) -> int:
    """OCF 质量调整：缺失不处理；利润轨弱匹配降权；亏损轨好转 +1。"""
    if ocf_ttm is None:
        return 0
    if track == "loss":
        if ocf_prior is not None and ocf_ttm > ocf_prior:
            return 1
        return 0
    if np_ttm is None or np_ttm <= 0:
        return 0
    if ocf_ttm < 0 and ocf_prior is not None and ocf_prior < 0 and ocf_ttm < ocf_prior:
        return -2
    ratio = ocf_ttm / np_ttm
    if ratio >= _OCF_RATIO_OK:
        return 0
    return -1


def evaluate_growth_track(
    *,
    np_ttm: float | None,
    np_yoy: float | None,
    rev_ttm: float | None,
    rev_yoy: float | None,
    np_prior: float | None,
) -> tuple[bool, str, str]:
    """返回 (是否入池, track, exclude_reason)。"""
    if np_ttm is None or rev_ttm is None:
        return False, "", "指标缺失"

    if np_ttm > 0:
        track = "profit"
        if np_ttm < _MIN_NP_ABS:
            return False, track, "利润规模不足"
        # 由亏转盈：直接放行（绝对值已过门槛）
        if np_prior is not None and np_prior <= 0:
            return True, track, ""
        if np_yoy is None or np_yoy < _MIN_NP_YOY:
            return False, track, "净利增速不足"
        return True, track, ""

    track = "loss"
    if rev_ttm < _MIN_REV_ABS_LOSS:
        return False, track, "营收规模不足"
    if rev_yoy is None or rev_yoy < _MIN_REV_YOY_LOSS:
        return False, track, "营收增速不足"
    if np_prior is not None and np_prior < 0 and np_ttm < 0:
        narrow = (abs(np_prior) - abs(np_ttm)) / abs(np_prior)
        if narrow >= _MIN_LOSS_NARROW:
            return True, track, ""
    return False, track, "亏损未明显收窄"


def _growth_theme_scores(info: GrowthStockInfo) -> bool:
    """成长题材仅在未软超涨时计分（对齐价值波段动量门控）。"""
    if not info.growth_theme:
        return False
    return overextend_score_delta(info.ret_5d) == 0


def compute_growth_signal_score(info: GrowthStockInfo) -> int:
    """计算信号分，并回写 ``overextend_delta`` / ``lane``（地板为 0）。"""
    s = 0
    if info.track == "profit":
        if info.np_ttm_yoy is not None and info.np_ttm_yoy >= _STRONG_NP_YOY:
            s += 3
        elif info.np_ttm_yoy is not None and info.np_ttm_yoy >= _MIN_NP_YOY:
            s += 2
        elif info.turnaround:
            s += 3
        else:
            s += 1
    elif info.track == "loss":
        if info.loss_narrowed or info.turnaround:
            s += 2
        else:
            s += 1
    accel = info.profit_accel or info.revenue_accel
    if accel:
        s += _ACCEL_EXTRA_SCORE
    if _growth_theme_scores(info):
        s += 1
    if info.high_liquidity:
        s += 1
    if info.no_nonrecurring:
        s -= 1
    s += int(info.ocf_score_delta)
    s += int(info.exp_score_delta or 0)
    info.overextend_delta = overextend_score_delta(info.ret_5d)
    s += int(info.overextend_delta)
    info.lane = assign_scan_lane(info)
    return max(0, s)


def why_selected_line(candidate: GrowthStockInfo | dict[str, Any]) -> str:
    def _get(key: str, default=None):
        if isinstance(candidate, dict):
            return candidate.get(key, default)
        return getattr(candidate, key, default)

    track = _get("track") or ""
    yoy = _get("np_ttm_yoy")
    bits: list[str] = []
    if track == "profit" and yoy is not None:
        # result_dict 可能已把 yoy 存成百分数
        yoy_f = float(yoy)
        label_yoy = yoy_f if yoy_f > 3 else yoy_f * 100
        prefix = "扣非TTM" if _get("used_deduct") else "净利TTM"
        bits.append(f"{prefix} {label_yoy:.0f}%")
    elif track == "loss":
        bits.append("亏损收窄/成长")
    if _get("turnaround"):
        bits.append("由亏转盈")
    if _get("profit_accel") or _get("revenue_accel"):
        bits.append("近季加速")
    # 题材仅在未超涨时展示为命中（与计分一致）
    theme = bool(_get("growth_theme"))
    ret = _get("ret_5d")
    try:
        ret_f = float(ret) if ret is not None else None
    except (TypeError, ValueError):
        ret_f = None
    if theme and overextend_score_delta(ret_f) == 0:
        bits.append("成长题材")
    if _get("high_liquidity"):
        bits.append("高流动性")
    if _get("no_nonrecurring"):
        bits.append("无扣非回退")
    ocf_d = _get("ocf_score_delta") or 0
    try:
        if int(ocf_d) < 0:
            bits.append("OCF偏弱")
        elif int(ocf_d) > 0:
            bits.append("OCF改善")
    except (TypeError, ValueError):
        pass
    try:
        exp_d = int(_get("exp_score_delta") or 0)
    except (TypeError, ValueError):
        exp_d = 0
    if exp_d > 0:
        bits.append(str(_get("exp_label") or "实际高于一致预期"))
    elif exp_d < 0:
        bits.append(str(_get("exp_label") or "一致预期透支"))
    try:
        ox = int(_get("overextend_delta") or 0)
    except (TypeError, ValueError):
        ox = 0
    if ox == 0 and ret_f is not None:
        ox = overextend_score_delta(ret_f)
    if ox < 0:
        bits.append(f"近5日超涨({ox})")
    lane = str(_get("lane") or "").strip().lower()
    if lane == "watch":
        bits.append("回撤观察道")
    return " · ".join(bits) if bits else "成长加速入池"


def l2_factor_hits(candidate: GrowthStockInfo | dict[str, Any]) -> list[dict[str, Any]]:
    def _get(key: str, default=False):
        if isinstance(candidate, dict):
            return candidate.get(key, default)
        return getattr(candidate, key, default)

    yoy = _get("np_ttm_yoy", None)
    track = _get("track", "")
    yoy_f: float | None
    try:
        yoy_f = float(yoy) if yoy is not None else None
        if yoy_f is not None and yoy_f > 3:
            yoy_f = yoy_f / 100.0
    except (TypeError, ValueError):
        yoy_f = None
    ret_raw = _get("ret_5d", None)
    try:
        ret_f = float(ret_raw) if ret_raw is not None else None
    except (TypeError, ValueError):
        ret_f = None
    theme_hit = bool(_get("growth_theme")) and overextend_score_delta(ret_f) == 0
    return [
        {
            "key": "np_growth",
            "label": "净利TTM高增",
            "active": True,
            "hit": track == "profit" and yoy_f is not None and yoy_f >= _MIN_NP_YOY,
        },
        {
            "key": "strong_growth",
            "label": "强增速≥100%",
            "active": True,
            "hit": yoy_f is not None and yoy_f >= _STRONG_NP_YOY,
        },
        {
            "key": "loss_inflection",
            "label": "亏损拐点/收窄",
            "active": True,
            "hit": bool(_get("loss_narrowed") or _get("turnaround")),
        },
        {
            "key": "profit_accel",
            "label": "近季加速",
            "active": True,
            "hit": bool(_get("profit_accel") or _get("revenue_accel")),
        },
        {
            "key": "growth_theme",
            "label": "成长题材且未超涨",
            "active": True,
            "hit": theme_hit,
        },
        {
            "key": "high_liquidity",
            "label": "高流动性",
            "active": True,
            "hit": bool(_get("high_liquidity")),
        },
        {
            "key": "exp_hit",
            "label": str(_get("exp_label") or "实际增速高于一致预期"),
            "active": True,
            "hit": bool(_get("exp_hit")),
        },
    ]

# ── 进度 ────────────────────────────────────────────────────────────────────


class _ScanProgress:
    def __init__(self, cb: ProgressCb | None, total_stocks: int):
        self._cb = cb
        self.total_stocks = total_stocks
        self.l0_passed = 0
        self.l1a_passed = 0
        self.l1b_passed = 0
        self.l2_passed = 0

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
            "l1a_passed": self.l1a_passed,
            "l1b_passed": self.l1b_passed,
            "l2_passed": self.l2_passed,
            "current_code": code,
            "current_name": name,
            "stage_index": int(stage_index),
            "stage_total": int(stage_total),
            "percent": max(0, min(100, int(percent))),
        }
        try:
            self._cb(snapshot)
        except Exception:  # noqa: BLE001
            logger.debug("growth progress callback failed", exc_info=True)


def _interp(lo: float, hi: float, index: int, total: int) -> float:
    if total <= 0:
        return hi
    return lo + (hi - lo) * (index / total)


# ── L0 / L1a ────────────────────────────────────────────────────────────────


def run_l0_filter() -> list[GrowthStockInfo]:
    """L0：流动性 + 非 ST；允许 PE≤0。"""
    from tradingagents.dataflows.a_stock import _tencent_quote

    all_codes = _get_all_cn_codes()
    logger.info("成长 L0: 全量种子 %d 只", len(all_codes))

    all_quotes: dict[str, dict] = {}
    for i in range(0, len(all_codes), _TENCENT_BATCH_SIZE):
        batch = all_codes[i: i + _TENCENT_BATCH_SIZE]
        try:
            all_quotes.update(_tencent_quote(batch))
        except Exception as e:
            logger.warning("成长 L0 腾讯报价 batch 失败: %s", e)
        time.sleep(0.25)

    passed: list[GrowthStockInfo] = []
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
        info = GrowthStockInfo(
            code=code, name=name, price=price, volume_wan=vol_wan, pe_ttm=pe_ttm, pb=pb
        )
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
        passed.append(info)

    logger.info("成长 L0: 通过 %d 只", len(passed))
    return passed


def run_l1a_prefilter(stocks: list[GrowthStockInfo]) -> list[GrowthStockInfo]:
    ranked = sorted(stocks, key=lambda s: s.volume_wan, reverse=True)
    out = ranked[:_L1_PROCESS_LIMIT]
    logger.info("成长 L1a: 预筛 %d 只", len(out))
    return out


# ── 财报拉取与 L1b（东财：扣非 + 经营现金流）───────────────────────────────


def _em_rows(report_name: str, code: str, columns: str, page_size: int = 16) -> list[dict]:
    resp = _em_get(
        "https://datacenter-web.eastmoney.com/api/data/v1/get",
        params={
            "reportName": report_name,
            "columns": columns,
            "filter": f'(SECURITY_CODE="{code}")',
            "pageNumber": "1",
            "pageSize": str(page_size),
            "sortTypes": "-1",
            "sortColumns": "REPORT_DATE",
            "source": "HEXIN",
            "client": "WEB",
        },
        timeout=10,
    )
    data = resp.json()
    rows = (data.get("result") or {}).get("data") or []
    return rows if isinstance(rows, list) else []


def _series_from_em_rows(rows: list[dict], field: str) -> pd.Series:
    dates: list[pd.Timestamp] = []
    vals: list[float] = []
    for row in rows:
        try:
            ts = pd.Timestamp(str(row.get("REPORT_DATE", ""))[:10])
            val = float(row[field]) if row.get(field) is not None else float("nan")
        except (TypeError, ValueError, KeyError):
            continue
        if pd.isna(ts) or pd.isna(val):
            continue
        dates.append(ts)
        vals.append(val)
    if not dates:
        return pd.Series(dtype=float)
    return pd.Series(vals, index=dates)


def _near_yoy_accel(series: pd.Series, ttm_yoy: float | None) -> bool:
    """最新累计 YoY 是否相对 TTM YoY 加速（> TTM + 5pct）。"""
    if series is None or series.empty or ttm_yoy is None:
        return False
    s = series.dropna().sort_index(ascending=False)
    if s.empty:
        return False
    latest_ts = pd.Timestamp(s.index[0])
    cur = _lookup_ytd(s, int(latest_ts.year), int(latest_ts.month))
    prev = _lookup_ytd(s, int(latest_ts.year) - 1, int(latest_ts.month))
    if cur is None or prev is None or prev == 0:
        return False
    # prev 可为负（亏损），仍用相对变化；为正时用常规 YoY
    if prev > 0:
        near_yoy = (cur - prev) / prev
    else:
        return False
    return near_yoy > ttm_yoy + 0.05


def _fetch_income_metrics(code: str) -> dict[str, Any]:
    """东财利润表（含扣非）+ 现金流量表 → TTM 指标。"""
    income_cols = (
        "SECURITY_CODE,REPORT_DATE,PARENT_NETPROFIT,"
        "DEDUCT_PARENT_NETPROFIT,TOTAL_OPERATE_INCOME,OPERATE_INCOME"
    )
    income_rows = _em_rows("RPT_DMSK_FN_INCOME", code, income_cols)
    cf_rows = _em_rows(
        "RPT_DMSK_FN_CASHFLOW",
        code,
        "SECURITY_CODE,REPORT_DATE,NETCASH_OPERATE",
    )

    parent_s = _series_from_em_rows(income_rows, "PARENT_NETPROFIT")
    deduct_s = _series_from_em_rows(income_rows, "DEDUCT_PARENT_NETPROFIT")
    rev_s = _series_from_em_rows(income_rows, "TOTAL_OPERATE_INCOME")
    if rev_s.empty:
        rev_s = _series_from_em_rows(income_rows, "OPERATE_INCOME")
    ocf_s = _series_from_em_rows(cf_rows, "NETCASH_OPERATE")

    parent_ttm, parent_prior, parent_yoy = compute_ttm_pair(parent_s)
    deduct_ttm, deduct_prior, deduct_yoy = compute_ttm_pair(deduct_s)
    rev_ttm, rev_prior, rev_yoy = compute_ttm_pair(rev_s)
    ocf_ttm, ocf_prior, _ = compute_ttm_pair(ocf_s)

    resolved = resolve_profit_metrics(
        parent_ttm=parent_ttm,
        parent_prior=parent_prior,
        parent_yoy=parent_yoy,
        deduct_ttm=deduct_ttm,
        deduct_prior=deduct_prior,
        deduct_yoy=deduct_yoy,
    )
    # 加速看用于入池的利润序列（扣非优先）
    profit_s = deduct_s if resolved["used_deduct"] else parent_s
    accel = _near_yoy_accel(profit_s, resolved["np_yoy"])

    return {
        **resolved,
        "rev_ttm": rev_ttm,
        "rev_ttm_prior": rev_prior,
        "rev_ttm_yoy": rev_yoy,
        "ocf_ttm": ocf_ttm,
        "ocf_ttm_prior": ocf_prior,
        "profit_accel": accel,
    }


def _finalize_passed_info(info: GrowthStockInfo, track: str) -> None:
    info.track = track
    info.turnaround = (
        info.np_ttm is not None
        and info.np_ttm > 0
        and info.np_ttm_prior is not None
        and info.np_ttm_prior <= 0
    )
    if (
        track == "loss"
        and info.np_ttm is not None
        and info.np_ttm_prior is not None
        and info.np_ttm < 0
        and info.np_ttm_prior < 0
    ):
        narrow = (abs(info.np_ttm_prior) - abs(info.np_ttm)) / abs(info.np_ttm_prior)
        info.loss_narrowed = narrow >= _MIN_LOSS_NARROW
    info.ocf_score_delta = ocf_quality_score_delta(
        track=track,
        np_ttm=info.np_ttm,
        ocf_ttm=info.ocf_ttm,
        ocf_prior=info.ocf_ttm_prior,
    )


def run_l1_growth_filter_impl(stocks: list[GrowthStockInfo]) -> list[GrowthStockInfo]:
    """纯判定（测试用）：假定 TTM / 质量字段已填好。"""
    passed: list[GrowthStockInfo] = []
    for info in stocks:
        if is_nonrecurring_gap(info.parent_np_ttm_yoy, info.deduct_np_ttm_yoy):
            info.exclude_reason = "扣非显著低于归母"
            continue
        ok, track, reason = evaluate_growth_track(
            np_ttm=info.np_ttm,
            np_yoy=info.np_ttm_yoy,
            rev_ttm=info.rev_ttm,
            rev_yoy=info.rev_ttm_yoy,
            np_prior=info.np_ttm_prior,
        )
        if not ok:
            info.exclude_reason = reason
            continue
        _finalize_passed_info(info, track)
        passed.append(info)
    return passed


def run_l1b_filter(
    stocks: list[GrowthStockInfo],
    *,
    on_item: ItemCb | None = None,
) -> list[GrowthStockInfo]:
    passed: list[GrowthStockInfo] = []
    total = len(stocks)
    for idx, info in enumerate(stocks, 1):
        if on_item is not None:
            on_item(info.code, info.name, idx, total)
        try:
            metrics = _fetch_income_metrics(info.code)
        except Exception as e:
            logger.debug("成长 L1b 财报失败 %s: %s", info.code, e)
            info.exclude_reason = "指标缺失"
            continue

        info.np_ttm = metrics["np_ttm"]
        info.np_ttm_prior = metrics["np_prior"]
        info.np_ttm_yoy = metrics["np_yoy"]
        info.parent_np_ttm_yoy = metrics["parent_yoy"]
        info.deduct_np_ttm_yoy = metrics["deduct_yoy"]
        info.used_deduct = bool(metrics["used_deduct"])
        info.no_nonrecurring = bool(metrics["no_nonrecurring"])
        info.rev_ttm = metrics["rev_ttm"]
        info.rev_ttm_prior = metrics["rev_ttm_prior"]
        info.rev_ttm_yoy = metrics["rev_ttm_yoy"]
        info.ocf_ttm = metrics["ocf_ttm"]
        info.ocf_ttm_prior = metrics["ocf_ttm_prior"]
        info.profit_accel = bool(metrics["profit_accel"])
        info.revenue_accel = info.profit_accel

        if is_nonrecurring_gap(info.parent_np_ttm_yoy, info.deduct_np_ttm_yoy):
            info.exclude_reason = "扣非显著低于归母"
            continue

        ok, track, reason = evaluate_growth_track(
            np_ttm=info.np_ttm,
            np_yoy=info.np_ttm_yoy,
            rev_ttm=info.rev_ttm,
            rev_yoy=info.rev_ttm_yoy,
            np_prior=info.np_ttm_prior,
        )
        if not ok:
            info.exclude_reason = reason
            continue
        _finalize_passed_info(info, track)
        passed.append(info)

    logger.info("成长 L1b: 通过 %d 只", len(passed))
    return passed


# ── L2 ──────────────────────────────────────────────────────────────────────


def _check_growth_theme(code: str, hot_stocks: dict[str, list[str]]) -> bool:
    for tag, codes in hot_stocks.items():
        if code not in codes:
            continue
        if any(kw in tag for kw in _GROWTH_THEME_KEYWORDS):
            return True
    return False


def run_l2_rank_impl(
    stocks: list[GrowthStockInfo],
    max_candidates: int = _MAX_CANDIDATES,
) -> list[GrowthStockInfo]:
    scored: list[GrowthStockInfo] = []
    for info in stocks:
        info.signal_score = compute_growth_signal_score(info)
        scored.append(info)
    scored.sort(
        key=lambda x: (
            x.signal_score,
            x.np_ttm_yoy if x.np_ttm_yoy is not None else -999.0,
            x.volume_wan,
        ),
        reverse=True,
    )
    return scored[:max_candidates]


def run_l2_filter(
    stocks: list[GrowthStockInfo],
    max_candidates: int = _MAX_CANDIDATES,
    *,
    on_item: ItemCb | None = None,
) -> list[GrowthStockInfo]:
    hot = _load_hot_stocks()
    if stocks:
        vol_cut = sorted((s.volume_wan for s in stocks), reverse=True)
        mid = vol_cut[len(vol_cut) // 2] if vol_cut else 0.0
    else:
        mid = 0.0

    total = len(stocks)
    for idx, info in enumerate(stocks, 1):
        if on_item is not None:
            on_item(info.code, info.name, idx, total)
        info.ret_5d = _safe_call(_calc_ret_nd, info.code, default=None)
        info.growth_theme = _check_growth_theme(info.code, hot)
        info.high_liquidity = info.volume_wan >= mid
        # 一致预期质量闸门（L1b 窄池；东财限流）
        try:
            snap = fetch_consensus_snapshot(info.code, info.price)
            apply_gate_fields(
                info,
                score_growth_expectation(
                    actual_yoy=info.np_ttm_yoy,
                    snap=snap,
                    track=info.track or "profit",
                ),
            )
        except Exception as e:
            logger.debug("成长预期闸门失败 %s: %s", info.code, e)
    return run_l2_rank_impl(stocks, max_candidates=max_candidates)


# ── 扫描入口 ────────────────────────────────────────────────────────────────


def run_growth_accel_scan(
    max_candidates: int = _MAX_CANDIDATES,
    *,
    progress_cb: ProgressCb | None = None,
) -> ScanResult:
    ts = time.time()
    scan_date = datetime.now().strftime("%Y-%m-%d")
    result = ScanResult(scan_date=scan_date)
    logger.info("═══ 成长加速扫描 %s ═══", scan_date)

    result.total_stocks = len(_get_all_cn_codes())
    prog = _ScanProgress(progress_cb, result.total_stocks)
    prog.emit("L0", _PCT_L0_START)

    l0 = run_l0_filter()
    result.l0_passed = len(l0)
    prog.l0_passed = len(l0)
    prog.emit("L0", _PCT_L0_DONE)

    l1a = run_l1a_prefilter(l0)
    result.l1a_passed = len(l1a)
    prog.l1a_passed = len(l1a)
    prog.emit("L1a", _PCT_L1A_DONE)

    def _on_l1b(code: str, name: str, idx: int, total: int) -> None:
        prog.emit(
            "L1b",
            _interp(_PCT_L1A_DONE, _PCT_L1B_DONE, idx, total),
            code=code,
            name=name,
            stage_index=idx,
            stage_total=total,
        )

    l1b = run_l1b_filter(l1a, on_item=_on_l1b)
    result.l1b_passed = len(l1b)
    prog.l1b_passed = len(l1b)
    prog.emit("L1b", _PCT_L1B_DONE)

    def _on_l2(code: str, name: str, idx: int, total: int) -> None:
        prog.emit(
            "L2",
            _interp(_PCT_L1B_DONE, _PCT_L2_DONE, idx, total),
            code=code,
            name=name,
            stage_index=idx,
            stage_total=total,
        )

    l2 = run_l2_filter(l1b, max_candidates=max_candidates, on_item=_on_l2)
    result.l2_passed = len(l2)
    result.candidates = l2
    prog.l2_passed = len(l2)
    prog.emit("done", _PCT_L2_DONE)

    result.duration_seconds = time.time() - ts
    logger.info(
        "═══ 成长 %d→%d→%d→%d 候选, %.0fs ═══",
        result.l0_passed,
        result.l1a_passed,
        result.l1b_passed,
        result.l2_passed,
        result.duration_seconds,
    )
    return result
