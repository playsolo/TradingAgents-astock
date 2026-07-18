import logging
import os
import time
from typing import Annotated

import pandas as pd
import yfinance as yf
from stockstats import wrap
from yfinance.exceptions import YFRateLimitError

from .config import get_config
from .utils import safe_ticker_component

logger = logging.getLogger(__name__)

# A vendor's latest OHLCV row this many calendar days before the requested date
# is treated as stale. Generous enough to span long holiday weekends, tight
# enough to catch the year-old frames yfinance occasionally returns (#1021).
MAX_OHLCV_STALE_DAYS = 10

# datetime.weekday(): Monday=0 ... Saturday=5, Sunday=6. Days >= this are the
# weekend, when no daily bar is produced.
_FIRST_WEEKEND_WEEKDAY = 5

# Don't re-attempt a stale-cache refresh for the same file more than once per
# this window. Daily bars land at most once a day, so a few minutes of staleness
# is harmless, while this collapses the many load_ohlcv calls in a single run
# (one per indicator) into a single fetch — even when the refresh fails or a
# holiday/closure means no newer bar will ever land.
_CACHE_REFRESH_MIN_INTERVAL_SECONDS = 300

# Monotonic timestamp of the last refresh *attempt* per cache file. Keyed in
# process memory (not file mtime) so a failed download — which never rewrites
# the file — still throttles the rest of the run. A fresh process starts empty,
# so a later rerun is always allowed one attempt to pick up newly-landed bars.
_last_refresh_attempt: dict[str, float] = {}

# How long a same-day cache that does not yet reach the requested day may be
# reused before it is refetched (#1150). Short enough that an intraday run picks
# up today's close soon after it publishes, long enough that a day with no bar
# at all (weekend, holiday) cannot trigger a download on every call.
OHLCV_CACHE_TTL_SECONDS = 900


def yf_retry(func, max_retries=3, base_delay=2.0):
    """Execute a yfinance call with exponential backoff on rate limits.

    yfinance raises YFRateLimitError on HTTP 429 responses but does not
    retry them internally. This wrapper adds retry logic specifically
    for rate limits. Other exceptions propagate immediately.
    """
    for attempt in range(max_retries + 1):
        try:
            return func()
        except YFRateLimitError:
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                logger.warning(f"Yahoo Finance rate limited, retrying in {delay:.0f}s (attempt {attempt + 1}/{max_retries})")
                time.sleep(delay)
            else:
                raise


def _ensure_date_column(data: pd.DataFrame) -> pd.DataFrame:
    """Normalize the date column to ``Date``.

    Some yfinance builds leave the index unnamed (so ``reset_index()`` yields
    ``index``) or use ``Datetime`` for intraday data. Rename the first
    date-like column so indicators don't silently drop when it isn't ``Date``.
    """
    if "Date" in data.columns:
        return data
    for candidate in ("index", "Datetime", "date"):
        if candidate in data.columns:
            return data.rename(columns={candidate: "Date"})
    return data


def _clean_dataframe(data: pd.DataFrame) -> pd.DataFrame:
    """Normalize a stock DataFrame for stockstats: parse dates, drop invalid rows, fill price gaps."""
    data = _ensure_date_column(data)
    data["Date"] = pd.to_datetime(data["Date"], errors="coerce")
    data = data.dropna(subset=["Date"])

    price_cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in data.columns]
    data[price_cols] = data[price_cols].apply(pd.to_numeric, errors="coerce")

    # _download_ohlcv already tries to fill NaN Close from the quote page or
    # from typical price. This is a final safety net so that cached data written
    # before those fixes were deployed still works.
    try:
        from ._nan_close_fallback import estimate_close_from_typical_price

        data = estimate_close_from_typical_price(data)
    except Exception:
        pass

    data[price_cols] = data[price_cols].ffill().bfill()
    data = data.dropna(subset=["Close"])

    return data


def _coerce_ohlcv_dates(data: pd.DataFrame) -> pd.Series:
    """Return parsed dates from an OHLCV frame, whether Date is a column or the index."""
    if "Date" in data.columns:
        return pd.to_datetime(data["Date"], errors="coerce").dropna()
    # yfinance keeps the dates in the index (a DatetimeIndex, sometimes unnamed).
    if isinstance(data.index, pd.DatetimeIndex):
        return pd.Series(pd.to_datetime(data.index, errors="coerce")).dropna()
    # Fallback: expose the index and look for any date-like column.
    df = data.reset_index()
    for col in ("Date", "Datetime", "date", "index"):
        if col in df.columns:
            parsed = pd.to_datetime(df[col], errors="coerce").dropna()
            if not parsed.empty:
                return parsed
    return pd.Series(dtype="datetime64[ns]")


