"""A-stock (China mainland) data vendor for TradingAgents.

Zero third-party data dependency (no akshare). All sources are direct HTTP APIs
or mootdx TCP.

Data sources:
- mootdx (TCP 7709): OHLCV K-lines, financial snapshots, F10 text
- Tencent Finance (HTTP GBK): PE/PB/market cap/turnover
- 东方财富 push2 / push2delay / datacenter-web (direct HTTP): stock info,
  industry boards, consensus EPS (RPT_WEB_RESPREDICT), dragon-tiger, lockup
- 新浪财经 (direct HTTP): K-line fallback, financial statements, fund-flow history
- 同花顺 (direct HTTP): consensus EPS fallback, hot stocks, northbound capital flow
- 财联社 (direct HTTP): global news wire
"""

from __future__ import annotations

from typing import Annotated, Any
from contextlib import contextmanager
from datetime import datetime
from dateutil.relativedelta import relativedelta
from io import StringIO
from pathlib import Path
import json as _json
import os
import logging
import functools
import math
import random
import re as _re
import socket
import threading
import time
import uuid
import urllib.request

from tradingagents.runtime.arrow_safety import configure_arrow_memory_pool

# Before pandas/pyarrow: avoid mimalloc TLS SIGSEGV on Streamlit worker threads.
configure_arrow_memory_pool()

import pandas as pd
import requests as _requests

from .utils import safe_ticker_component

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TTL-Cached helpers (avoid redundant API calls within process lifetime)
# ---------------------------------------------------------------------------

_ttl_caches: dict[str, tuple[float, Any]] = {}
_TTL_SECONDS = 300  # 5 minutes


def _ttl_cache(key: str, ttl: int = _TTL_SECONDS) -> Any:
    """Get cached value by key, or None if expired/missing."""
    entry = _ttl_caches.get(key)
    if entry is not None and time.time() - entry[0] < ttl:
        return entry[1]
    return None


def _ttl_store(key: str, value: Any) -> None:
    _ttl_caches[key] = (time.time(), value)


# ---------------------------------------------------------------------------
# Helpers: ticker format & market detection
# ---------------------------------------------------------------------------

def _get_prefix(code: str) -> str:
    """6-digit A-stock code -> market prefix for Tencent API.

    The 92 prefix must be checked before the leading-9 rule: the Beijing Stock
    Exchange started issuing 920xxx codes for new listings in October 2024, and
    a bare ``startswith("9")`` routes them to Shanghai, where the Tencent quote
    endpoint returns an empty payload (issue #85).  Only 900xxx (Shanghai B
    shares) legitimately belongs to ``sh``.
    """
    if code.startswith("92"):
        return "bj"
    if code.startswith(("6", "9")):
        return "sh"
    elif code.startswith("8"):
        return "bj"
    return "sz"


