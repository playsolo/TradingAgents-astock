#!/usr/bin/env python3
"""Probe Eastmoney push2 endpoints for finer-grained ("L2-ish") data.

Goal: verify which endpoints are reachable WITHOUT login/membership, and how
much depth each exposes. All eastmoney calls go through a_stock._em_get() so
they honor the shared throttle/session (see CLAUDE.md "东财接口防封限流").

Probes:
  1. stock/get        — 5-level bid/ask snapshot (free, Level-1 depth)
  2. details/get      — per-3s transaction details / tick (free)
  3. details/sse      — SSE variant of the above
  4. push2his details — historical-day details (free)
  5. push2ex          — true L2 tick/order (expected: auth required)

Usage:
    python scripts/probe_em_l2.py [secid] [--ut TOKEN]
Example:
    python scripts/probe_em_l2.py 1.600519
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime

from tradingagents.dataflows import a_stock

_SECID_DEFAULT = "1.600519"  # 贵州茅台, Shanghai (market 1)
_UT_DEFAULT = "fa5fd1943c7b386f172d6893dbfba10b"

# 5-level depth (买一~买五 / 卖一~卖五 价/量) + snapshot basics.
_QUOTE_FIELDS = (
    "f43,f44,f45,f46,f47,f48,f57,f58,f60,f71,f86,f168,f169,f170,f171,f116,f117,"
    "f19,f20,f21,f22,f23,f24,f25,f26,f27,f28,f29,f30,f31,f32,f33,f34,f35,f36,f37,f38"
)

_DEPTH_FIELDS = [
    (19, 20, "买一"), (21, 22, "买二"), (23, 24, "买三"), (25, 26, "买四"), (27, 28, "买五"),
    (29, 30, "卖一"), (31, 32, "卖二"), (33, 34, "卖三"), (35, 36, "卖四"), (37, 38, "卖五"),
]


def _get(url: str, params: dict):
    t0 = time.monotonic()
    try:
        r = a_stock._em_get(url, params=params, timeout=15)
    except Exception as exc:  # noqa: BLE001 - probe must survive single-endpoint failures
        return None, None, time.monotonic() - t0, f"{type(exc).__name__}: {exc}"
    elapsed = time.monotonic() - t0
    try:
        data = r.json()
    except ValueError:
        data = None
    return r, data, elapsed, None


def _fmt(v):
    if v is None:
        return "-"
    if isinstance(v, (int, float)):
        return f"{v:,}"
    return str(v)


def probe_quote(secid: str, ut: str) -> dict:
    url = "https://push2.eastmoney.com/api/qt/stock/get"
    params = {
        "secid": secid,
        "fields": _QUOTE_FIELDS,
        "fltt": "2",
        "invt": "2",
        "ut": ut,
    }
    r, data, elapsed, err = _get(url, params)
    if err is not None:
        return {"name": "stock/get (5档快照)", "http": None, "latency_s": round(elapsed, 2), "note": err}
    d = (data or {}).get("data") or {}
    if isinstance(d, dict) and "f19" in d:
        levels = []
        for bp, bv, label in _DEPTH_FIELDS:
            price, vol = d.get(f"f{bp}"), d.get(f"f{bv}")
            levels.append((label, price, vol))
        return {
            "name": "stock/get (5档快照)",
            "http": r.status_code,
            "latency_s": round(elapsed, 2),
            "name_code": f"{d.get('f58')} {d.get('f57')}",
            "price": d.get("f43"),
            "levels_present": sum(1 for _, p, v in levels if p not in (None, "-")),
            "levels": levels,
            "raw_depth": {f"f{k}": d.get(f"f{k}") for k in range(19, 39)},
        }
    return {
        "name": "stock/get (5档快照)",
        "http": r.status_code,
        "latency_s": round(elapsed, 2),
        "note": f"no depth payload: rc={data.get('rc') if data else '-'}",
    }


def probe_details(url: str, name: str, secid: str, ut: str, date: str | None = None) -> dict:
    params = {
        "secid": secid,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55",
        "pos": "-1",
        "mpi": "2000",
        "fltt": "2",
        "ut": ut,
    }
    if date:
        params["date"] = date
    r, data, elapsed, err = _get(url, params)
    if err is not None:
        return {"name": name, "http": None, "latency_s": round(elapsed, 2), "note": err}
    payload = (data or {}).get("data") or {}
    rows = payload.get("details") or payload.get("data") or []
    if rows:
        return {
            "name": name,
            "http": r.status_code,
            "latency_s": round(elapsed, 2),
            "total_rows": len(rows),
            "pre_price": payload.get("prePrice"),
            "sample": [str(x).split(",") for x in rows[:5]],
        }
    return {
        "name": name,
        "http": r.status_code,
        "latency_s": round(elapsed, 2),
        "note": f"rc={data.get('rc') if data else '-'} details=空 (可能需 --date 指定交易日, 或该宿主当前无今日数据) payload={str(payload)[:120]!r}",
    }


def probe_push2ex(url: str, name: str, secid: str, ut: str) -> dict:
    params = {"secid": secid, "ut": ut, "uid": "", "dpt": "wz.ztzt"}
    r, data, elapsed, err = _get(url, params)
    if err is not None:
        return {"name": name, "http": None, "latency_s": round(elapsed, 2), "preview": err}
    return {
        "name": name,
        "http": r.status_code,
        "latency_s": round(elapsed, 2),
        "rc": (data or {}).get("code"),
        "preview": r.text[:160],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("secid", nargs="?", default=_SECID_DEFAULT, help="e.g. 1.600519 (沪) / 0.000001 (深)")
    ap.add_argument("--ut", default=_UT_DEFAULT, help="eastmoney ut token (defaults to a known-good one)")
    ap.add_argument("--date", default="", help="历史明细交易日 YYYYMMDD (仅 push2his details/get)")
    args = ap.parse_args()

    # Light probe run: don't wait the full batch interval between calls.
    a_stock._EM_MIN_INTERVAL = float(a_stock._EM_MIN_INTERVAL or 1.0)
    if a_stock._EM_MIN_INTERVAL >= 1.0:
        a_stock._EM_MIN_INTERVAL = 0.4
    a_stock._em_last_call[0] = 0.0

    print(f"== 东财免费端点探测 secid={args.secid} ({datetime.now():%Y-%m-%d %H:%M:%S}) ==", flush=True)

    results = []
    results.append(probe_quote(args.secid, args.ut))
    results.append(probe_details(
        "https://push2.eastmoney.com/api/qt/stock/details/get",
        "details/get (分时逐笔明细)", args.secid, args.ut,
    ))
    results.append(probe_details(
        "https://push2.eastmoney.com/api/qt/stock/details/sse",
        "details/sse (SSE变体)", args.secid, args.ut,
    ))
    results.append(probe_details(
        "https://push2his.eastmoney.com/api/qt/stock/details/get",
        "push2his details/get (历史明细)", args.secid, args.ut, args.date or None,
    ))
    results.append(probe_push2ex(
        "https://push2ex.eastmoney.com/getTopicZDFenBi",
        "push2ex 逐笔成交 (真L2)", args.secid, args.ut,
    ))
    results.append(probe_push2ex(
        "https://push2ex.eastmoney.com/getTopicDMMap",
        "push2ex 委托队列 (真L2)", args.secid, args.ut,
    ))

    for res in results:
        print("\n" + "=" * 70)
        print(f"[{res['name']}]  http={res.get('http')}  latency={res.get('latency_s')}s")
        if res.get("note"):
            print("  NOTE:", res["note"])
        if res.get("name_code"):
            print(f"  {res['name_code']}  最新价={_fmt(res['price'])}")
        if "levels_present" in res:
            print(f"  五档档位有效数: {res['levels_present']}/5 (买), 5 (卖字段)")
            for label, price, vol in res["levels"]:
                print(f"    {label}: {_fmt(price)} x {_fmt(vol)}")
            print("    raw:", res.get("raw_depth"))
        if res.get("total_rows"):
            print(f"  明细总条数: {res['total_rows']}  昨收={res.get('pre_price')}  (首5条: time, price, vol(手), amount(万), avg)")
            for row in res["sample"]:
                print("    " + ", ".join(_fmt(c) for c in row))
        if "rc" in res:
            print("  rc:", res.get("rc"))
            print("  preview:", res.get("preview"))
        elif res.get("name_code") is None and "levels_present" not in res \
                and res.get("total_rows") is None:
            print("  preview:", res.get("preview"))

    print("\n== 探测完成 ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