def _most_recent_completed_weekday(today: pd.Timestamp) -> pd.Timestamp:
    """Return the most recent weekday strictly before ``today``.

    Used as a source-agnostic lower bound for "a daily bar that should already
    exist". We deliberately exclude ``today`` itself: for an instrument on a
    foreign exchange (e.g. a US ticker viewed from GMT+8) today's session is
    often still open or unsettled, so demanding today's bar would force a
    re-download on every run. Holidays are not modelled here; a missing holiday
    bar simply means the cache stays one weekday behind until real data lands.
    """
    day = today.normalize() - pd.Timedelta(days=1)
    while day.weekday() >= _FIRST_WEEKEND_WEEKDAY:
        day -= pd.Timedelta(days=1)
    return day


def _latest_ohlcv_date(data: pd.DataFrame) -> pd.Timestamp | None:
    """Return the newest (normalized) date in an OHLCV frame, or None if empty."""
    dates = _coerce_ohlcv_dates(data)
    return dates.max().normalize() if not dates.empty else None


def _cache_is_behind(
    data: pd.DataFrame,
    curr_date_dt: pd.Timestamp,
    today_date: pd.Timestamp,
) -> bool:
    """Decide whether a cache hit is stale enough to justify re-downloading.

    The per-symbol cache filename only changes once per local calendar day, so
    the first fetch of the day freezes the data for the rest of that day. When
    that first fetch happened before a foreign exchange had posted its latest
    daily bar (the classic "7/15 analysis shows 7/13 data" seen from GMT+8),
    intraday reruns would otherwise keep serving the frozen, incomplete cache.

    Returns True only for *live/current* analysis whose cached data stops before
    the most recent completed weekday. Historical backtest dates (curr_date well
    before the cache's latest row) never trigger a refresh, so replayed runs
    stay deterministic and look-ahead-free.
    """
    dates = _coerce_ohlcv_dates(data)
    if dates.empty:
        return False
    cached_latest = dates.max().normalize()
    # Backtest: the cache already extends past the requested date — never refetch.
    if curr_date_dt.normalize() < cached_latest:
        return False
    return cached_latest < _most_recent_completed_weekday(today_date)


def _download_ohlcv(
    symbol: str, start_str: str, end_str: str
) -> pd.DataFrame:
    """Download OHLCV from Yahoo, raising ValueError when nothing comes back.

    Three-layer fallback for NaN Close in recent rows:
    1. ``auto_adjust=True`` (preferred for dividend-adjusted prices)
    2. ``auto_adjust=False`` when adjusted data has NaN Close
    3. Yahoo Finance quote-page scrape + typical-price estimate
    """
    downloaded = yf_retry(lambda: yf.download(
        symbol,
        start=start_str,
        end=end_str,
        multi_level_index=False,
        progress=False,
        auto_adjust=True,
    ))
    downloaded = _ensure_date_column(downloaded.reset_index())
    if downloaded.empty or "Close" not in downloaded.columns:
        raise ValueError(f"Yahoo Finance returned no rows for {symbol}")

    # Layer 2: fall back to unadjusted data when auto_adjust produces NaN Close.
    recent = downloaded.tail(5)
    if "Close" in recent.columns and recent["Close"].isna().any():
        logger.info(
            "auto_adjust=True returned NaN Close for recent %s rows; "
            "retrying with auto_adjust=False",
            symbol,
        )
        raw = yf_retry(lambda: yf.download(
            symbol,
            start=start_str,
            end=end_str,
            multi_level_index=False,
            progress=False,
            auto_adjust=False,
        ))
        raw = _ensure_date_column(raw.reset_index())
        if not raw.empty and "Close" in raw.columns:
            downloaded = raw

    # Layer 3: fill remaining NaN Close from the Yahoo Finance quote page
    # (always correct) or estimate from typical price (within ~2.5 %).
    try:
        from ._nan_close_fallback import fill_nan_close_from_web

        downloaded = fill_nan_close_from_web(downloaded, symbol)
    except Exception:
        pass  # web scrape is best-effort

    return downloaded


def _needs_same_day_refresh(data_file, curr_date_dt, today_date) -> bool:
    """Whether a cached frame must be refetched to reflect the requested day.

    The cache file is keyed per day, so without this a run started before the
    day's bar was final keeps serving that snapshot to every later run (#1150).
    Two distinct staleness cases exist for a current-day request: the bar may be
    missing entirely, or present but still in progress — Yahoo publishes a
    partial daily candle during market hours, whose ``Close`` is not the closing
    price. Row inspection cannot tell a partial bar from a final one, so the TTL
    governs every current-day cache. Historical requests always reuse the cache,
    since those rows are immutable.
    """
    if curr_date_dt.date() < today_date.date():
        return False
    return time.time() - os.path.getmtime(data_file) > OHLCV_CACHE_TTL_SECONDS