def _normalize_ticker(symbol: str) -> str:
    """Strip exchange prefix/suffix, return pure 6-digit code.

    Handles: '688017', 'SH688017', '688017.SH', 'sh688017'
    """
    s = symbol.strip().upper()
    # Remove .SH / .SZ / .BJ suffix
    for suffix in (".SH", ".SZ", ".BJ"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            break
    # Remove SH / SZ / BJ prefix
    for prefix in ("SH", "SZ", "BJ"):
        if s.startswith(prefix):
            s = s[len(prefix) :]
            break
    return safe_ticker_component(s)


# ---------------------------------------------------------------------------
# Stock name <-> code mapping (cached)
# ---------------------------------------------------------------------------

_name_to_code: dict[str, str] | None = None
_code_to_name: dict[str, str] | None = None

# 构建名称映射的重试次数。mootdx 的 count/list 在 get_security_count() 瞬时返回
# None 时会抛 TypeError（socket 抖动/脏连接），一次异常不代表通达信真的挂了，
# 重连后大概率成功。
_NAME_MAP_MAX_ATTEMPTS = 3
# 串行化名称映射构建：mootdx TCP socket 非线程安全，Web UI 多 Agent 并发时
# 双重检查 + 加锁可避免重复构建与 socket 交叉污染。
_name_map_lock = threading.RLock()
# 通达信 get_security_list 分页大小（与 mootdx.stocks 一致）
_SECURITY_LIST_PAGE = 1000
# 全市场名称映射磁盘缓存（命中则不走远程通达信）。可用 ASTOCK_NAME_MAP_CACHE 覆盖路径。
# 代码/简称极少变更（IPO/更名/退市），TTL 宜长；过期后才尝试远程刷新，失败仍回退磁盘。
_NAME_MAP_DISK_TTL_SECONDS = 180 * 24 * 3600
_NAME_MAP_MIN_DISK_ENTRIES = 1000


def _name_map_cache_path() -> Path:
    override = os.environ.get("ASTOCK_NAME_MAP_CACHE", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".tradingagents" / "cache" / "astock_name_code_map.json"


def _normalize_name_map_pair(
    n2c_raw: Any, c2n_raw: Any
) -> tuple[dict[str, str], dict[str, str]] | None:
    if not isinstance(n2c_raw, dict) or not isinstance(c2n_raw, dict):
        return None
    n2c: dict[str, str] = {}
    c2n: dict[str, str] = {}
    for name, code in n2c_raw.items():
        clean_name = str(name).replace(" ", "").replace("　", "").strip()
        clean_code = str(code).strip()
        if clean_name and _re.match(r"^[036]\d{5}$", clean_code):
            n2c[clean_name] = clean_code
    for code, name in c2n_raw.items():
        clean_code = str(code).strip()
        clean_name = str(name).replace(" ", "").replace("　", "").strip()
        if clean_name and _re.match(r"^[036]\d{5}$", clean_code):
            c2n[clean_code] = clean_name
    if len(n2c) < _NAME_MAP_MIN_DISK_ENTRIES or len(c2n) < _NAME_MAP_MIN_DISK_ENTRIES:
        return None
    return n2c, c2n


def _load_name_map_disk() -> tuple[dict[str, str], dict[str, str], float] | None:
    """Load cached maps from disk. Returns (n2c, c2n, updated_at) or None."""
    path = _name_map_cache_path()
    try:
        if not path.exists():
            return None
        with open(path, encoding="utf-8") as f:
            payload = _json.load(f)
        if not isinstance(payload, dict):
            return None
        maps = _normalize_name_map_pair(
            payload.get("name_to_code"), payload.get("code_to_name")
        )
        if maps is None:
            return None
        updated_at = float(payload.get("updated_at") or 0.0)
        return maps[0], maps[1], updated_at
    except (OSError, TypeError, ValueError, _json.JSONDecodeError) as e:
        logger.warning("读取股票名称映射磁盘缓存失败：%s", e)
        return None


def _save_name_map_disk(n2c: dict[str, str], c2n: dict[str, str]) -> None:
    path = _name_map_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": time.time(),
            "name_to_code": n2c,
            "code_to_name": c2n,
        }
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(payload, f, ensure_ascii=False)
        tmp.replace(path)
        logger.info(
            "Persisted stock name-code map to disk: %d entries (%s)",
            len(n2c), path,
        )
    except OSError as e:
        logger.warning("写入股票名称映射磁盘缓存失败：%s", e)


def _iter_mootdx_security_rows(client, market: int):
    """Yield (code, name) from mootdx without pandas/DataFrame.

    故意不调用 ``client.stocks()``：该路径内部 ``pandas.concat`` / ``to_data``
    会走 pyarrow，在 macOS Streamlit 工作线程上可触发 libarrow mimalloc
    ``mi_thread_init`` SIGSEGV（进程直接 quit，exit 139）。
    """
    raw = getattr(client, "client", None)
    if raw is None or not hasattr(raw, "get_security_list"):
        raise RuntimeError("mootdx Quotes 缺少底层 get_security_list")

    count = client.stock_count(market=market)
    # 与 mootdx.stocks() 内部 `counts > 0` 相同的瞬时故障形态，交给外层重试
    if count is None:
        raise TypeError(
            "'>' not supported between instances of 'NoneType' and 'int'"
        )
    if not isinstance(count, int) or count <= 0:
        return

    for start in range(0, count, _SECURITY_LIST_PAGE):
        rows = raw.get_security_list(market=market, start=start) or []
        for row in rows:
            if isinstance(row, dict):
                code = str(row.get("code", "")).strip()
                name = str(row.get("name", "")).strip()
            else:
                code = str(getattr(row, "code", "")).strip()
                name = str(getattr(row, "name", "")).strip()
            if code and name:
                yield code, name


def _fetch_name_map_from_mootdx() -> tuple[dict[str, str], dict[str, str]]:
    """Pull SH/SZ name maps from mootdx with reconnect retries."""
    last_err: Exception | None = None
    for attempt in range(1, _NAME_MAP_MAX_ATTEMPTS + 1):
        try:
            # Hold mootdx lock for the full list pull so a concurrent
            # reset cannot close the TCP socket mid-iteration.
            with _mootdx_client_session() as client:
                n2c: dict[str, str] = {}
                c2n: dict[str, str] = {}
                for market in (0, 1):  # 0=SZ, 1=SH
                    for code, name in _iter_mootdx_security_rows(client, market):
                        if not _re.match(r"^[036]\d{5}$", code):
                            continue
                        clean_name = name.replace(" ", "").replace("　", "")
                        n2c[clean_name] = code
                        c2n[code] = clean_name

                if not n2c:
                    raise ValueError("mootdx 返回股票列表为空")
                return n2c, c2n
        except Exception as e:
            last_err = e
            logger.warning(
                "构建股票名称映射失败（第 %d/%d 次）：%s",
                attempt, _NAME_MAP_MAX_ATTEMPTS, e,
            )
            _reset_mootdx_client()
            if attempt < _NAME_MAP_MAX_ATTEMPTS:
                time.sleep(0.5 * attempt)

    raise ValueError(
        "无法通过 mootdx 解析股票名称（通达信服务暂时不可达）：%s。"
        "请稍后重试，或直接输入 6 位股票代码。" % last_err
    ) from last_err


def _build_name_code_map() -> tuple[dict[str, str], dict[str, str]]:
    """Build name→code and code→name maps (disk cache first, mootdx as refresh).

    优先级：
    1. 进程内内存
    2. 未过期的磁盘缓存（命中则**不走**远程通达信）
    3. mootdx 远程拉取（成功后落盘）
    4. 过期/任意可用磁盘缓存回退（通达信 RST/不可达时保底）

    实现刻意绕过 ``Quotes.stocks()``，避免分析线程上 pyarrow 段错误导致 Python quit。
    """
    global _name_to_code, _code_to_name
    if _name_to_code is not None:
        return _name_to_code, _code_to_name

    with _name_map_lock:
        if _name_to_code is not None:  # 双重检查：等锁期间别的线程可能已构建好
            return _name_to_code, _code_to_name

        disk = _load_name_map_disk()
        if disk is not None:
            n2c, c2n, updated_at = disk
            age = time.time() - updated_at
            if age <= _NAME_MAP_DISK_TTL_SECONDS:
                _name_to_code, _code_to_name = n2c, c2n
                logger.info(
                    "Loaded stock name-code map from disk cache: %d entries (age=%.0fs)",
                    len(n2c), max(age, 0.0),
                )
                return _name_to_code, _code_to_name

        try:
            n2c, c2n = _fetch_name_map_from_mootdx()
            _name_to_code, _code_to_name = n2c, c2n
            _save_name_map_disk(n2c, c2n)
            logger.info("Built stock name-code map: %d entries", len(n2c))
            return _name_to_code, _code_to_name
        except Exception as remote_err:
            if disk is not None:
                n2c, c2n, updated_at = disk
                _name_to_code, _code_to_name = n2c, c2n
                logger.warning(
                    "通达信拉取名称映射失败，回退磁盘缓存 %d 条（age=%.0fs）：%s",
                    len(n2c),
                    max(time.time() - updated_at, 0.0),
                    remote_err,
                )
                return _name_to_code, _code_to_name
            raise


def resolve_ticker(user_input: str) -> str:
    """Resolve user input (code or Chinese name) to a 6-digit A-stock code.

    Accepts: '600379', 'SH600379', '600379.SH', '宝光股份'
    Returns: '600379'
    Raises: ValueError if not resolvable.
    """
    s = user_input.strip()
    if not s:
        raise ValueError("输入不能为空")

    has_chinese = any("一" <= ch <= "鿿" for ch in s)

    if not has_chinese:
        return _normalize_ticker(s)

    clean = s.replace(" ", "").replace("　", "")
    n2c, _ = _build_name_code_map()

    if clean in n2c:
        return n2c[clean]

    matches = {name: code for name, code in n2c.items() if clean in name}
    if len(matches) == 1:
        return next(iter(matches.values()))
    if len(matches) > 1:
        examples = ", ".join(f"{n}({c})" for n, c in list(matches.items())[:5])
        raise ValueError(f"'{s}' 匹配到多只股票: {examples}，请输入完整名称或代码")

    # LLM 有时会把行业/概念名（如 '游戏'、'白酒'）当 ticker 传进来（#76）。
    # 报错必须写明原因和正确用法，让模型能在下一次工具调用中自我纠正。
    raise ValueError(
        f"找不到股票 '{s}'。ticker 参数只接受 6 位股票代码（如 '600519'）"
        f"或完整股票名称（如 '贵州茅台'）；行业/概念/板块名（如 '游戏'）不是"
        f"有效的股票标识。请改用目标个股的 6 位股票代码重试。"
    )


# ---------------------------------------------------------------------------
# mootdx client (singleton)
# ---------------------------------------------------------------------------

_mootdx_client = None
# mootdx/pytdx TCP socket 非线程安全；所有 RPC（含 reset）必须持此锁，
# 避免名称映射重试关闭连接时打断分析线程的 bars()/finance()/F10()。
_mootdx_lock = threading.RLock()

# 通达信 HQ 冗余列表。经典联通/电信骨干站优先（2026-07 在新加坡出口验证：
# 华为云镜像 TCP 通但 SetupCmd1 阶段 RST；仅 TCP probe 会误选坏节点）。
_TDX_SERVERS = [
    ("123.125.108.14", 7709), ("180.153.18.170", 7709),
    ("218.75.126.9", 7709), ("60.12.136.250", 7709),
    ("119.97.185.59", 7709), ("124.70.133.119", 7709), ("116.205.183.150", 7709),
    ("123.60.73.44", 7709), ("116.205.163.254", 7709), ("121.36.225.169", 7709),
    ("123.60.70.228", 7709), ("124.71.9.153", 7709), ("110.41.147.114", 7709),
    ("124.71.187.122", 7709),
]


def _probe_tdx(ip: str, port: int, timeout: float = 2.0) -> bool:
    """TCP 握手探测通达信服务器是否可达。"""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def _connect_tdx_quotes(ip: str, port: int, timeout: float = 5.0):
    """建立 mootdx Quotes 并做一次廉价 RPC，确认协议握手成功。

    仅 TCP connect 不够：部分 HQ 会 accept 后在 SetupCmd1 上 RST。
    """
    from mootdx.quotes import Quotes

    client = Quotes.factory(market="std", server=(ip, port), timeout=timeout)
    try:
        count = client.stock_count(market=0)
        if count is None:
            raise RuntimeError("stock_count returned None")
    except Exception:
        try:
            client.close()
        except Exception:
            pass
        raise
    return client


def _get_mootdx_client():
    """Lazy-init 健壮版 mootdx Quotes client（TCP 连接，可复用）。

    规避 mootdx 0.11.x BESTIP 空串 bug，并对每个候选做协议级校验：
    TCP 通但握手 RST 的节点会被跳过，继续尝试下一台。再 fallback 到
    bestip / 裸 factory。

    Prefer ``_mootdx_client_session()`` for RPCs so the lock covers the full call.
    """
    global _mootdx_client
    with _mootdx_lock:
        if _mootdx_client is not None:
            return _mootdx_client

        from mootdx.quotes import Quotes

        last_err: Exception | None = None
        for ip, port in _TDX_SERVERS:
            if not _probe_tdx(ip, port):
                continue
            try:
                _mootdx_client = _connect_tdx_quotes(ip, port)
                logger.info("mootdx connected via %s:%s", ip, port)
                return _mootdx_client
            except Exception as e:
                last_err = e
                logger.warning(
                    "mootdx handshake failed on %s:%s：%s", ip, port, e
                )
        try:
            client = Quotes.factory(market="std", bestip=True)  # fallback 1
            client.stock_count(market=0)
            _mootdx_client = client
            return _mootdx_client
        except Exception as e:
            last_err = e
        try:
            client = Quotes.factory(market="std")  # fallback 2（老用户 config 已有 IP）
            client.stock_count(market=0)
            _mootdx_client = client
            return _mootdx_client
        except Exception as e:
            last_err = e
            raise RuntimeError(
                "mootdx 通达信服务器均不可达（TCP 7709）。海外网络通常全部超时，"
                "请走国内代理或直接使用 6 位股票代码。原始错误：%s" % last_err
            ) from e


@contextmanager
def _mootdx_client_session():
    """Yield the singleton client while holding ``_mootdx_lock`` for the RPC."""
    with _mootdx_lock:
        yield _get_mootdx_client()


def _reset_mootdx_client() -> None:
    """丢弃当前 mootdx 单例并关闭连接，下次 _get_mootdx_client() 会重连。

    用于 socket 抖动/脏连接后强制重建（可能换到别的通达信服务器）。
    Serialized with in-flight RPCs via ``_mootdx_lock``.
    """
    global _mootdx_client
    with _mootdx_lock:
        client, _mootdx_client = _mootdx_client, None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Tencent Finance API
# ---------------------------------------------------------------------------

def _tencent_quote(codes: list[str]) -> dict[str, dict]:
    """Batch real-time quotes from Tencent Finance (qt.gtimg.cn).

    Returns dict[code] -> {name, price, pe_ttm, pb, mcap_yi, ...}
    Fields that fail to parse default to 0; individual bad stocks don't kill the batch.
    """
    prefixed = [f"{_get_prefix(c)}{c}" for c in codes]
    url = "https://qt.gtimg.cn/q=" + ",".join(prefixed)
    # Percent-encode non-ASCII characters so Chinese ticker values (e.g.
    # "完美世界" from an unresolveable LLM input) do not crash on
    # http.client._encode_request which calls request.encode("ascii").
    url = urllib.parse.quote(url, safe=":/?=&,")
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0")
    resp = urllib.request.urlopen(req, timeout=10)
    raw = resp.read().decode("gbk")

    def _sf(v: str) -> float:
        """Safe float parse, returns 0.0 on failure."""
        try:
            return float(v) if v else 0.0
        except (ValueError, TypeError):
            return 0.0

    result = {}
    for line in raw.strip().split(";"):
        if not line.strip() or "=" not in line or '"' not in line:
            continue
        key = line.split("=")[0].split("_")[-1]
        vals = line.split('"')[1].split("~")
        if len(vals) < 53:
            continue
        code = key[2:]  # strip sh/sz/bj prefix
        result[code] = {
            "name": vals[1],
            "price": _sf(vals[3]),
            "last_close": _sf(vals[4]),
            "open": _sf(vals[5]),
            "change_pct": _sf(vals[32]),
            "high": _sf(vals[33]),
            "low": _sf(vals[34]),
            "turnover_pct": _sf(vals[38]),
            "pe_ttm": _sf(vals[39]),
            "mcap_yi": _sf(vals[44]),
            "float_mcap_yi": _sf(vals[45]),
            "pb": _sf(vals[46]),
            "limit_up": _sf(vals[47]),
            "limit_down": _sf(vals[48]),
            "pe_static": _sf(vals[52]),
        }
    return result


# ---------------------------------------------------------------------------
# Price monitor helpers (watchlist buy-zone scanning)
# ---------------------------------------------------------------------------

def batch_get_spot_prices(tickers: list[str]) -> dict[str, float]:
    """Fetch current prices for a batch of A-share tickers.

    Returns ``{ticker: current_price}``. Uses the Tencent Finance batch API
    (no Eastmoney throttling needed). Unresolvable tickers or API errors
    produce no entry (caller checks ``ticker in result``).
    """
    codes = [str(t).strip() for t in tickers if str(t).strip()]
    if not codes:
        return {}
    try:
        quotes = _tencent_quote(codes)
        return {code: info["price"] for code, info in quotes.items() if info.get("price", 0) > 0}
    except Exception:
        logger.warning("batch_get_spot_prices failed for %d tickers", len(codes), exc_info=True)
        return {}


# ---------------------------------------------------------------------------
# Eastmoney Datacenter unified helper (龙虎榜/解禁 etc.)
# ---------------------------------------------------------------------------

_DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


# ---------------------------------------------------------------------------
# 东财防封：全局节流 + 会话复用 (Eastmoney anti-ban: throttle + Keep-Alive)
# ---------------------------------------------------------------------------
# 东财系 HTTP 接口（push2 / push2his / datacenter-web / search-api / np-weblist）
# 有风控：每秒 >5 次 / 单 IP 并发 ≥10 / 1 分钟 ≥200 次 / 5 分钟 ≥300 次 → 临时封 IP。
# 多 Agent 投研跑批量分析时会高频请求东财，是被封的头号元凶。所有 eastmoney.com
# 请求一律走 _em_get()：有限并发限流 + 复用 Keep-Alive 会话 + 默认 UA。
# 注意：仅东财接口走此入口；mootdx(TCP) / 腾讯 / 新浪 / 同花顺 / 财联社 / 百度 等
# 不限流（实测不封 IP 或风控极弱）。批量任务可调大 EM_MIN_INTERVAL 进一步降速。
_EM_SESSION = _requests.Session()
_EM_SESSION.headers.update({"User-Agent": _UA})
# 两次东财请求最小间隔(秒)；批量多 Agent 场景可设环境变量 EM_MIN_INTERVAL=1.5~2 降速。
_EM_MIN_INTERVAL = float(os.environ.get("EM_MIN_INTERVAL", "1.0"))
# 最大并发东财请求数。3 路并行 × 1.0s 间隔 ≈ 3 QPS，仍远低于东财 5 QPS 封禁线。
# 设环境变量 EM_MAX_CONCURRENT=1 恢复旧串行行为。
_EM_MAX_CONCURRENT = int(os.environ.get("EM_MAX_CONCURRENT", "3"))
_em_semaphore = threading.Semaphore(_EM_MAX_CONCURRENT)
_em_last_call = [0.0]  # 模块级上次东财请求时间戳
# SSL/连接闪断在东财 push2his 上偶发；短暂重试通常可恢复（如主力资金日 K）。
_EM_MAX_ATTEMPTS = 3
_EM_RETRY_BASE_DELAY = 0.5
_EM_RETRY_EXCEPTIONS = (
    _requests.exceptions.SSLError,
    _requests.exceptions.ConnectionError,
    _requests.exceptions.Timeout,
    _requests.exceptions.ChunkedEncodingError,
)


def _em_get(url, params=None, headers=None, timeout=15, **kwargs):
    """东财统一请求入口：自动节流 + 复用 session + 默认 UA + 瞬态重试。

    所有 eastmoney.com 接口都应通过它请求，避免多 Agent 高频拉数据被封 IP。
    有限并发限流：最多 _EM_MAX_CONCURRENT 个线程同时请求。
    每个请求前等待确保距上次请求 ≥ _EM_MIN_INTERVAL + 0.1~0.5s 抖动。
    传入的 headers 会覆盖 session 默认 UA（用于保留各端点自己的 Referer/Origin）。
    SSL/连接/超时类错误最多重试 _EM_MAX_ATTEMPTS 次（指数退避）。
    """
    acquired = _em_semaphore.acquire(timeout=30)
    if not acquired:
        raise TimeoutError("东财请求队列排队超时（30s），请降速或加大 EM_MAX_CONCURRENT。")

    try:
        wait = _EM_MIN_INTERVAL - (time.time() - _em_last_call[0])
        if wait > 0:
            time.sleep(wait + random.uniform(0.1, 0.5))

        last_exc: Exception | None = None
        for attempt in range(_EM_MAX_ATTEMPTS):
            try:
                return _EM_SESSION.get(
                    url, params=params, headers=headers, timeout=timeout, **kwargs
                )
            except _EM_RETRY_EXCEPTIONS as exc:
                last_exc = exc
                if attempt + 1 >= _EM_MAX_ATTEMPTS:
                    break
                delay = _EM_RETRY_BASE_DELAY * (2**attempt) + random.uniform(0.0, 0.2)
                logger.warning(
                    "eastmoney request failed (%s), retry %d/%d in %.1fs: %s",
                    type(exc).__name__,
                    attempt + 1,
                    _EM_MAX_ATTEMPTS - 1,
                    delay,
                    url,
                )
                time.sleep(delay)
            finally:
                _em_last_call[0] = time.time()
        assert last_exc is not None
        raise last_exc
    finally:
        _em_semaphore.release()


def _eastmoney_datacenter(
    report_name: str,
    columns: str = "ALL",
    filter_str: str = "",
    page_size: int = 50,
    sort_columns: str = "",
    sort_types: str = "-1",
) -> list[dict]:
    """东财数据中心统一查询 — 龙虎榜/解禁 共用."""
    params = {
        "reportName": report_name,
        "columns": columns,
        "filter": filter_str,
        "pageNumber": "1",
        "pageSize": str(page_size),
        "sortColumns": sort_columns,
        "sortTypes": sort_types,
        "source": "WEB",
        "client": "WEB",
    }
    r = _em_get(_DATACENTER_URL, params=params, timeout=15)
    d = r.json()
    if d.get("result") and d["result"].get("data"):
        return d["result"]["data"]
    return []


# ---------------------------------------------------------------------------
# Consensus EPS helpers (东财 primary, 同花顺 fallback)
# ---------------------------------------------------------------------------


def _em_eps_forecast(code: str) -> tuple[list[dict], int]:
    """Fetch consensus EPS from Eastmoney RPT_WEB_RESPREDICT.

    Returns (rows, org_count) where each row is
    ``{"year": str, "eps": float, "analysts": int}``.
    """
    try:
        data = _eastmoney_datacenter(
            report_name="RPT_WEB_RESPREDICT",
            columns="WEB_RESPREDICT",
            filter_str=f'(SECURITY_CODE="{code}")',
            page_size=5,
            sort_columns="RATING_ORG_NUM",
            sort_types="-1",
        )
    except Exception as exc:
        logger.warning("Eastmoney EPS forecast failed for %s: %s", code, exc)
        return [], 0

    if not data:
        return [], 0

    row = data[0]
    try:
        org_count = int(row.get("RATING_ORG_NUM") or 0)
    except (TypeError, ValueError):
        org_count = 0

    records: list[dict] = []
    for idx in range(1, 5):
        year_raw = row.get(f"YEAR{idx}")
        eps_raw = row.get(f"EPS{idx}")
        if year_raw is None or eps_raw is None:
            continue
        try:
            year = str(int(year_raw))
        except (TypeError, ValueError):
            year = str(year_raw).strip()
        try:
            eps = float(eps_raw)
        except (TypeError, ValueError):
            continue
        if not year:
            continue
        records.append({"year": year, "eps": eps, "analysts": org_count})
    return records, org_count


def _ths_eps_forecast(code: str) -> pd.DataFrame:
    """Fetch consensus EPS forecast from 同花顺 (direct HTTP).

    Returns DataFrame with columns roughly: 年度, 预测机构数, 最小值, 均值, 最大值.
    SPA pages and HTML parse failures return an empty DataFrame (no exception).
    """
    url = f"https://basic.10jqka.com.cn/new/{code}/worth.html"
    headers = {
        "User-Agent": _UA,
        "Referer": "https://basic.10jqka.com.cn/",
    }
    try:
        r = _requests.get(url, headers=headers, timeout=15)
        r.encoding = "gbk"
        html = r.text or ""
        if not html.strip() or "<table" not in html.lower():
            # Modern F10 pages are JS-rendered SPAs without static tables
            return pd.DataFrame()
        # Pass a file-like buffer — pandas treats bare strings as paths.
        dfs = pd.read_html(StringIO(html))
    except Exception as exc:
        logger.warning("Consensus EPS forecast failed for %s: %s", code, exc)
        return pd.DataFrame()

    for df in dfs:
        cols = [str(c) for c in df.columns]
        if any("每股收益" in c or "均值" in c for c in cols):
            return df
    return dfs[0] if dfs else pd.DataFrame()


def _ths_eps_records(code: str) -> list[dict]:
    """Normalize THS EPS DataFrame into ``{year, eps, analysts}`` records."""
    df = _ths_eps_forecast(code)
    if df is None or df.empty:
        return []
    records: list[dict] = []
    for _, row in df.iterrows():
        year = str(row.iloc[0]) if len(row) > 0 else ""
        count_val = row.iloc[1] if len(row) > 1 else 0
        mean_eps_val = row.iloc[3] if len(row) > 3 else 0
        try:
            count = int(count_val)
        except (ValueError, TypeError):
            count = 0
        try:
            mean_eps = float(mean_eps_val)
        except (ValueError, TypeError):
            mean_eps = 0.0
        if not year:
            continue
        records.append({"year": year, "eps": mean_eps, "analysts": count})
    return records


def _format_eps_forecast_lines(
    code: str,
    records: list[dict],
    source_label: str,
) -> str:
    """Render consensus EPS records plus optional forward PE/PEG."""
    lines = [
        f"# Consensus EPS Forecast for {code} (A-stock)",
        f"# Source: {source_label}",
        f"# Retrieved: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]
    eps_by_year: dict[str, float] = {}
    for item in records:
        year = str(item.get("year") or "")
        mean_eps = float(item.get("eps") or 0)
        count = int(item.get("analysts") or 0)
        lines.append(f"FY{year}: EPS={mean_eps}, analysts={count}")
        if count and count < 3:
            lines.append("  Warning: low coverage (<3 analysts)")
        if year:
            eps_by_year[year] = mean_eps

    try:
        tq = _tencent_quote([code])
        if code in tq:
            price = tq[code]["price"]
            pe_ttm = tq[code]["pe_ttm"]
            lines.append(f"\nCurrent: price={price}, PE(TTM)={pe_ttm}")
            years_sorted = sorted(eps_by_year.keys())
            if years_sorted and eps_by_year.get(years_sorted[0], 0) > 0:
                eps_cur = eps_by_year[years_sorted[0]]
                fwd_pe = price / eps_cur
                lines.append(f"Forward PE (FY{years_sorted[0]}): {fwd_pe:.1f}x")
                if (
                    len(years_sorted) >= 2
                    and eps_by_year.get(years_sorted[1], 0) > 0
                ):
                    eps_next = eps_by_year[years_sorted[1]]
                    cagr = eps_next / eps_cur - 1
                    if cagr > 0:
                        peg = fwd_pe / (cagr * 100)
                        lines.append(f"PEG: {peg:.2f} (CAGR={cagr * 100:.0f}%)")
                        if fwd_pe > 30:
                            digest = math.log(fwd_pe / 30) / math.log(1 + cagr)
                            lines.append(
                                f"PE Digestion to 30x: {digest:.1f} years"
                            )
                    else:
                        lines.append(
                            f"EPS declining ({cagr * 100:.0f}%), "
                            f"PEG not applicable"
                        )
    except Exception as e:
        logger.warning("Forward PE calc failed for %s: %s", code, e)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sina K-line fallback helper (direct HTTP, no akshare)
# ---------------------------------------------------------------------------


def _sina_kline_fallback(code: str, start_date: str = None, end_date: str = None) -> pd.DataFrame:
    """Fetch daily K-line from Sina HTTP API as mootdx fallback.

    Returns DataFrame with columns: Date, Open, High, Low, Close, Volume.
    """
    prefix = "sh" if code.startswith("6") else "sz"
    url = (
        "http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "CN_MarketData.getKLineData"
    )
    params = {
        "symbol": f"{prefix}{code}",
        "scale": "240",  # daily
        "ma": "no",
        "datalen": "800",
    }
    r = _requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    data = _json.loads(r.text)

    if not data:
        return pd.DataFrame()

    rows = []
    for item in data:
        rows.append({
            "Date": item["day"],
            "Open": float(item["open"]),
            "High": float(item["high"]),
            "Low": float(item["low"]),
            "Close": float(item["close"]),
            "Volume": int(item["volume"]),
        })

    df = pd.DataFrame(rows)
    df["Date"] = pd.to_datetime(df["Date"])

    if start_date:
        df = df[df["Date"] >= pd.to_datetime(start_date)]
    if end_date:
        df = df[df["Date"] <= pd.to_datetime(end_date)]

    return df


def _last_ohlcv_date(df: pd.DataFrame) -> pd.Timestamp | None:
    """Return the latest OHLCV Date in a normalized dataframe."""
    if df is None or df.empty or "Date" not in df.columns:
        return None
    dates = pd.to_datetime(df["Date"], errors="coerce")
    if dates.dropna().empty:
        return None
    return dates.max().normalize()


def _normalize_ohlcv_dates(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize OHLCV Date values to daily granularity."""
    if df is None or df.empty or "Date" not in df.columns:
        return df
    df = df.copy()
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce").dt.normalize()
    return df.dropna(subset=["Date"])


def _needs_sina_supplement(df: pd.DataFrame, target_date: str | None) -> bool:
    """True when mootdx/cache data is older than the requested cutoff date."""
    if not target_date:
        return False
    last_date = _last_ohlcv_date(df)
    if last_date is None:
        return True
    target = pd.to_datetime(target_date).normalize()
    return last_date < target


def _merge_ohlcv(primary: pd.DataFrame, supplement: pd.DataFrame) -> pd.DataFrame:
    """Merge OHLCV frames, preferring supplement rows on duplicate dates."""
    frames = [frame for frame in (primary, supplement) if frame is not None and not frame.empty]
    if not frames:
        return pd.DataFrame(columns=["Date", "Open", "High", "Low", "Close", "Volume"])
    combined = pd.concat(frames, ignore_index=True)
    combined = _normalize_ohlcv_dates(combined)
    combined = combined.drop_duplicates(subset=["Date"], keep="last")
    combined = combined.sort_values("Date").reset_index(drop=True)
    return combined


def _supplement_stale_ohlcv_with_sina(
    code: str,
    df: pd.DataFrame,
    target_date: str | None,
    start_date: str | None = None,
) -> tuple[pd.DataFrame, bool]:
    """Use Sina daily K-line to fill dates missing from mootdx/cache data."""
    if not _needs_sina_supplement(df, target_date):
        return df, False
    try:
        sina_df = _sina_kline_fallback(code, start_date, target_date)
    except Exception as e:
        logger.warning("sina K-line supplement failed for %s: %s", code, e)
        return df, False
    if sina_df.empty:
        return df, False
    merged = _merge_ohlcv(df, sina_df)
    return merged, _last_ohlcv_date(merged) != _last_ohlcv_date(df)


# ---------------------------------------------------------------------------
# OHLCV loading with cache (mootdx -> CSV)
# ---------------------------------------------------------------------------

def _load_ohlcv_astock(symbol: str, curr_date: str) -> pd.DataFrame:
    """Fetch OHLCV via mootdx, cache to CSV, filter by curr_date.

    Mirrors stockstats_utils.load_ohlcv but uses mootdx instead of yfinance.
    Returns DataFrame with columns: Date, Open, High, Low, Close, Volume
    """
    from .config import get_config

    code = _normalize_ticker(symbol)
    config = get_config()
    cache_dir = config.get(
        "data_cache_dir", os.path.expanduser("~/.tradingagents/cache")
    )
    os.makedirs(cache_dir, exist_ok=True)

    cache_file = os.path.join(cache_dir, f"{code}-astock-daily.csv")

    if os.path.exists(cache_file):
        mtime = datetime.fromtimestamp(os.path.getmtime(cache_file))
        if mtime.date() == datetime.now().date():
            data = pd.read_csv(cache_file, on_bad_lines="skip", encoding="utf-8")
            data = _normalize_ohlcv_dates(data)
            data, supplemented = _supplement_stale_ohlcv_with_sina(
                code, data, curr_date, start_date=None
            )
            if supplemented:
                data.to_csv(cache_file, index=False, encoding="utf-8")
            cutoff = pd.to_datetime(curr_date)
            return data[data["Date"] <= cutoff]

    # Fetch from mootdx — 800 daily bars (~3 years of trading days)
    try:
        with _mootdx_client_session() as client:
            df = client.bars(symbol=code, category=4, offset=800)

        if df is None or df.empty:
            raise ValueError(f"No OHLCV data from mootdx for {code}")

        # mootdx returns index named 'datetime' AND a column named 'datetime'
        # (plus year/month/day/hour/minute/volume). Drop duplicates before reset.
        df = df.drop(columns=["datetime", "year", "month", "day", "hour", "minute"], errors="ignore")
        df = df.reset_index()  # moves index 'datetime' → column 'datetime'
        rename_map = {
            "datetime": "Date",
            "open": "Open",
            "close": "Close",
            "high": "High",
            "low": "Low",
            "volume": "Volume",
        }
        df = df.rename(columns=rename_map)
        df = df[["Date", "Open", "High", "Low", "Close", "Volume"]]
        df = _normalize_ohlcv_dates(df)
    except Exception as e:
        logger.warning("mootdx OHLCV failed for %s: %s, trying sina HTTP fallback", code, e)
        # Fallback: Sina direct HTTP API
        try:
            df = _sina_kline_fallback(code)
            if df.empty:
                raise ValueError(f"No OHLCV data from sina for {code}")
        except Exception:
            raise ValueError(f"No OHLCV data from mootdx/sina for {code}")

    df, _ = _supplement_stale_ohlcv_with_sina(code, df, curr_date, start_date=None)

    # Cache to disk
    df.to_csv(cache_file, index=False, encoding="utf-8")

    # Filter by curr_date to prevent look-ahead bias
    cutoff = pd.to_datetime(curr_date)
    return df[df["Date"] <= cutoff]


# ===========================================================================
# 9 Vendor Methods (matching interface.py VENDOR_METHODS signatures)
# ===========================================================================


# ---- 1. get_stock_data ----


def get_stock_data(
    symbol: Annotated[str, "A-stock code (e.g. 688017, SH688017)"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
) -> str:
    """Get OHLCV stock price data via mootdx."""
    code = _normalize_ticker(symbol)

    data_source = "mootdx (TCP)"
    try:
        with _mootdx_client_session() as client:
            df = client.bars(symbol=code, category=4, offset=800)

        if df is None or df.empty:
            raise ValueError(f"No data from mootdx for {code}")

        # Drop duplicate datetime column + extra columns before reset_index
        df = df.drop(
            columns=["datetime", "year", "month", "day", "hour", "minute"],
            errors="ignore",
        )
        df = df.reset_index()  # index 'datetime' → column 'datetime'
        df = df.rename(
            columns={
                "datetime": "Date",
                "open": "Open",
                "close": "Close",
                "high": "High",
                "low": "Low",
                "volume": "Volume",
                "amount": "Amount",
            }
        )
        df = _normalize_ohlcv_dates(df)

    except Exception as e:
        logger.warning("mootdx K-line failed for %s: %s, trying sina HTTP fallback", code, e)
        # Fallback: Sina direct HTTP API
        try:
            # Fetch the full available history (not just the requested window) so
            # the first bar of the window still carries a real PrevClose/ChangePct.
            df = _sina_kline_fallback(code)
            if df.empty:
                return "K线数据获取失败：mootdx和新浪备用源均不可用，请检查网络连接"
            data_source = "sina HTTP (fallback)"
        except Exception:
            return "K线数据获取失败：mootdx和新浪备用源均不可用，请检查网络连接"

    df, supplemented = _supplement_stale_ohlcv_with_sina(code, df, end_date, start_date)
    if supplemented:
        data_source = f"{data_source} + sina HTTP supplement"

    # Sort chronologically and derive PrevClose + official daily change BEFORE the
    # window filter, so every row in the window shows the true close-to-close
    # (vs previous trading day) change even when that previous day lies outside
    # the requested range.
    df = df.sort_values("Date").reset_index(drop=True)
    df["PrevClose"] = df["Close"].shift(1)
    df["ChangePct"] = ((df["Close"] - df["PrevClose"]) / df["PrevClose"] * 100).round(2)

    # Filter by date range
    start_dt = pd.to_datetime(start_date)
    end_dt = pd.to_datetime(end_date)
    df = df[(df["Date"] >= start_dt) & (df["Date"] <= end_dt)]

    if df.empty:
        return (
            f"No data found for A-stock '{code}' "
            f"between {start_date} and {end_date}"
        )

    for col in ["Open", "High", "Low", "Close", "PrevClose"]:
        if col in df.columns:
            df[col] = df[col].round(2)

    df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")
    csv_out = df[["Date", "Open", "High", "Low", "Close", "ChangePct", "Volume", "PrevClose"]].to_csv(
        index=False
    )

    header = f"# Stock data for {code} (A-stock) from {start_date} to {end_date}\n"
    header += f"# Total records: {len(df)}\n"
    header += f"# Data source: {data_source}\n"
    header += "# Columns: Date,Open,High,Low,Close,ChangePct,Volume,PrevClose\n"
    header += (
        "# ChangePct = official daily change vs previous trading day close (%) — "
        "当日涨跌幅请直接引用该列，勿用 (Close-Open)/Open 代替。\n"
    )
    header += (
        f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    )

    return header + csv_out


# ---- 2. get_indicators ----

# Supported technical indicators with descriptions
_INDICATOR_DESCRIPTIONS = {
    "close_50_sma": "50 SMA: Medium-term trend indicator.",
    "close_200_sma": "200 SMA: Long-term trend benchmark.",
    "close_10_ema": "10 EMA: Responsive short-term average.",
    "macd": "MACD: Momentum via EMA differences.",
    "macds": "MACD Signal: EMA smoothing of MACD line.",
    "macdh": "MACD Histogram: Gap between MACD and signal.",
    "rsi": "RSI: Momentum overbought/oversold indicator (70/30 thresholds).",
    "boll": "Bollinger Middle: 20 SMA basis for Bollinger Bands.",
    "boll_ub": "Bollinger Upper Band: 2 std devs above middle.",
    "boll_lb": "Bollinger Lower Band: 2 std devs below middle.",
    "atr": "ATR: Average True Range volatility measure.",
    "vwma": "VWMA: Volume-weighted moving average.",
    "mfi": "MFI: Money Flow Index (volume + price momentum).",
}


def get_indicators(
    symbol: Annotated[str, "A-stock code"],
    indicator: Annotated[
        str, "technical indicator (e.g. rsi, macd, close_50_sma)"
    ],
    curr_date: Annotated[str, "Current trading date, YYYY-mm-dd"],
    look_back_days: Annotated[int, "how many days to look back"],
) -> str:
    """Get technical indicators using stockstats on mootdx OHLCV data."""
    from stockstats import wrap

    code = _normalize_ticker(symbol)

    if indicator not in _INDICATOR_DESCRIPTIONS:
        raise ValueError(
            f"Indicator {indicator} not supported. "
            f"Choose from: {list(_INDICATOR_DESCRIPTIONS.keys())}"
        )

    try:
        data = _load_ohlcv_astock(code, curr_date)
        df = wrap(data)
        df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")

        # Trigger stockstats calculation
        df[indicator]

        # Build date -> value lookup
        ind_dict = {}
        for _, row in df.iterrows():
            d = row["Date"]
            v = row[indicator]
            ind_dict[d] = "N/A" if pd.isna(v) else str(round(float(v), 4))

        # Generate output for look_back window
        curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
        before = curr_dt - relativedelta(days=look_back_days)

        lines = []
        dt = curr_dt
        while dt >= before:
            ds = dt.strftime("%Y-%m-%d")
            val = ind_dict.get(ds, "N/A: Not a trading day (weekend or holiday)")
            lines.append(f"{ds}: {val}")
            dt -= relativedelta(days=1)

        result = (
            f"## {indicator} values for {code} "
            f"from {before.strftime('%Y-%m-%d')} to {curr_date}:\n\n"
            + "\n".join(lines)
            + "\n\n"
            + _INDICATOR_DESCRIPTIONS.get(indicator, "")
        )
        return result

    except Exception as e:
        return f"Error calculating {indicator} for {code}: {str(e)}"


# ---- 3. get_fundamentals ----


def get_fundamentals(
    ticker: Annotated[str, "A-stock code"],
    curr_date: Annotated[str, "current date"] = None,
) -> str:
    """Get company fundamentals from Tencent + mootdx + Eastmoney + 同花顺."""
    code = _normalize_ticker(ticker)

    try:
        lines = []

        # --- Tencent: real-time valuation ---
        try:
            tq = _tencent_quote([code])
            if code in tq:
                q = tq[code]
                lines.extend(
                    [
                        f"Name: {q['name']}",
                        f"Price: {q['price']}",
                        f"PE (TTM): {q['pe_ttm']}",
                        f"PE (Static): {q['pe_static']}",
                        f"PB: {q['pb']}",
                        f"Market Cap (100M CNY): {q['mcap_yi']}",
                        f"Float Market Cap (100M CNY): {q['float_mcap_yi']}",
                        f"Turnover Rate: {q['turnover_pct']}%",
                        f"Change: {q['change_pct']}%",
                        f"Limit Up: {q['limit_up']}",
                        f"Limit Down: {q['limit_down']}",
                    ]
                )
        except Exception as e:
            logger.warning("Tencent quote failed for %s: %s", code, e)

        # --- mootdx: financial snapshot (quarterly) ---
        try:
            with _mootdx_client_session() as client:
                fin = client.finance(symbol=code)
            if fin is not None and not (
                isinstance(fin, pd.DataFrame) and fin.empty
            ):
                row = fin.iloc[0] if isinstance(fin, pd.DataFrame) else fin
                field_map = {
                    "eps": "EPS (Quarterly)",
                    "bvps": "Book Value Per Share",
                    "roe": "ROE (%)",
                    "profit": "Net Profit",
                    "income": "Revenue",
                    "liutongguben": "Float Shares",
                    "zongguben": "Total Shares",
                }
                idx = row.index if hasattr(row, "index") else []
                for field, label in field_map.items():
                    if field in idx:
                        val = row[field]
                        if val is not None and str(val) != "nan":
                            lines.append(f"{label}: {val}")
        except Exception as e:
            logger.warning("mootdx finance failed for %s: %s", code, e)

        # --- Eastmoney push2: basic stock info (direct HTTP) ---
        try:
            market_code = 1 if code.startswith("6") else 0
            _info_url = "https://push2.eastmoney.com/api/qt/stock/get"
            _info_params = {
                "fltt": "2",
                "invt": "2",
                "fields": "f57,f58,f84,f85,f127,f116,f117,f189,f43",
                "secid": f"{market_code}.{code}",
            }
            r = _em_get(_info_url, params=_info_params, timeout=10)
            d = r.json().get("data", {})
            if d:
                if d.get("f127"):
                    lines.append(f"行业: {d['f127']}")
                if d.get("f84"):
                    lines.append(f"总股本: {d['f84']}")
                if d.get("f85"):
                    lines.append(f"流通股本: {d['f85']}")
                if d.get("f116"):
                    lines.append(f"总市值: {d['f116']}")
                if d.get("f117"):
                    lines.append(f"流通市值: {d['f117']}")
                if d.get("f189"):
                    lines.append(f"上市日期: {d['f189']}")
        except Exception as e:
            logger.warning("eastmoney push2 stock info failed for %s: %s", code, e)

        # --- Consensus EPS: 东财 primary, 同花顺 fallback (+ forward PE/PEG) ---
        try:
            em_records, _org = _em_eps_forecast(code)
            if em_records:
                block = _format_eps_forecast_lines(
                    code,
                    em_records,
                    "东财 RPT_WEB_RESPREDICT (Eastmoney datacenter)",
                )
                lines.append("\n--- Consensus EPS Forecast (东财) ---")
                lines.extend(block.splitlines()[4:])  # skip standalone title/meta
            else:
                ths_records = _ths_eps_records(code)
                if ths_records:
                    block = _format_eps_forecast_lines(
                        code,
                        ths_records,
                        "同花顺 analyst consensus (fallback)",
                    )
                    lines.append("\n--- Consensus EPS Forecast (同花顺 fallback) ---")
                    lines.extend(block.splitlines()[4:])
                else:
                    lines.append(
                        "\n--- Consensus EPS Forecast ---\n"
                        "No analyst coverage found (东财+同花顺均无数据；"
                        "请表述为「无公开一致预期覆盖」，勿标 [数据缺失])"
                    )
        except Exception as e:
            logger.warning("Consensus EPS forecast failed for %s: %s", code, e)

        if not lines:
            return f"No fundamentals data found for A-stock '{code}'"

        header = f"# Company Fundamentals for {code} (A-stock)\n"
        header += (
            f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        )

        return header + "\n".join(lines)

    except Exception as e:
        return f"Error retrieving fundamentals for {code}: {str(e)}"


# ---- 4. get_balance_sheet ----


def _sina_stock_code(code: str) -> str:
    """Pure 6-digit code → sina format (sh688017 / sz000001 / bj832000)."""
    return f"{_get_prefix(code)}{code}"


def _sina_report_list_to_df(report_list: dict) -> pd.DataFrame:
    """Convert Sina report_list dict → wide DataFrame (one row per period)."""
    if not isinstance(report_list, dict) or not report_list:
        return pd.DataFrame()

    rows: list[dict] = []
    for date_key, report in report_list.items():
        if not isinstance(report, dict):
            continue
        row: dict = {
            "报告日": date_key,
            "publish_date": report.get("publish_date", ""),
            "rType": report.get("rType", ""),
        }
        for item in report.get("data") or []:
            if not isinstance(item, dict):
                continue
            title = (item.get("item_title") or "").strip()
            value = item.get("item_value")
            if not title or value in (None, ""):
                continue
            row[title] = value
        rows.append(row)

    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def _filter_financial_df(
    df: pd.DataFrame,
    freq: str,
    curr_date: str | None,
    *,
    limit: int = 8,
) -> pd.DataFrame:
    if df.empty or "报告日" not in df.columns:
        return df

    df = df.copy()
    df["报告日"] = pd.to_datetime(df["报告日"], errors="coerce")
    df = df.dropna(subset=["报告日"])

    if curr_date:
        cutoff = pd.to_datetime(curr_date)
        df = df[df["报告日"] <= cutoff]

    if freq.lower() == "annual":
        df = df[df["报告日"].dt.month == 12]

    n = max(1, int(limit))
    return df.sort_values("报告日", ascending=False).head(n).reset_index(drop=True)


def _get_financial_report_sina(
    code: str,
    report_type: str,
    freq: str,
    curr_date: str = None,
    *,
    limit: int = 8,
) -> pd.DataFrame:
    """Shared helper: fetch financial report via Sina direct HTTP API.

    report_type: '资产负债表' | '利润表' | '现金流量表'

    Sina currently returns ``report_list`` keyed by YYYYMMDD. Legacy payloads
    that expose a top-level ``fzb``/``lrb``/``llb`` list are still accepted.
    """
    _report_type_map = {
        "资产负债表": "fzb",
        "利润表": "lrb",
        "现金流量表": "llb",
    }
    source_type = _report_type_map.get(report_type, "lrb")

    paper_code = _sina_stock_code(code)
    url = "https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022"
    params = {
        "paperCode": paper_code,
        "source": source_type,
        "type": "0",
        "page": "1",
        "num": "20",
    }
    r = _requests.get(url, params=params, headers={"User-Agent": _UA}, timeout=15)
    d = r.json()

    result = d.get("result", {}).get("data", {})
    if not isinstance(result, dict):
        return pd.DataFrame()

    # Legacy: top-level list under fzb/lrb/llb
    items = result.get(source_type, [])
    if isinstance(items, list) and items:
        df = pd.DataFrame(items)
        return _filter_financial_df(df, freq, curr_date, limit=limit)

    # Current: nested report_list
    df = _sina_report_list_to_df(result.get("report_list") or {})
    return _filter_financial_df(df, freq, curr_date, limit=limit)


def _append_debt_ratio_summary(df: pd.DataFrame) -> str:
    """Compute 资产负债率 lines from 资产总计/负债合计 columns when present."""
    if df.empty or "资产总计" not in df.columns or "负债合计" not in df.columns:
        return ""

    lines = ["# Derived 资产负债率 (负债合计 / 资产总计):"]
    for _, row in df.iterrows():
        try:
            assets = float(row["资产总计"])
            liab = float(row["负债合计"])
        except (TypeError, ValueError):
            continue
        if assets <= 0:
            continue
        date_label = row["报告日"]
        if hasattr(date_label, "strftime"):
            date_label = date_label.strftime("%Y-%m-%d")
        lines.append(f"{date_label}: {liab / assets * 100:.2f}%")
    if len(lines) == 1:
        return ""
    return "\n".join(lines) + "\n\n"


def get_balance_sheet(
    ticker: Annotated[str, "A-stock code"],
    freq: Annotated[str, "frequency: 'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str, "current date in YYYY-MM-DD format"] = None,
) -> str:
    """Get balance sheet via Sina direct HTTP API."""
    code = _normalize_ticker(ticker)

    try:
        df = _get_financial_report_sina(code, "资产负债表", freq, curr_date)

        if df.empty:
            return f"No balance sheet data found for A-stock '{code}'"

        csv_string = df.to_csv(index=False)
        ratio_block = _append_debt_ratio_summary(df)

        header = f"# Balance Sheet for {code} (A-stock, {freq})\n"
        header += "# Data source: sina direct HTTP\n"
        header += (
            f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        )

        return header + ratio_block + csv_string

    except Exception as e:
        return f"Error retrieving balance sheet for {code}: {str(e)}"


# ---- 5. get_cashflow ----


def get_cashflow(
    ticker: Annotated[str, "A-stock code"],
    freq: Annotated[str, "frequency: 'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str, "current date in YYYY-MM-DD format"] = None,
) -> str:
    """Get cash flow statement via Sina direct HTTP API."""
    code = _normalize_ticker(ticker)

    try:
        df = _get_financial_report_sina(code, "现金流量表", freq, curr_date)

        if df.empty:
            return f"No cash flow data found for A-stock '{code}'"

        csv_string = df.to_csv(index=False)

        header = f"# Cash Flow for {code} (A-stock, {freq})\n"
        header += "# Data source: sina direct HTTP\n"
        header += (
            f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        )

        return header + csv_string

    except Exception as e:
        return f"Error retrieving cash flow for {code}: {str(e)}"


# ---- 6. get_income_statement ----


def get_income_statement(
    ticker: Annotated[str, "A-stock code"],
    freq: Annotated[str, "frequency: 'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str, "current date in YYYY-MM-DD format"] = None,
) -> str:
    """Get income statement via Sina direct HTTP API."""
    code = _normalize_ticker(ticker)

    try:
        df = _get_financial_report_sina(code, "利润表", freq, curr_date)

        if df.empty:
            return f"No income statement data found for A-stock '{code}'"

        csv_string = df.to_csv(index=False)

        header = f"# Income Statement for {code} (A-stock, {freq})\n"
        header += "# Data source: sina direct HTTP\n"
        header += (
            f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        )

        return header + csv_string

    except Exception as e:
        return f"Error retrieving income statement for {code}: {str(e)}"


# ---- 7. get_news ----


def _fetch_news_eastmoney(code: str, page_size: int = 20) -> list[dict]:
    """Direct East Money search API for individual stock news."""
    url = "https://search-api-web.eastmoney.com/search/jsonp"
    inner_param = {
        "uid": "",
        "keyword": code,
        "type": ["cmsArticleWebOld"],
        "client": "web",
        "clientType": "web",
        "clientVersion": "curr",
        "param": {
            "cmsArticleWebOld": {
                "searchScope": "default",
                "sort": "default",
                "pageIndex": 1,
                "pageSize": page_size,
                "preTag": "",
                "postTag": "",
            }
        },
    }
    params = {
        "cb": "callback",
        "param": _json.dumps(inner_param, ensure_ascii=False),
        "_": "1",
    }
    headers = {
        "Referer": "https://so.eastmoney.com/",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
        ),
    }

    resp = _em_get(url, params=params, headers=headers, timeout=15)
    resp.raise_for_status()
    text = resp.text
    text = text[text.index("(") + 1 : text.rindex(")")]
    data = _json.loads(text)

    articles: list[dict] = []
    for item in data.get("result", {}).get("cmsArticleWebOld", []):
        articles.append({
            "title": item.get("title", ""),
            "content": item.get("content", ""),
            "time": item.get("date", ""),
            "source": item.get("mediaName", "东方财富"),
            "url": item.get("url", ""),
        })
    return articles


def _fetch_news_sina(code: str, page_size: int = 20) -> list[dict]:
    """Sina Finance stock news API (backup source)."""
    prefix = _get_prefix(code)
    url = (
        f"https://vip.stock.finance.sina.com.cn/corp/view/"
        f"vCB_AllNewsStock.php?symbol={prefix}{code}&Page=1"
    )
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
        ),
        "Referer": "https://finance.sina.com.cn/",
    }

    resp = _requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    resp.encoding = "gb2312"
    html = resp.text

    articles: list[dict] = []
    rows = _re.findall(
        r"(\d{4}-\d{2}-\d{2})\s*(?:&nbsp;)*(\d{2}:\d{2})\s*(?:&nbsp;)*"
        r"<a[^>]+href='([^']+)'[^>]*>([^<]+)</a>",
        html,
    )
    for date_str, time_str, link, title in rows[:page_size]:
        articles.append({
            "title": title.strip(),
            "content": "",
            "time": f"{date_str} {time_str}",
            "source": "新浪财经",
            "url": link,
        })
    return articles


def get_news(
    ticker: Annotated[str, "A-stock code"],
    start_date: Annotated[str, "Start date yyyy-mm-dd"],
    end_date: Annotated[str, "End date yyyy-mm-dd"],
) -> str:
    """Get stock-specific news via East Money direct API (Sina as fallback)."""
    code = _normalize_ticker(ticker)

    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    articles: list[dict] = []
    source_label = ""

    try:
        articles = _fetch_news_eastmoney(code)
        source_label = "东方财富"
    except Exception as e:
        logger.warning("East Money news fetch failed for %s: %s", code, e)

    if not articles:
        try:
            articles = _fetch_news_sina(code)
            source_label = "新浪财经"
        except Exception as e:
            logger.warning("Sina news fetch failed for %s: %s", code, e)

    if not articles:
        return f"No news found for A-stock '{code}'"

    news_str = ""
    count = 0
    for art in articles:
        pub_time = art.get("time", "")
        try:
            pub_dt = datetime.strptime(pub_time[:10], "%Y-%m-%d")
            if pub_dt < start_dt or pub_dt > end_dt:
                continue
        except (ValueError, IndexError):
            pass

        title = art["title"]
        content = art.get("content", "")
        source = art.get("source", source_label)
        link = art.get("url", "")

        news_str += f"### {title} (source: {source})\n"
        if content:
            snippet = content[:300] + "..." if len(content) > 300 else content
            news_str += f"{snippet}\n"
        if link and link != "nan":
            news_str += f"Link: {link}\n"
        news_str += "\n"
        count += 1

    if count == 0:
        return (
            f"No news found for A-stock '{code}' "
            f"between {start_date} and {end_date}"
        )

    return (
        f"## {code} (A-stock) News, from {start_date} to {end_date}:\n\n"
        + news_str
    )


# ---- 8. get_global_news ----


def get_global_news(
    curr_date: Annotated[str, "Current date yyyy-mm-dd"],
    look_back_days: Annotated[int, "Days to look back"] = 7,
    limit: Annotated[int, "Max articles"] = 10,
) -> str:
    """Get China/global financial news via direct HTTP (CLS + Eastmoney)."""
    # Cache key: same parameters yield same content within TTL.
    cache_key = f"global_news:{curr_date}:{look_back_days}:{limit}"
    cached = _ttl_cache(cache_key)
    if cached is not None:
        return cached
    start_dt = datetime.strptime(curr_date, "%Y-%m-%d") - relativedelta(
        days=look_back_days
    )
    start_date = start_dt.strftime("%Y-%m-%d")

    all_news: list[dict] = []

    # Source 1: CLS wire (财联社快讯) — direct HTTP
    try:
        cls_url = "https://www.cls.cn/nodeapi/telegraphList"
        cls_params = {"rn": str(limit), "page": "1"}
        cls_headers = {"User-Agent": _UA, "Referer": "https://www.cls.cn/"}
        r_cls = _requests.get(cls_url, params=cls_params, headers=cls_headers, timeout=10)
        d_cls = r_cls.json()
        for item in d_cls.get("data", {}).get("roll_data", []):
            title = item.get("title", "") or item.get("brief", "")
            content = item.get("content", "") or item.get("brief", "")
            ctime = item.get("ctime", "")
            # ctime is unix timestamp
            pub_time = ""
            if ctime:
                try:
                    pub_time = datetime.fromtimestamp(int(ctime)).strftime("%Y-%m-%d %H:%M")
                except (ValueError, TypeError, OSError):
                    pub_time = str(ctime)
            all_news.append({
                "title": title,
                "content": content,
                "time": pub_time,
                "source": "CLS Wire",
            })
    except Exception as e:
        logger.warning("CLS news fetch failed: %s", e)

    # Source 2: Eastmoney global (东财7x24资讯) — direct HTTP
    try:
        em_url = "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
        em_params = {
            "client": "web",
            "biz": "web_724",
            "fastColumn": "102",
            "sortEnd": "",
            "pageSize": str(limit),
            "req_trace": str(uuid.uuid4()),
        }
        em_headers = {"User-Agent": _UA, "Referer": "https://kuaixun.eastmoney.com/"}
        r_em = _em_get(em_url, params=em_params, headers=em_headers, timeout=10)
        d_em = r_em.json()
        for item in d_em.get("data", {}).get("fastNewsList", []):
            title = item.get("title", "")
            summary = item.get("summary", "")[:200]
            pub_time = item.get("showTime", "")
            all_news.append({
                "title": title,
                "content": summary,
                "time": pub_time,
                "source": "Eastmoney Global",
            })
    except Exception as e:
        logger.warning("Eastmoney global news fetch failed: %s", e)

    if not all_news:
        return f"No global news found for {curr_date}"

    # Deduplicate by title
    seen: set[str] = set()
    unique: list[dict] = []
    for n in all_news:
        if n["title"] not in seen:
            seen.add(n["title"])
            unique.append(n)

    news_str = ""
    for n in unique[:limit]:
        news_str += f"### {n['title']} (source: {n['source']})\n"
        if n.get("content"):
            snippet = (
                n["content"][:300] + "..."
                if len(n["content"]) > 300
                else n["content"]
            )
            news_str += f"{snippet}\n"
        news_str += "\n"

    result = (
        f"## China & Global Market News, from {start_date} to {curr_date}:\n\n"
        + news_str
    )
    _ttl_store(cache_key, result)
    return result


# ---- 9. get_insider_transactions ----


def _coerce_f10_text(payload) -> str:
    """Normalize mootdx F10 payloads (str or dict-of-sections) to plain text."""
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        parts = []
        for key, value in payload.items():
            if value is None:
                continue
            chunk = value if isinstance(value, str) else str(value)
            if not chunk.strip():
                continue
            parts.append(chunk if key in chunk[:40] else f"【{key}】\n{chunk}")
        return "\n\n".join(parts)
    return str(payload)


def _em_security_code(code: str) -> str:
    """6-digit code → Eastmoney F10 code, e.g. SH688401 / SZ000001."""
    prefix = _get_prefix(code).upper()
    return f"{prefix}{code}"


def _em_shareholder_research(code: str) -> str:
    """Shareholder / institution holdings via Eastmoney PC_HSF10 PageAjax."""
    url = (
        "https://emweb.securities.eastmoney.com"
        "/PC_HSF10/ShareholderResearch/PageAjax"
    )
    r = _em_get(
        url,
        params={"code": _em_security_code(code)},
        timeout=15,
    )
    d = r.json()
    if not isinstance(d, dict) or not d:
        return ""

    lines = [
        f"# Shareholder Research for {code} (A-stock)",
        "# Note: A-stock equivalent of insider transactions",
        "# Data source: 东财 F10 ShareholderResearch",
        f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]

    controllers = d.get("sjkzr") or []
    if controllers:
        lines.append("## 实际控制人")
        for row in controllers:
            lines.append(
                f"  {row.get('HOLDER_NAME', '')}: "
                f"持股比例 {row.get('HOLD_RATIO', '')}%"
            )

    tops = d.get("sdgd") or []
    if tops:
        end_date = str(tops[0].get("END_DATE", ""))[:10]
        lines.append(f"\n## 十大股东 ({end_date})")
        lines.append("排名 | 股东 | 持股数 | 占比(%)")
        for row in tops[:10]:
            lines.append(
                f"  {row.get('HOLDER_RANK', '')} "
                f"| {row.get('HOLDER_NAME', '')} "
                f"| {row.get('HOLD_NUM', '')} "
                f"| {row.get('HOLD_NUM_RATIO', '')}"
            )

    float_tops = d.get("sdltgd") or []
    if float_tops:
        end_date = str(float_tops[0].get("END_DATE", ""))[:10]
        lines.append(f"\n## 十大流通股东 ({end_date})")
        lines.append("排名 | 股东 | 类型 | 持股数 | 占比(%)")
        for row in float_tops[:10]:
            lines.append(
                f"  {row.get('HOLDER_RANK', '')} "
                f"| {row.get('HOLDER_NAME', '')} "
                f"| {row.get('HOLDER_TYPE', '')} "
                f"| {row.get('HOLD_NUM', '')} "
                f"| {row.get('HOLD_NUM_RATIO', '')}"
            )

    orgs = d.get("jgcc") or []
    if orgs:
        lines.append("\n## 机构持仓汇总")
        lines.append("报告期 | 机构类型 | 家数 | 占总股本比(%)")
        for row in orgs[:12]:
            lines.append(
                f"  {str(row.get('REPORT_DATE', ''))[:10]} "
                f"| {row.get('ORG_TYPE', '')} "
                f"| {row.get('TOTAL_ORG_NUM', '')} "
                f"| {row.get('TOTAL_SHARES_RATIO', '')}"
            )

    holders = d.get("gdrs") or []
    if holders:
        lines.append("\n## 股东户数")
        lines.append("期末 | 股东户数 | 户均持股 | 较上期")
        for row in holders[:8]:
            lines.append(
                f"  {str(row.get('END_DATE', ''))[:10]} "
                f"| {row.get('HOLDER_TOTAL_NUM', '')} "
                f"| {row.get('AVG_FREE_SHARES', '')} "
                f"| {row.get('TOTAL_NUM_RATIO', '')}"
            )

    # Need at least one meaningful section besides headers
    if len(lines) <= 5:
        return ""
    return "\n".join(lines)


def get_insider_transactions(
    ticker: Annotated[str, "A-stock code"],
) -> str:
    """Get shareholder/insider activity (东财 F10 primary, mootdx F10 fallback).

    Note: A-stock insider transaction data differs from US markets.
    mootdx ``F10(name=股东研究)`` currently often returns the 最新提示 tab
    instead of shareholder sections, so Eastmoney ShareholderResearch is
    preferred.
    """
    code = _normalize_ticker(ticker)
    last_err: Exception | None = None

    try:
        em_text = _em_shareholder_research(code)
        if em_text:
            return em_text
    except Exception as em_exc:
        last_err = em_exc
        logger.warning(
            "Eastmoney shareholder research failed for %s: %s", code, em_exc
        )

    try:
        with _mootdx_client_session() as client:
            payload = client.F10(symbol=code, name="股东研究")

        text = _coerce_f10_text(payload)
        # Require explicit shareholder-structure markers; 最新提示 often
        # mentions 「股东人数」 in a headline metric and must not qualify.
        has_shareholder_section = any(
            key in text
            for key in ("【4.股东变化】", "十大股东", "十大流通股东")
        )
        if text.strip() and has_shareholder_section:
            header = f"# Shareholder Research for {code} (A-stock)\n"
            header += "# Note: A-stock equivalent of insider transactions\n"
            header += "# Data source: mootdx F10\n"
            header += (
                f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            )

            import re

            sec4_hits = list(re.finditer(r"\r?\n【4\.股东变化】\r?\n", text))
            if sec4_hits:
                sec4_pos = sec4_hits[-1].start()
                before_sec4 = text[:sec4_pos]
                sec4_text = text[sec4_pos:]
                cut_at = 2000
                if len(sec4_text) > cut_at:
                    sec4_text = (
                        sec4_text[:cut_at]
                        + "\n\n(... older shareholder history omitted, "
                        f"{len(text) - sec4_pos - cut_at} chars truncated ...)"
                    )
                text = before_sec4 + sec4_text

            return header + text
    except Exception as e:
        last_err = e

    if last_err is not None:
        return (
            f"Error retrieving insider/shareholder data for {code}: {last_err}"
        )
    return f"No insider/shareholder data found for A-stock '{code}'"


# ---- 10. get_profit_forecast ----


def get_profit_forecast(
    ticker: Annotated[str, "A-stock code"],
    curr_date: Annotated[str, "current date (unused, for interface compat)"] = None,
) -> str:
    """Get consensus EPS forecasts with forward valuation.

    Primary: Eastmoney ``RPT_WEB_RESPREDICT``. Fallback: 同花顺 F10.
    Empty coverage is a valid EmptyOK result (not a hard tool failure).
    """
    code = _normalize_ticker(ticker)

    try:
        em_records, _org = _em_eps_forecast(code)
        if em_records:
            return _format_eps_forecast_lines(
                code,
                em_records,
                "东财 RPT_WEB_RESPREDICT (Eastmoney datacenter)",
            )

        ths_records = _ths_eps_records(code)
        if ths_records:
            return _format_eps_forecast_lines(
                code,
                ths_records,
                "同花顺 analyst consensus (fallback)",
            )

        return (
            f"No analyst coverage found for A-stock '{code}' "
            "(东财+同花顺均无一致预期；请写「无公开一致预期覆盖」，勿标 [数据缺失])"
        )

    except Exception as e:
        return f"Error retrieving profit forecast for {code}: {str(e)}"


# ---- 11. get_hot_stocks ----


def get_hot_stocks(
    curr_date: Annotated[str, "Date YYYY-MM-DD, empty string for today"] = "",
) -> str:
    """Get strong stocks with topic attribution from 同花顺 editorial team.

    Returns stocks that hit limit-up with human-curated reason tags
    explaining WHY they surged (e.g. '算力租赁+AI政务').
    """
    import requests

    if not curr_date or curr_date.strip() == "":
        curr_date = datetime.now().strftime("%Y-%m-%d")

    cache_key = f"hot_stocks:{curr_date}"
    cached = _ttl_cache(cache_key)
    if cached is not None:
        return cached

    try:
        url = (
            f"http://zx.10jqka.com.cn/event/api/getharden/"
            f"date/{curr_date}/orderby/date/orderway/desc/charset/GBK/"
        )
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "Chrome/117.0.0.0 Safari/537.36"
            )
        }
        r = requests.get(url, headers=headers, timeout=10)
        data = r.json()

        if data.get("errocode", 0) != 0:
            return f"同花顺 API error: {data.get('errormsg', 'unknown')}"

        rows = data.get("data") or []
        if not rows:
            return (
                f"No hot stocks data for {curr_date} "
                f"(may be non-trading day or data not yet available)"
            )

        lines = [
            f"# Hot Stocks with Topic Attribution ({curr_date})",
            f"# Source: 同花顺 editorial (human-curated reason tags)",
            f"# Total: {len(rows)} stocks",
            "",
        ]

        from collections import Counter

        all_tags: list[str] = []

        for row in rows:
            code = row.get("code", "")
            name = row.get("name", "")
            reason = row.get("reason", "")
            zhangfu = row.get("zhangfu", "")
            huanshou = row.get("huanshou", "")
            chengjiaoe = row.get("chengjiaoe", "")
            dde = row.get("ddejingliang", "")

            lines.append(
                f"{code} {name}: +{zhangfu}% "
                f"换手{huanshou}% 成交额{chengjiaoe} "
                f"大单净量{dde} | {reason}"
            )

            if reason:
                tags = [t.strip() for t in str(reason).split("+") if t.strip()]
                all_tags.extend(tags)

        if all_tags:
            cnt = Counter(all_tags)
            lines.append(f"\n## Theme Frequency (top 15)")
            for tag, n in cnt.most_common(15):
                lines.append(f"  {tag}: {n} stocks")

        result = "\n".join(lines)
        _ttl_store(cache_key, result)
        return result

    except Exception as e:
        return f"Error fetching hot stocks for {curr_date}: {str(e)}"


# ---- 12. get_northbound_flow ----


def _northbound_cache_path() -> str:
    """Path to local CSV cache for northbound daily close snapshots."""
    from .config import get_config

    config = get_config()
    cache_dir = config.get(
        "data_cache_dir", os.path.expanduser("~/.tradingagents/cache")
    )
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, "northbound_daily.csv")


def _save_northbound_snapshot(date_str: str, hgt: float, sgt: float) -> None:
    """Append today's northbound close to local CSV cache (dedup by date)."""
    import csv

    path = _northbound_cache_path()
    existing: dict[str, tuple[str, str]] = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            next(reader, None)
            for row in reader:
                if len(row) >= 3:
                    existing[row[0]] = (row[1], row[2])
    existing[date_str] = (f"{hgt:.2f}", f"{sgt:.2f}")
    sorted_dates = sorted(existing.keys())
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "hgt", "sgt"])
        for d in sorted_dates:
            writer.writerow([d, existing[d][0], existing[d][1]])


def _load_northbound_history(n: int = 20) -> list[tuple[str, float, float]]:
    """Load last N days of northbound close data from local cache."""
    import csv

    path = _northbound_cache_path()
    if not os.path.exists(path):
        return []
    rows: list[tuple[str, float, float]] = []
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
            if len(row) >= 3:
                try:
                    rows.append((row[0], float(row[1]), float(row[2])))
                except ValueError:
                    continue
    return rows[-n:]


def get_northbound_flow(
    curr_date: Annotated[str, "Date YYYY-MM-DD"],
    include_history: Annotated[
        bool, "Include historical daily data (last 20 trading days)"
    ] = False,
) -> str:
    """Get northbound capital flow (沪深股通) from 同花顺 hsgtApi.

    Realtime: minute-level cumulative net buying for HGT(沪股通) + SGT(深股通).
    History: self-cached daily close snapshots (upstream APIs stopped updating
    northbound history since 2024-08).
    """
    import requests

    hsgt_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "Chrome/117.0.0.0 Safari/537.36"
        ),
        "Host": "data.hexin.cn",
        "Referer": "https://data.hexin.cn/",
    }

    lines = [
        f"# Northbound Capital Flow ({curr_date})",
        "# Source: 同花顺 hsgtApi (沪深股通) + local cache",
        "",
    ]

    hgt_close = 0.0
    sgt_close = 0.0
    got_realtime = False

    try:
        url_rt = "https://data.hexin.cn/market/hsgtApi/method/dayChart/"
        r = requests.get(url_rt, headers=hsgt_headers, timeout=10)
        d = r.json()

        times = d.get("time", [])
        hgt = d.get("hgt", [])
        sgt = d.get("sgt", [])

        if times:
            lines.append("## Realtime (cumulative net buying, 亿元)")
            n = len(times)
            start_idx = max(0, n - 10)
            for i in range(start_idx, n):
                t = times[i]
                h = hgt[i] if i < len(hgt) else "N/A"
                s = sgt[i] if i < len(sgt) else "N/A"
                lines.append(f"  {t}: HGT={h} SGT={s}")

            hgt_close = float(hgt[-1]) if hgt else 0
            sgt_close = float(sgt[-1]) if sgt else 0
            total = hgt_close + sgt_close
            lines.append(
                f"\nClose: HGT(沪股通)={hgt_close:.2f}亿 "
                f"SGT(深股通)={sgt_close:.2f}亿 "
                f"Total={total:.2f}亿"
            )
            if total > 0:
                lines.append("Signal: Net northbound INFLOW (bullish)")
            elif total < 0:
                lines.append("Signal: Net northbound OUTFLOW (bearish)")
            got_realtime = True
        else:
            lines.append("No realtime data (non-trading hours or holiday)")

        if got_realtime:
            today_str = datetime.now().strftime("%Y-%m-%d")
            _save_northbound_snapshot(today_str, hgt_close, sgt_close)

        if include_history:
            history = _load_northbound_history(20)
            if history:
                lines.append("\n## Historical Daily Close (local cache, 亿元)")
                lines.append("Date       | HGT(沪股通) | SGT(深股通) | Total")
                for date, h, s in history:
                    lines.append(f"  {date}: HGT={h:.2f} SGT={s:.2f} Total={h + s:.2f}")
                avg_total = sum(h + s for _, h, s in history) / len(history)
                lines.append(
                    f"\n{len(history)}-day avg net flow: {avg_total:.2f}亿"
                )
                if got_realtime:
                    today_total = hgt_close + sgt_close
                    diff = today_total - avg_total
                    lines.append(
                        f"Today vs avg: {'+' if diff >= 0 else ''}{diff:.2f}亿 "
                        f"({'above' if diff >= 0 else 'below'} average)"
                    )
            else:
                lines.append(
                    "\n## Historical Daily: No cached data yet. "
                    "History accumulates automatically with each call."
                )

        return "\n".join(lines)

    except Exception as e:
        return f"Error fetching northbound flow: {str(e)}"


