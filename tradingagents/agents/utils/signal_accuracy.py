"""Signal accuracy ledger — track direction hit rate after analysis.

Extends the trading-memory workflow with a structured JSON ledger that:
- enrolls every final decision (full history auto-track)
- settles 1d / 5d / 20d horizons automatically when price bars exist
- scores direction hit (long/short/neutral) with a configurable epsilon band

The markdown ``TradingMemoryLog`` remains the PM prompt source; this ledger is
the authority for accuracy metrics.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from tradingagents.agents.utils.rating import parse_rating

logger = logging.getLogger(__name__)

DEFAULT_HORIZONS: tuple[int, ...] = (1, 5, 20)
DEFAULT_EPS: float = 0.005

CloseFetcher = Callable[[str, str, int], Optional[Sequence[float]]]
# (ticker, trade_date, need_bars) -> closes[0]=anchor, closes[h]=price h trading days later

_LONG = frozenset({"buy", "overweight"})
_SHORT = frozenset({"sell", "underweight"})

_LOG_DATE_RE = re.compile(r"full_states_log_(\d{4}-\d{2}-\d{2})\.json$")


def rating_to_direction(rating: str) -> str:
    """Map 5-tier (or collapsed) rating → long / short / neutral."""
    key = (rating or "").strip().lower()
    if key in _LONG:
        return "long"
    if key in _SHORT:
        return "short"
    return "neutral"


def rating_bucket(rating: str) -> str:
    """Collapse 5-tier rating to Buy / Sell / Hold for sample keys."""
    d = rating_to_direction(rating)
    if d == "long":
        return "Buy"
    if d == "short":
        return "Sell"
    return "Hold"


def direction_hit(direction: str, ret: float, eps: float = DEFAULT_EPS) -> bool:
    """Direction-hit rule with neutral bandwidth ±eps."""
    if direction == "long":
        return ret > eps
    if direction == "short":
        return ret < -eps
    return abs(ret) <= eps


def return_at_horizon(closes: Sequence[float], horizon: int) -> Optional[float]:
    """Absolute return from closes[0] to closes[horizon] (trading-day steps)."""
    if horizon < 1 or len(closes) <= horizon:
        return None
    anchor = float(closes[0])
    if anchor == 0.0:
        return None
    return float(closes[horizon]) / anchor - 1.0


def config_fingerprint(config: Optional[dict] = None) -> str:
    """Stable short hash of model/debate knobs that affect signal quality."""
    cfg = config or {}
    payload = {
        "llm_provider": cfg.get("llm_provider"),
        "deep_think_llm": cfg.get("deep_think_llm"),
        "quick_think_llm": cfg.get("quick_think_llm"),
        "max_debate_rounds": cfg.get("max_debate_rounds"),
        "max_risk_discuss_rounds": cfg.get("max_risk_discuss_rounds"),
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def sample_id(
    ticker: str,
    trade_date: str,
    rating: str,
    config_fp: str,
) -> str:
    bucket = rating_bucket(rating)
    return f"{trade_date}|{str(ticker).upper()}|{bucket}|{config_fp or '-'}"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _empty_horizon() -> dict:
    return {
        "status": "pending",
        "return": None,
        "hit": None,
        "settled_at": None,
    }


class SignalAccuracyLedger:
    """JSON ledger of analysis signals and multi-horizon direction outcomes."""

    def __init__(
        self,
        path: Path | str,
        *,
        horizons: Sequence[int] = DEFAULT_HORIZONS,
        eps: float = DEFAULT_EPS,
    ):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.horizons = tuple(int(h) for h in horizons)
        self.eps = float(eps)
        self._data: Dict[str, Any] = {"version": 1, "records": []}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            self._data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(self._data.get("records"), list):
                self._data = {"version": 1, "records": []}
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Corrupt accuracy ledger at %s: %s", self.path, exc)
            self._data = {"version": 1, "records": []}

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self._data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def records(self) -> List[dict]:
        return list(self._data.get("records") or [])

    def enroll(
        self,
        ticker: str,
        trade_date: str,
        rating: str,
        *,
        config_fp: str = "",
        decision: str = "",
        source: str = "live",
    ) -> dict:
        """Enroll a signal. Dedupes by (ticker, date, rating_bucket, config_fp)."""
        ticker_u = str(ticker).upper().strip()
        rating_c = (rating or parse_rating(decision) or "Hold").strip()
        # Normalise capitalisation for known tiers
        for canon in ("Buy", "Overweight", "Hold", "Underweight", "Sell"):
            if rating_c.lower() == canon.lower():
                rating_c = canon
                break
        sid = sample_id(ticker_u, trade_date, rating_c, config_fp)
        for existing in self._data["records"]:
            if existing.get("id") == sid:
                return existing

        rec = {
            "id": sid,
            "ticker": ticker_u,
            "trade_date": trade_date,
            "rating": rating_c,
            "direction": rating_to_direction(rating_c),
            "config_fingerprint": config_fp or "",
            "source": source,
            "enrolled_at": _utc_now_iso(),
            "decision_excerpt": (decision or "")[:500],
            "horizons": {str(h): _empty_horizon() for h in self.horizons},
        }
        self._data["records"].append(rec)
        self._save()
        return rec

    def migrate_from_memory_log(self, memory_log, *, config_fp: str = "legacy") -> int:
        """Enroll pending + resolved memory entries. Returns newly enrolled count."""
        added = 0
        for entry in memory_log.load_entries():
            rating = entry.get("rating") or parse_rating(entry.get("decision") or "")
            before = len(self._data["records"])
            self.enroll(
                ticker=entry["ticker"],
                trade_date=entry["date"],
                rating=rating,
                config_fp=config_fp,
                decision=entry.get("decision") or "",
                source="memory",
            )
            if len(self._data["records"]) > before:
                added += 1
        return added

    def migrate_from_results_dir(
        self,
        results_dir: Path | str,
        *,
        config_fp: str = "legacy",
    ) -> int:
        """Scan full_states_log_*.json and enroll signals. Returns new count."""
        root = Path(results_dir).expanduser()
        if not root.exists():
            return 0
        added = 0
        for log_file in sorted(root.rglob("full_states_log_*.json")):
            m = _LOG_DATE_RE.search(log_file.name)
            if not m:
                continue
            trade_date = m.group(1)
            ticker = log_file.parent.parent.name
            try:
                state = json.loads(log_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            rating = _rating_from_state(state)
            before = len(self._data["records"])
            self.enroll(
                ticker=ticker,
                trade_date=trade_date,
                rating=rating,
                config_fp=config_fp,
                decision=str(state.get("final_trade_decision") or ""),
                source="results_log",
            )
            if len(self._data["records"]) > before:
                added += 1
        return added

    def summary(self) -> dict:
        """Aggregate hit rates by horizon, direction, and config fingerprint."""
        by_horizon: Dict[str, dict] = {
            str(h): {"settled": 0, "hits": 0, "hit_rate": None} for h in self.horizons
        }
        by_direction: Dict[str, Dict[str, dict]] = {}
        by_config: Dict[str, Dict[str, dict]] = {}
        pending = 0

        def _bump(bucket: dict, hkey: str, hit: bool) -> None:
            cell = bucket.setdefault(
                hkey, {"settled": 0, "hits": 0, "hit_rate": None}
            )
            cell["settled"] += 1
            if hit:
                cell["hits"] += 1

        for rec in self.records():
            direction = rec.get("direction") or "neutral"
            cfg = rec.get("config_fingerprint") or "-"
            for hkey, cell in (rec.get("horizons") or {}).items():
                if cell.get("status") != "settled":
                    if cell.get("status") == "pending":
                        pending += 1
                    continue
                hit = bool(cell.get("hit"))
                by_horizon[hkey]["settled"] += 1
                if hit:
                    by_horizon[hkey]["hits"] += 1
                _bump(by_direction.setdefault(direction, {}), hkey, hit)
                _bump(by_config.setdefault(cfg, {}), hkey, hit)

        for cell in by_horizon.values():
            n = cell["settled"]
            cell["hit_rate"] = (cell["hits"] / n) if n else None
        for group in (by_direction, by_config):
            for bucket in group.values():
                for cell in bucket.values():
                    n = cell["settled"]
                    cell["hit_rate"] = (cell["hits"] / n) if n else None

        return {
            "total_records": len(self.records()),
            "pending_horizons": pending,
            "by_horizon": by_horizon,
            "by_direction": by_direction,
            "by_config": by_config,
            "eps": self.eps,
            "horizons": list(self.horizons),
        }


def _rating_from_state(state: dict) -> str:
    plan = state.get("action_plan")
    if isinstance(plan, dict) and plan.get("rating"):
        return str(plan["rating"])
    return parse_rating(str(state.get("final_trade_decision") or ""))


def settle_due(
    ledger: SignalAccuracyLedger,
    price_fetcher: CloseFetcher,
    *,
    as_of: Optional[date] = None,  # reserved for future calendar gating
    eps: Optional[float] = None,
) -> List[dict]:
    """Settle all pending horizons with enough bars. Returns new settle events."""
    del as_of  # kept for API stability / future calendar cutoffs
    band = ledger.eps if eps is None else float(eps)
    events: List[dict] = []
    changed = False
    max_h = max(ledger.horizons) if ledger.horizons else 1

    for rec in ledger._data["records"]:
        pending_hs = [
            int(h)
            for h, cell in (rec.get("horizons") or {}).items()
            if cell.get("status") == "pending"
        ]
        if not pending_hs:
            continue
        # Hint: ask for the longest pending horizon; fetcher may return fewer bars.
        need = max(max(pending_hs), max_h) + 1
        try:
            closes = price_fetcher(rec["ticker"], rec["trade_date"], need)
        except Exception as exc:  # noqa: BLE001 — never block the batch
            logger.warning(
                "Price fetch failed for %s @ %s: %s",
                rec["ticker"],
                rec["trade_date"],
                exc,
            )
            closes = None
        if not closes:
            continue

        direction = rec.get("direction") or rating_to_direction(rec.get("rating", ""))
        for h in pending_hs:
            ret = return_at_horizon(closes, h)
            if ret is None:
                continue
            hit = direction_hit(direction, ret, eps=band)
            cell = rec["horizons"][str(h)]
            cell["status"] = "settled"
            cell["return"] = round(float(ret), 6)
            cell["hit"] = hit
            cell["settled_at"] = _utc_now_iso()
            changed = True
            events.append(
                {
                    "id": rec["id"],
                    "ticker": rec["ticker"],
                    "trade_date": rec["trade_date"],
                    "rating": rec["rating"],
                    "direction": direction,
                    "horizon": h,
                    "return": cell["return"],
                    "hit": hit,
                    "decision_excerpt": rec.get("decision_excerpt") or "",
                }
            )

    if changed:
        ledger._save()
    return events


def default_ledger_path(config: Optional[dict] = None) -> Path:
    cfg = config or {}
    explicit = cfg.get("signal_accuracy_path")
    if explicit:
        return Path(explicit).expanduser()
    mem = cfg.get("memory_log_path")
    if mem:
        return Path(mem).expanduser().parent / "signal_accuracy.json"
    return Path.home() / ".tradingagents" / "memory" / "signal_accuracy.json"


def get_ledger(config: Optional[dict] = None) -> SignalAccuracyLedger:
    cfg = config or {}
    horizons = cfg.get("signal_accuracy_horizons") or DEFAULT_HORIZONS
    eps = cfg.get("signal_accuracy_eps")
    if eps is None:
        eps = DEFAULT_EPS
    return SignalAccuracyLedger(
        default_ledger_path(cfg),
        horizons=tuple(horizons),
        eps=float(eps),
    )


def to_yf_symbol(ticker: str) -> str:
    """Best-effort Yahoo symbol for A-share 6-digit codes."""
    t = str(ticker).upper().strip()
    if t.endswith((".SS", ".SZ", ".BJ")):
        return t
    if t.isdigit() and len(t) == 6:
        if t.startswith(("6", "9")):
            return f"{t}.SS"
        if t.startswith(("0", "3")):
            return f"{t}.SZ"
        if t.startswith(("4", "8")):
            return f"{t}.BJ"
    return t


def yfinance_close_fetcher(
    ticker: str, trade_date: str, need_bars: int
) -> Optional[List[float]]:
    """Fetch close series via yfinance; closes[0] is first bar on/after trade_date."""
    try:
        from datetime import timedelta

        import yfinance as yf

        start = datetime.strptime(trade_date, "%Y-%m-%d")
        # Calendar buffer for weekends/holidays; need_bars trading days.
        end = start + timedelta(days=need_bars * 3 + 14)
        hist = yf.Ticker(to_yf_symbol(ticker)).history(
            start=trade_date, end=end.strftime("%Y-%m-%d")
        )
        if hist is None or hist.empty or "Close" not in hist.columns:
            return None
        closes = [float(x) for x in hist["Close"].tolist()]
        # Return whatever bars exist; settle_due only settles horizons that fit.
        return closes if closes else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("yfinance close fetch failed for %s: %s", ticker, exc)
        return None


def run_accuracy_maintenance(
    config: Optional[dict] = None,
    *,
    force_remigrate: bool = False,
    sync_memory: bool = True,
    price_fetcher: Optional[CloseFetcher] = None,
) -> dict:
    """Backfill ledger from history (once) and settle due horizons.

    Returns ``{migrated, events, summary}``. Safe to call from Web/CLI without
    constructing a full ``TradingAgentsGraph``.
    """
    from tradingagents.agents.utils.memory import TradingMemoryLog

    cfg = config or {}
    ledger = get_ledger(cfg)
    memory = TradingMemoryLog(cfg)
    marker = ledger.path.with_suffix(".migrated")

    if force_remigrate and marker.exists():
        try:
            marker.unlink()
        except OSError:
            pass

    if marker.exists():
        migrated: dict = {"skipped": True, "from_memory": 0, "from_logs": 0}
    else:
        cfg_fp = "legacy"
        from_mem = ledger.migrate_from_memory_log(memory, config_fp=cfg_fp)
        from_logs = ledger.migrate_from_results_dir(
            cfg.get("results_dir") or "",
            config_fp=cfg_fp,
        )
        migrated = {"skipped": False, "from_memory": from_mem, "from_logs": from_logs}
        try:
            marker.write_text(
                json.dumps(
                    {
                        "migrated_at": datetime.now(timezone.utc)
                        .replace(microsecond=0)
                        .isoformat(),
                        "from_memory": from_mem,
                        "from_logs": from_logs,
                    }
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("Could not write accuracy migrate marker: %s", exc)

    fetcher = price_fetcher or yfinance_close_fetcher
    events = settle_due(ledger, fetcher)

    if sync_memory and events:
        _sync_pending_memory_from_events(memory, events)

    return {
        "migrated": migrated,
        "events": events,
        "summary": ledger.summary(),
    }


def _sync_pending_memory_from_events(memory_log, events: List[dict]) -> None:
    """Resolve pending markdown memory entries when 5d direction checks settle."""
    pending = {
        (e["date"], str(e["ticker"]).upper()): e
        for e in memory_log.get_pending_entries()
    }
    updates = []
    seen = set()
    for ev in events:
        if int(ev.get("horizon") or 0) != 5:
            continue
        key = (ev["trade_date"], str(ev["ticker"]).upper())
        if key not in pending or key in seen:
            continue
        seen.add(key)
        hit_s = "HIT" if ev.get("hit") else "MISS"
        reflection = (
            f"Auto-settled 5d direction check: {hit_s} "
            f"(direction={ev.get('direction')}, return={float(ev['return']):+.2%})."
        )
        updates.append(
            {
                "ticker": ev["ticker"],
                "trade_date": ev["trade_date"],
                "raw_return": float(ev["return"]),
                "alpha_return": 0.0,
                "holding_days": 5,
                "reflection": reflection,
            }
        )
    if updates:
        memory_log.batch_update_with_outcomes(updates)