def load_ohlcv(symbol: str, curr_date: str) -> pd.DataFrame:
    """Fetch OHLCV data with caching, filtered to prevent look-ahead bias.

    Downloads 5 years of data up to today and caches per symbol. On
    subsequent calls the cache is reused. Rows after curr_date are
    filtered out so backtests never see future prices.
    """
    # Reject ticker values that would escape the cache directory when
    # interpolated into the cache filename (e.g. ``../../tmp/x``).
    safe_symbol = safe_ticker_component(symbol)

    config = get_config()
    curr_date_dt = pd.to_datetime(curr_date)

    # Cache uses a fixed window (5y to today) so one file per symbol.
    today_date = pd.Timestamp.today()
    start_date = today_date - pd.DateOffset(years=5)
    start_str = start_date.strftime("%Y-%m-%d")
    # yfinance ``end`` is EXCLUSIVE; request tomorrow so today's row is included
    # when curr_date is the current day. Look-ahead is still prevented by the
    # curr_date filter below.
    end_str = (today_date + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

    os.makedirs(config["data_cache_dir"], exist_ok=True)
    data_file = os.path.join(
        config["data_cache_dir"],
        f"{safe_symbol}-YFin-data-{start_str}-{end_str}.csv",
    )

    # A cached file may be empty if a prior fetch failed (unknown symbol,
    # transient rate limit). Treat an empty/columnless cache as a miss and
    # re-fetch rather than serving the poisoned file forever.
    data = None
    if os.path.exists(data_file):
        cached = pd.read_csv(data_file, on_bad_lines="skip", encoding="utf-8")
        # Serve the cache only when it is usable and not a stale snapshot of the
        # day being requested (#1150); otherwise fall through and refetch.
        if (
            not cached.empty
            and "Close" in cached.columns
            and not _needs_same_day_refresh(data_file, curr_date_dt, today_date)
        ):
            data = cached

    # Refresh a same-day cache that stops before the latest completed trading
    # day so intraday reruns pick up newly-landed bars instead of the frozen
    # first-fetch snapshot. Throttle by last *attempt* time so a run's many
    # indicator calls (and holidays, where no newer bar will ever land) don't
    # re-download every call — even when the refresh fails. If the refresh
    # fails, keep the stale cache: partial data beats turning a transient error
    # into "no data".
    now = time.monotonic()
    last_attempt = _last_refresh_attempt.get(data_file)
    recently_attempted = (
        last_attempt is not None
        and now - last_attempt < _CACHE_REFRESH_MIN_INTERVAL_SECONDS
    )
    if (
        data is not None
        and not recently_attempted
        and _cache_is_behind(data, curr_date_dt, today_date)
    ):
        _last_refresh_attempt[data_file] = now
        try:
            refreshed = _download_ohlcv(symbol, start_str, end_str)
            # Only adopt the refresh when it is at least as fresh as the cache.
            # Yahoo sometimes returns an older/partial frame (#1021); overwriting
            # good on-disk data with it would poison the cache for the rest of
            # the day.
            refreshed_latest = _latest_ohlcv_date(refreshed)
            cached_latest = _latest_ohlcv_date(data)
            if refreshed_latest is not None and (
                cached_latest is None or refreshed_latest >= cached_latest
            ):
                refreshed.to_csv(data_file, index=False, encoding="utf-8")
                data = refreshed
            else:
                logger.warning(
                    "Refresh for %s returned older/partial data; keeping cache",
                    symbol,
                )
        except ValueError:
            logger.warning(
                "Stale cache refresh for %s returned no rows; keeping cache",
                symbol,
            )
        except Exception as e:  # network/rate-limit — fall back to the cache
            logger.warning(
                "Stale cache refresh for %s failed (%s); keeping cache",
                symbol,
                e,
            )

    if data is None:
        downloaded = _download_ohlcv(symbol, start_str, end_str)
        downloaded.to_csv(data_file, index=False, encoding="utf-8")
        data = downloaded

    data = _clean_dataframe(data)

    # Filter to curr_date to prevent look-ahead bias in backtesting
    data = data[data["Date"] <= curr_date_dt]

    return data


def filter_financials_by_date(data: pd.DataFrame, curr_date: str) -> pd.DataFrame:
    """Drop financial statement columns (fiscal period timestamps) after curr_date.

    yfinance financial statements use fiscal period end dates as columns.
    Columns after curr_date represent future data and are removed to
    prevent look-ahead bias.
    """
    if not curr_date or data.empty:
        return data
    cutoff = pd.Timestamp(curr_date)
    mask = pd.to_datetime(data.columns, errors="coerce") <= cutoff
    return data.loc[:, mask]


class StockstatsUtils:
    @staticmethod
    def get_stock_stats(
        symbol: Annotated[str, "ticker symbol for the company"],
        indicator: Annotated[
            str, "quantitative indicators based off of the stock data for the company"
        ],
        curr_date: Annotated[
            str, "curr date for retrieving stock price data, YYYY-mm-dd"
        ],
    ):
        data = load_ohlcv(symbol, curr_date)
        df = wrap(data)
        df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")
        curr_date_str = pd.to_datetime(curr_date).strftime("%Y-%m-%d")

        df[indicator]  # trigger stockstats to calculate the indicator
        matching_rows = df[df["Date"].str.startswith(curr_date_str)]

        if not matching_rows.empty:
            indicator_value = matching_rows[indicator].values[0]
            return indicator_value
        else:
            return "N/A: Not a trading day (weekend or holiday)"