# ---------------------------------------------------------------------------
# Baidu PAE (百度股市通) helpers
# ---------------------------------------------------------------------------

_BAIDU_PAE_HEADERS = {
    "Host": "finance.pae.baidu.com",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) "
        "Gecko/20100101 Firefox/110.0"
    ),
    "Accept": "application/vnd.finance-web.v1+json",
    "Origin": "https://gushitong.baidu.com",
    "Referer": "https://gushitong.baidu.com/",
}


# ---- 13. get_concept_blocks ----


def _em_concept_blocks(code: str) -> str:
    """Concept / industry boards via Eastmoney CoreConception PageAjax."""
    url = (
        "https://emweb.securities.eastmoney.com"
        "/PC_HSF10/CoreConception/PageAjax"
    )
    r = _em_get(
        url,
        params={"code": _em_security_code(code)},
        timeout=15,
    )
    d = r.json()
    if not isinstance(d, dict):
        return ""

    boards = d.get("ssbk") or []
    themes = d.get("hxtc") or []
    if not boards and not themes:
        return ""

    lines = [
        f"# Concept & Sector Blocks for {code} (A-stock)",
        "# Source: 东财 F10 CoreConception",
        f"# Retrieved: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]

    concept_names: list[str] = []
    if boards:
        lines.append("## 所属板块")
        for row in boards:
            name = (row.get("BOARD_NAME") or "").strip()
            if not name:
                continue
            rank = row.get("BOARD_RANK", "")
            suffix = f" (rank {rank})" if rank not in (None, "") else ""
            lines.append(f"  {name}{suffix}")
            concept_names.append(name)

    if themes:
        lines.append("\n## 核心题材 / 主营要点")
        for row in themes[:12]:
            keyword = (row.get("KEYWORD") or "").strip()
            klass = (row.get("KEY_CLASSIF") or "").strip()
            content = (row.get("MAINPOINT_CONTENT") or "").strip()
            label = keyword or klass or "要点"
            detail = content[:120] + ("…" if len(content) > 120 else "")
            lines.append(f"  {label}" + (f"：{detail}" if detail else ""))
            if keyword and keyword not in concept_names:
                concept_names.append(keyword)

    if concept_names:
        lines.append(f"\nConcept tags: {' / '.join(concept_names[:20])}")
    return "\n".join(lines)


def get_concept_blocks(
    ticker: Annotated[str, "A-stock code (e.g. 688017)"],
) -> str:
    """Get concept/sector/region blocks for a stock.

    Primary: 百度股市通 PAE. Fallback: 东财 F10 CoreConception when Baidu
    returns non-zero ResultCode (403 is common) or empty payload.
    """
    import requests

    code = _normalize_ticker(ticker)

    try:
        url = (
            "https://finance.pae.baidu.com/api/getrelatedblock"
            f'?stock=[{{"code":"{code}","market":"ab","type":"stock"}}]'
            "&finClientType=pc"
        )
        r = requests.get(url, headers=_BAIDU_PAE_HEADERS, timeout=10)
        d = r.json()

        if str(d.get("ResultCode", -1)) == "0":
            result = d.get("Result", {})
            categories = result.get(code, [])
            if categories:
                lines = [
                    f"# Concept & Sector Blocks for {code} (A-stock)",
                    "# Source: 百度股市通 (Baidu PAE)",
                    f"# Retrieved: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                    "",
                ]
                concept_names: list[str] = []
                for cat in categories:
                    cat_name = cat.get("name", "")
                    items = cat.get("list", [])
                    if not items:
                        continue
                    lines.append(f"## {cat_name}")
                    for item in items:
                        name = item.get("name", "")
                        ratio = item.get("ratio", "")
                        desc = item.get("describe", "")
                        suffix = f" ({desc})" if desc else ""
                        lines.append(f"  {name}{suffix}: {ratio}")
                        if cat_name == "概念":
                            concept_names.append(name)
                if concept_names:
                    lines.append(f"\nConcept tags: {' / '.join(concept_names)}")
                return "\n".join(lines)
            baidu_err = "empty Result"
        else:
            baidu_err = (
                f"ResultCode={d.get('ResultCode')} {d.get('ResultMsg', '')}".strip()
            )
    except Exception as e:
        baidu_err = f"{type(e).__name__}: {e}"

    try:
        em_text = _em_concept_blocks(code)
        if em_text:
            return em_text
    except Exception as em_exc:
        logger.warning(
            "Eastmoney concept fallback failed for %s: %s", code, em_exc
        )
        return (
            f"Error fetching concept blocks for {code}: "
            f"Baidu ({baidu_err}); EM ({em_exc})"
        )

    return (
        f"No concept/block data for {code} "
        f"(Baidu: {baidu_err}; Eastmoney fallback empty)"
    )


# ---- 14. get_fund_flow ----


def _sina_fund_flow_history(code: str, days: int = 20) -> list[dict]:
    """Daily fund-flow history from Sina MoneyFlow (heterogeneous EM backup).

    Each item: ``{"date", "main_net", "net_amount"}`` in yuan.
    ``main_net`` maps to Sina ``r0_net`` (largest-order net); ``net_amount`` is
    overall net inflow. Caliber differs from Eastmoney 超大单 — callers must
    label the source.
    """
    url = (
        "https://vip.stock.finance.sina.com.cn/quotes_service/api/"
        "json_v2.php/MoneyFlow.ssl_qsfx_zjlrqs"
    )
    params = {
        "page": 1,
        "num": max(1, int(days)),
        "sort": "opendate",
        "asc": 0,
        "daima": _sina_stock_code(code),
    }
    headers = {
        "User-Agent": _UA,
        "Referer": "https://vip.stock.finance.sina.com.cn/",
    }
    r = _requests.get(url, params=params, headers=headers, timeout=12)
    r.raise_for_status()
    payload = r.json()
    if not isinstance(payload, list):
        return []
    rows: list[dict] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        date = str(item.get("opendate") or "").strip()
        if not date:
            continue
        try:
            main_net = float(item.get("r0_net") or 0)
        except (TypeError, ValueError):
            main_net = 0.0
        try:
            net_amount = float(item.get("netamount") or 0)
        except (TypeError, ValueError):
            net_amount = 0.0
        rows.append(
            {"date": date, "main_net": main_net, "net_amount": net_amount}
        )
    return rows


def get_realtime_main_net_inflow(
    ticker: Annotated[str, "A-stock code"],
) -> float | None:
    """Return the latest 主力净流入 in yuan, or None if unavailable.

    Prefer东财 push2 minute series; on disconnect/empty, fall back to the
    newest Sina MoneyFlow daily ``r0_net`` (heterogeneous caliber).
    """
    code = _normalize_ticker(ticker)
    secid = f"1.{code}" if code.startswith("6") else f"0.{code}"
    try:
        url_rt = "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get"
        params_rt = {
            "secid": secid,
            "klt": 1,
            "fields1": "f1,f2,f3,f7",
            "fields2": "f51,f52,f53,f54,f55,f56,f57",
        }
        r = _em_get(url_rt, params=params_rt, timeout=10)
        klines = r.json().get("data", {}).get("klines", []) or []
        if klines:
            parts = str(klines[-1]).split(",")
            if len(parts) >= 2:
                return float(parts[1])
    except Exception as em_exc:
        logger.warning(
            "realtime fund flow failed for %s: %s", code, em_exc
        )

    try:
        sina_rows = _sina_fund_flow_history(code, days=5)
    except Exception as sina_exc:
        logger.warning(
            "sina fund flow fallback failed for %s: %s", code, sina_exc
        )
        return None
    if not sina_rows:
        return None
    return float(sina_rows[0]["main_net"])


def _append_sina_fund_flow_close(
    lines: list[str],
    row: dict,
    *,
    as_primary: bool,
) -> None:
    """Append Close/Signal lines from one Sina daily row."""
    main_net = float(row["main_net"])
    date = str(row.get("date") or "")
    if as_primary:
        lines.append(
            "## Daily Fund Flow "
            f"(新浪 MoneyFlow | 东财分时不可用时的日度兜底 | {date})"
        )
        lines.append(
            f"  最大单净流入 r0_net≈{main_net / 1e4:.0f}万 "
            f"| 整体净流入≈{float(row['net_amount']) / 1e4:.0f}万"
        )
        lines.append(
            "(sina caliber ≠ 东财超大单；可用作个股主力净流入，"
            "勿写报告级数据缺失标记)"
        )
    lines.append(f"\nClose: 主力净流入≈{main_net / 1e4:.0f}万元 ({date} 新浪日度)")
    if main_net > 0:
        lines.append("Signal: Net main force INFLOW (bullish)")
    elif main_net < 0:
        lines.append("Signal: Net main force OUTFLOW (bearish)")


def get_fund_flow(
    ticker: Annotated[str, "A-stock code"],
    curr_date: Annotated[str, "Date YYYY-MM-DD"],
    include_history: Annotated[
        bool, "Include historical daily fund flow (last 20 days)"
    ] = True,
) -> str:
    """Get individual stock fund flow from 东财 push2 (+ 新浪兜底).

    Realtime: minute-level main/large/medium/small/super order net inflow.
    If push2 disconnects/errors, fall back to Sina MoneyFlow daily series for
    a usable Close 主力净流入 (labeled; do not treat as report-level gap).
    History: Eastmoney push2his daykline; on SSL/empty, fall back to Sina.

    V0.2.7: replaced 百度 PAE (fundflow/fundsortlist, offline since 2026-05)
    with 东财 push2 fund flow API.
    """
    code = _normalize_ticker(ticker)
    secid = f"1.{code}" if code.startswith("6") else f"0.{code}"
    sources: list[str] = []
    lines = [
        f"# Fund Flow for {code} (A-stock)",
        f"# Source: 东财 push2 (Eastmoney)",
        f"# Retrieved: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]

    sina_cache: list[dict] | None = None
    sina_resolved = False  # True only after a non-exception Sina response

    def _sina_rows(days: int = 20) -> list[dict]:
        nonlocal sina_cache, sina_resolved
        if sina_resolved:
            return sina_cache or []
        try:
            sina_cache = _sina_fund_flow_history(code, days=days)
            sina_resolved = True
            return sina_cache
        except Exception as sina_exc:
            logger.warning(
                "sina fund flow history failed for %s: %s",
                code,
                sina_exc,
            )
            # Do not cache the exception — history block may retry.
            return []

    em_rt_present = False
    rt_err: str | None = None
    try:
        url_rt = "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get"
        params_rt = {
            "secid": secid,
            "klt": 1,
            "fields1": "f1,f2,f3,f7",
            "fields2": "f51,f52,f53,f54,f55,f56,f57",
        }
        r = _em_get(url_rt, params=params_rt, timeout=10)
        d = r.json()
        klines = d.get("data", {}).get("klines", []) or []

        if klines:
            em_rt_present = True
            sources.append("东财 push2 realtime")
            lines.append(
                "## Realtime Minute Flow "
                "(主力/小单/中单/大单/超大单 净流入, 元)"
            )
            for line in klines[-10:]:
                parts = line.split(",")
                if len(parts) >= 6:
                    lines.append(
                        f"  {parts[0]}: "
                        f"主力={float(parts[1])/1e4:.0f}万 "
                        f"小单={float(parts[2])/1e4:.0f}万 "
                        f"中单={float(parts[3])/1e4:.0f}万 "
                        f"大单={float(parts[4])/1e4:.0f}万 "
                        f"超大单={float(parts[5])/1e4:.0f}万"
                    )

            last_parts = klines[-1].split(",")
            if len(last_parts) >= 2:
                main_net = float(last_parts[1])
                lines.append(
                    f"\nClose: 主力净流入={main_net/1e4:.0f}万元"
                )
                if main_net > 0:
                    lines.append(
                        "Signal: Net main force INFLOW (bullish)"
                    )
                elif main_net < 0:
                    lines.append(
                        "Signal: Net main force OUTFLOW (bearish)"
                    )
    except Exception as em_exc:
        rt_err = f"{type(em_exc).__name__}: {em_exc}"
        logger.warning("fund flow realtime failed for %s: %s", code, em_exc)

    if not em_rt_present:
        sina_rows = _sina_rows()
        if sina_rows:
            sources.append("新浪 MoneyFlow (分时兜底)")
            if rt_err:
                lines.append(
                    f"(东财分时资金流不可用: {rt_err} — "
                    "已用新浪日度主力净流入兜底，勿写报告级数据缺失标记)"
                )
            else:
                lines.append(
                    "(东财分时空数据/非交易时段 — "
                    "已用新浪日度主力净流入兜底，勿写报告级数据缺失标记)"
                )
            _append_sina_fund_flow_close(
                lines, sina_rows[0], as_primary=True
            )

    # Historical daily fund flow (push2his) — isolated so SSL flakes
    # do not discard an otherwise successful realtime / Sina section.
    if include_history:
        hist_done = False
        try:
            url_hist = (
                "https://push2his.eastmoney.com"
                "/api/qt/stock/fflow/daykline/get"
            )
            params_hist = {
                "secid": secid,
                "lmt": 20,
                "klt": 101,
                "fields1": "f1,f2,f3,f7",
                "fields2": "f51,f52,f53,f54,f55,f56,f57",
            }
            rh = _em_get(url_hist, params=params_hist, timeout=10)
            dh = rh.json()
            hist_klines = dh.get("data", {}).get("klines", [])

            if hist_klines:
                sources.append("东财 push2his history")
                lines.append(
                    f"\n## Historical Daily Fund Flow "
                    f"(last {len(hist_klines)} trading days | 东财 push2his)"
                )
                lines.append(
                    "Date | 主力净流入(万) | 大单(万) "
                    "| 中单(万) | 小单(万) | 超大单(万)"
                )
                for line in hist_klines:
                    parts = line.split(",")
                    if len(parts) >= 6:
                        lines.append(
                            f"  {parts[0]} "
                            f"| main={float(parts[1])/1e4:.0f} "
                            f"| large={float(parts[4])/1e4:.0f} "
                            f"| mid={float(parts[3])/1e4:.0f} "
                            f"| small={float(parts[2])/1e4:.0f} "
                            f"| super={float(parts[5])/1e4:.0f}"
                        )
                hist_done = True
        except Exception as hist_exc:
            logger.warning(
                "fund flow history failed for %s: %s", code, hist_exc
            )

        if not hist_done:
            sina_rows = _sina_rows()
            if sina_rows:
                if not any("新浪 MoneyFlow" in s for s in sources):
                    sources.append("新浪 MoneyFlow history")
                lines.append(
                    f"\n## Historical Daily Fund Flow "
                    f"(last {len(sina_rows)} trading days | "
                    f"新浪 MoneyFlow fallback)"
                )
                lines.append(
                    "Date | 最大单净流入r0_net(万) | 整体净流入(万) "
                    "| 口径备注"
                )
                for row in sina_rows:
                    lines.append(
                        f"  {row['date']} "
                        f"| main≈{row['main_net']/1e4:.0f} "
                        f"| net={row['net_amount']/1e4:.0f} "
                        f"| sina caliber ≠ 东财超大单"
                    )
                hist_done = True

        if not hist_done:
            if em_rt_present:
                hist_note = (
                    "盘中分时资金流仍可用，勿因此标注报告级数据缺失"
                )
            else:
                hist_note = (
                    "分时与日度均不可用时才可标数据缺口；"
                    "单源失败勿写报告级数据缺失标记"
                )
            lines.append(
                "\n## Historical Daily Fund Flow\n"
                "(历史日度暂不可用: 东财 push2his + 新浪 fallback 均失败 — "
                f"{hist_note})"
            )

    # Sina may succeed only on a history retry; attach Close then so we
    # never claim 新浪兜底失败 while history already has usable rows.
    has_close = any("Close: 主力净流入" in ln for ln in lines)
    if not em_rt_present and not has_close:
        sina_rows = _sina_rows()
        if sina_rows:
            if not any("新浪 MoneyFlow (分时兜底)" in s for s in sources):
                sources.append("新浪 MoneyFlow (分时兜底)")
            if rt_err:
                lines.append(
                    f"(东财分时资金流不可用: {rt_err} — "
                    "已用新浪日度主力净流入兜底，勿写报告级数据缺失标记)"
                )
            else:
                lines.append(
                    "(东财分时空数据/非交易时段 — "
                    "已用新浪日度主力净流入兜底，勿写报告级数据缺失标记)"
                )
            _append_sina_fund_flow_close(
                lines, sina_rows[0], as_primary=True
            )
        elif rt_err:
            lines.append(
                f"No realtime fund flow (东财不可用: {rt_err}；新浪兜底也失败)"
            )
        else:
            lines.append(
                "No realtime fund flow (non-trading hours or holiday)"
            )

    if sources:
        lines[1] = f"# Source: {' + '.join(sources)}"

    body = "\n".join(lines)
    # Soft partial (notes only) still beats a hard Error — probes treat
    # ``Error`` as failure while markdown soft-gaps remain regenerable.
    if (
        "主力净流入" not in body
        and "Realtime Minute Flow" not in body
        and "Daily Fund Flow" not in body
        and "Historical Daily Fund Flow" not in body
    ):
        detail = rt_err or "no fund-flow source available"
        return f"Error fetching fund flow for {code}: {detail}"
    return body


# ---------------------------------------------------------------------------
# 15. Dragon Tiger Board (龙虎榜)
# ---------------------------------------------------------------------------

def get_dragon_tiger_board(
    ticker: str,
    trade_date: str,
    look_back_days: int = 30,
) -> str:
    """Get dragon-tiger board (龙虎榜) appearances and seat details.

    Args:
        ticker: 6-digit A-share code, e.g. '000858'
        trade_date: YYYY-MM-DD
        look_back_days: how many days back to search (default 30)

    Returns:
        Formatted text with LHB appearances, top buyer/seller seats,
        and institutional activity.
    """
    code = safe_ticker_component(ticker)
    end_dt = datetime.strptime(trade_date, "%Y-%m-%d")
    start_dt = end_dt - pd.Timedelta(days=look_back_days)
    start_date_str = start_dt.strftime("%Y-%m-%d")
    lines = [f"# 龙虎榜数据 | {code} | {trade_date} (近{look_back_days}日)"]

    # 1. 上榜记录 — eastmoney datacenter direct HTTP
    try:
        data = _eastmoney_datacenter(
            "RPT_DAILYBILLBOARD_DETAILSNEW",
            filter_str=(
                f"(TRADE_DATE>='{start_date_str}')"
                f"(TRADE_DATE<='{trade_date}')"
                f"(SECURITY_CODE=\"{code}\")"
            ),
            page_size=50,
            sort_columns="TRADE_DATE",
            sort_types="-1",
        )
        if not data:
            lines.append(f"\n近{look_back_days}日未上龙虎榜。")
        else:
            lines.append(f"\n## 上榜记录 ({len(data)} 次)")
            lines.append("日期 | 原因 | 净买入(万) | 换手率")
            for row in data:
                net_buy = round((row.get("BILLBOARD_NET_AMT") or 0) / 10000, 1)
                turnover = round(float(row.get("TURNOVERRATE") or 0), 2)
                lines.append(
                    f"  {str(row.get('TRADE_DATE', ''))[:10]} "
                    f"| {row.get('EXPLANATION', '')} "
                    f"| {net_buy:.0f} "
                    f"| {turnover:.2f}%"
                )
    except Exception as e:
        lines.append(f"龙虎榜列表查询失败: {e}")

    # 2. 最近上榜的买卖席位 — eastmoney datacenter direct HTTP
    try:
        if data:
            latest_date = str(data[0].get("TRADE_DATE", ""))[:10]
            lines.append(f"\n## 最近上榜席位明细 ({latest_date})")

            # 买入席位
            buy_data = _eastmoney_datacenter(
                "RPT_BILLBOARD_DAILYDETAILSBUY",
                filter_str=f"(TRADE_DATE='{latest_date}')(SECURITY_CODE=\"{code}\")",
                page_size=10,
                sort_columns="BUY",
                sort_types="-1",
            )
            if buy_data:
                lines.append("\n### 买入席位 TOP5")
                lines.append("营业部 | 买入(万) | 卖出(万) | 净额(万)")
                for row in buy_data[:5]:
                    buy_amt = round((row.get("BUY") or 0) / 10000, 1)
                    sell_amt = round((row.get("SELL") or 0) / 10000, 1)
                    net = round((row.get("NET") or 0) / 10000, 1)
                    lines.append(
                        f"  {row.get('OPERATEDEPT_NAME', '')} "
                        f"| {buy_amt:.0f} | {sell_amt:.0f} | {net:.0f}"
                    )

            # 卖出席位
            sell_data = _eastmoney_datacenter(
                "RPT_BILLBOARD_DAILYDETAILSSELL",
                filter_str=f"(TRADE_DATE='{latest_date}')(SECURITY_CODE=\"{code}\")",
                page_size=10,
                sort_columns="SELL",
                sort_types="-1",
            )
            if sell_data:
                lines.append("\n### 卖出席位 TOP5")
                lines.append("营业部 | 买入(万) | 卖出(万) | 净额(万)")
                for row in sell_data[:5]:
                    buy_amt = round((row.get("BUY") or 0) / 10000, 1)
                    sell_amt = round((row.get("SELL") or 0) / 10000, 1)
                    net = round((row.get("NET") or 0) / 10000, 1)
                    lines.append(
                        f"  {row.get('OPERATEDEPT_NAME', '')} "
                        f"| {buy_amt:.0f} | {sell_amt:.0f} | {net:.0f}"
                    )
    except Exception:
        pass

    # 3. 机构动向 — 从买卖席位明细筛选机构专用席位 (OPERATEDEPT_CODE="0")
    try:
        inst_buy = 0.0
        inst_sell = 0.0
        for detail, side in [(buy_data, "buy"), (sell_data, "sell")]:
            for row in (detail or []):
                if str(row.get("OPERATEDEPT_CODE", "")) == "0":
                    if side == "buy":
                        inst_buy += (row.get("BUY") or 0)
                    else:
                        inst_sell += (row.get("SELL") or 0)
        if inst_buy > 0 or inst_sell > 0:
            lines.append("\n## 机构动向")
            lines.append(
                f"  机构买入 {inst_buy/1e4:.0f} 万 "
                f"| 卖出 {inst_sell/1e4:.0f} 万 "
                f"| 净额 {(inst_buy - inst_sell)/1e4:.0f} 万"
            )
    except Exception:
        pass

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 16. Lockup Expiry Calendar (限售解禁日历)
# ---------------------------------------------------------------------------

def get_lockup_expiry(
    ticker: str,
    trade_date: str,
    forward_days: int = 90,
) -> str:
    """Get lockup expiry schedule for a stock.

    Args:
        ticker: 6-digit A-share code
        trade_date: YYYY-MM-DD
        forward_days: how many days forward to check (default 90)

    Returns:
        Formatted text with historical unlock records and upcoming
        expiry calendar with impact metrics.
    """
    code = safe_ticker_component(ticker)
    lines = [f"# 限售解禁日历 | {code} | {trade_date}"]

    # 1. 历史解禁记录 — eastmoney datacenter direct HTTP
    try:
        history_data = _eastmoney_datacenter(
            "RPT_LIFT_STAGE",
            filter_str=f"(SECURITY_CODE=\"{code}\")",
            page_size=15,
            sort_columns="FREE_DATE",
            sort_types="-1",
        )
        if history_data:
            lines.append(f"\n## 个股解禁记录 (共 {len(history_data)} 批)")
            lines.append("解禁时间 | 类型 | 解禁数量 | 占比")
            for row in history_data:
                lines.append(
                    f"  {str(row.get('FREE_DATE', ''))[:10]} "
                    f"| {row.get('LIMITED_STOCK_TYPE', '')} "
                    f"| {row.get('FREE_SHARES_NUM', '')} "
                    f"| {row.get('FREE_RATIO', '')}"
                )
        else:
            lines.append("\n无历史解禁记录。")
    except Exception as e:
        lines.append(f"个股解禁查询失败: {e}")

    # 2. 未来待解禁 — eastmoney datacenter direct HTTP
    try:
        end_dt = datetime.strptime(trade_date, "%Y-%m-%d") + pd.Timedelta(
            days=forward_days
        )
        end_str = end_dt.strftime("%Y-%m-%d")
        upcoming_data = _eastmoney_datacenter(
            "RPT_LIFT_STAGE",
            filter_str=(
                f"(SECURITY_CODE=\"{code}\")"
                f"(FREE_DATE>='{trade_date}')"
                f"(FREE_DATE<='{end_str}')"
            ),
            page_size=20,
            sort_columns="FREE_DATE",
            sort_types="1",
        )
        if upcoming_data:
            lines.append(f"\n## 未来 {forward_days} 天待解禁")
            for row in upcoming_data:
                lines.append(
                    f"  {str(row.get('FREE_DATE', ''))[:10]} "
                    f"| {row.get('LIMITED_STOCK_TYPE', '')} "
                    f"| 数量 {row.get('FREE_SHARES_NUM', '')} "
                    f"| 占比 {row.get('FREE_RATIO', '')}"
                )
        else:
            lines.append(f"\n未来 {forward_days} 天无待解禁。")
    except Exception as e:
        lines.append(f"解禁日历查询失败: {e}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 17. Industry Comparison (行业横向对比)
# ---------------------------------------------------------------------------

# Industry board clist hosts: primary then delay mirror (same schema).
_EM_INDUSTRY_CLIST_HOSTS = (
    "https://push2.eastmoney.com",
    "https://push2delay.eastmoney.com",
)


def get_industry_comparison(
    ticker: str,
    trade_date: str,
    top_n: int = 20,
) -> str:
    """Get industry sector performance comparison.

    Args:
        ticker: 6-digit A-share code (used to identify relevant sector)
        trade_date: YYYY-MM-DD
        top_n: number of top/bottom industries to show (default 20)

    Returns:
        Formatted text with sector performance ranking, highlighting
        the sector the target stock belongs to.
    """
    code = safe_ticker_component(ticker)
    lines = [f"# 行业横向对比 | {code} | {trade_date}"]

    params = {
        "pn": "1",
        "pz": "100",
        "po": "1",
        "np": "1",
        "fltt": "2",
        "invt": "2",
        "fs": "m:90+t:2",
        "fields": "f2,f3,f4,f12,f13,f14,f104,f105,f128,f136,f140,f141,f207",
    }
    items: list = []
    last_err: Exception | None = None
    used_host = ""

    for host in _EM_INDUSTRY_CLIST_HOSTS:
        url = f"{host}/api/qt/clist/get"
        try:
            r = _em_get(url, params=params, timeout=15)
            # Empty / HTML 502 bodies → try next host
            if not (r.text or "").strip() or (r.text or "").lstrip().startswith("<"):
                raise ValueError(
                    f"empty or non-JSON industry payload (HTTP {r.status_code})"
                )
            d = r.json()
            items = d.get("data", {}).get("diff", []) or []
            if items:
                used_host = host
                break
            last_err = ValueError(f"{host}: empty industry diff")
        except Exception as e:
            last_err = e
            logger.warning("industry clist failed via %s: %s", host, e)

    if items:
        host_tag = "push2delay" if "push2delay" in used_host else "push2"
        lines.append(
            f"\n## 全行业表现 (东财 {host_tag} {len(items)} 个行业)"
        )
        lines.append(
            "排名 | 行业 | 涨跌幅 | 上涨 | 下跌 | 领涨股"
        )
        for i, item in enumerate(items):
            name = item.get("f14", "")
            change_pct = item.get("f3", 0)
            up_count = item.get("f104", 0)
            down_count = item.get("f105", 0)
            leader = item.get("f140", "")
            lines.append(
                f"  {i+1}. {name} "
                f"| {change_pct}% "
                f"| {up_count} "
                f"| {down_count} "
                f"| {leader}"
            )
            if i >= top_n * 2 - 1:
                lines.append(f"  ... (showing top/bottom {top_n})")
                break
    else:
        lines.append(f"行业对比查询失败: {last_err or 'empty'}")
        try:
            concept = _em_concept_blocks(code)
            if concept:
                lines.append(
                    "\n## 个股所属板块兜底 (东财 CoreConception)\n"
                    "全市场行业排名暂不可用，以下为该股板块归属："
                )
                lines.append(concept)
        except Exception as fb_exc:
            lines.append(f"板块兜底也失败: {fb_exc}")

    return "\n".join(lines)
