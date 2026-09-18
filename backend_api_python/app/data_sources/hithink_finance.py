"""HiThink (同花顺) official financial data client.

Single exit point for every Tonghuashun call (base: ``fuyao.aicubes.cn``).
Only A-share data is covered: quotes, daily K-lines, corporate actions,
valuations, financial statements and the special-data boards (limit pools,
ladder, dragon-tiger, hot list, anomalies).

Envelope contract: every endpoint answers HTTP 200 with
``{"code": 0, "message": ..., "data": {...}}``. Non-zero ``code`` raises a
typed error so callers can decide between degrading to Tencent/AkShare
(``exc.degradable``) and hard-failing the request.

All timestamps in the API are Asia/Shanghai milliseconds. Timestamps returned
to callers here are UTC Unix seconds, matching ``BaseDataSource.get_kline``.
"""

from __future__ import annotations

import os
import re
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

import requests

from app.data_sources.errors import DataSourceError
from app.data_sources.rate_limiter import RateLimiter
from app.utils.cache import CacheManager
from app.utils.logger import get_logger
from app.utils.resource_guard import assert_fd_available

logger = get_logger(__name__)

SOURCE = "hithink_finance"
SOURCE_NAME = "HiThink Financial API"
DEFAULT_BASE_URL = "https://fuyao.aicubes.cn"
DEFAULT_TIMEOUT = 20
DEFAULT_MAX_RETRY = 3
DEFAULT_RATE_LIMIT_PER_MIN = 120

SHANGHAI_TZ = timezone(timedelta(hours=8))
_MARKET_OPEN = (9, 15)
_MARKET_CLOSE = (15, 30)

# Endpoint TTLs (seconds).
TTL_SNAPSHOT = 3
TTL_VALUATION = 300
TTL_FINANCIAL = 86_400
TTL_BOARD_TODAY = 300
TTL_BOARD_HISTORY = 86_400
TTL_LADDER = 300
TTL_CALENDAR = 86_400
TTL_DAILY_CLOSED = 21_600
TTL_DAILY_INTRADAY = 60

_PROXY_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)

# Envelope codes we translate into typed errors.
_CODE_INVALID_KEY = 2001
_CODE_FORBIDDEN = 2003
_CODE_RATE_LIMITED = 4001

_THSCODE_RE = re.compile(r"^(\d{6})\.(SH|SZ|BJ)$", re.IGNORECASE)


class HiThinkError(DataSourceError):
    """Base error for HiThink calls. ``degradable`` marks fallback-worthy failures."""

    degradable = False

    def __init__(self, message: str, *, code: Optional[int] = None, path: str = ""):
        self.code = code
        self.path = path
        super().__init__(message)


class HiThinkNotConfigured(HiThinkError):
    """API key missing or ``HITHINK_FINANCE_ENABLED`` is off."""

    degradable = True

    def __init__(self, message: str = "HiThink Financial API is disabled or has no API key"):
        super().__init__(message)


class HiThinkUnauthorized(HiThinkError):
    """Envelope ``code=2001`` — invalid API key."""

    degradable = True


class HiThinkForbidden(HiThinkError):
    """Envelope ``code=2003`` — key valid but the endpoint is not granted."""

    degradable = True


class HiThinkRateLimited(HiThinkError):
    """HTTP 429 or envelope ``code=4001``."""

    degradable = True


class HiThinkParamError(HiThinkError):
    """Envelope ``code=10xx`` — request rejected by the upstream contract."""


class HiThinkUnavailable(HiThinkError):
    """Transport/5xx/envelope ``5xxx`` failure."""

    degradable = True


def enabled() -> bool:
    """Global kill switch; ``false`` degrades the whole chain to Tencent/AkShare."""
    raw = str(os.getenv("HITHINK_FINANCE_ENABLED") or "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def api_key() -> str:
    return str(os.getenv("HITHINK_FINANCE_API_KEY") or "").strip()


def base_url() -> str:
    return str(os.getenv("HITHINK_FINANCE_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")


def _int_env(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name) or "").strip() or default)
    except (TypeError, ValueError):
        return default


def timeout_seconds() -> int:
    return max(1, _int_env("HITHINK_FINANCE_TIMEOUT", DEFAULT_TIMEOUT))


def max_retries() -> int:
    return max(1, _int_env("HITHINK_FINANCE_MAX_RETRY", DEFAULT_MAX_RETRY))


def configured() -> bool:
    return enabled() and bool(api_key())


# ---------------------------------------------------------------------------
# Symbol mapping (thscode <-> project symbol / Tencent code)
# ---------------------------------------------------------------------------

def to_thscode(symbol: str) -> str:
    """Normalize any A-share symbol form to ``600519.SH`` style thscode."""
    raw = str(symbol or "").strip().upper()
    if not raw:
        return ""
    match = _THSCODE_RE.match(raw)
    if match:
        return f"{match.group(1)}.{match.group(2).upper()}"
    if raw.endswith((".SS", ".SH")):
        return f"{raw[:-3]}.SH"
    if raw.endswith(".SZ"):
        return f"{raw[:-3]}.SZ"
    if raw.endswith(".BJ"):
        return f"{raw[:-3]}.BJ"
    digits = raw[2:] if raw[:2] in {"SH", "SZ", "BJ"} else raw
    if digits.isdigit() and len(digits) == 6:
        if digits.startswith("6"):
            return f"{digits}.SH"
        if digits.startswith(("4", "8", "92")):
            return f"{digits}.BJ"
        return f"{digits}.SZ"
    return ""


def to_project_symbol(thscode: str) -> str:
    """``600519.SH`` (thscode) -> project symbol, ``SH600519`` accepted as input."""
    raw = str(thscode or "").strip().upper()
    if not raw:
        return ""
    if raw[:2] in {"SH", "SZ", "BJ"} and raw[2:].isdigit():
        return f"{raw[2:]}.{raw[:2]}"
    return raw


def to_tencent_code(thscode: str) -> str:
    """thscode -> ``sh600519`` Tencent code."""
    project = to_project_symbol(thscode)
    if "." not in project:
        return project.lower()
    digits, suffix = project.split(".", 1)
    return f"{suffix.lower()}{digits}"


def shanghai_now() -> datetime:
    return datetime.now(SHANGHAI_TZ)


def shanghai_today() -> date:
    return shanghai_now().date()


def date_to_ms(value: date) -> int:
    """Asia/Shanghai 00:00 of ``value`` as epoch milliseconds."""
    return int(
        datetime(value.year, value.month, value.day, tzinfo=SHANGHAI_TZ).timestamp() * 1000
    )


def ms_to_date(value: Any) -> Optional[date]:
    try:
        return datetime.fromtimestamp(int(value) / 1000, SHANGHAI_TZ).date()
    except (TypeError, ValueError, OSError):
        return None


def ms_to_utc_seconds(value: Any) -> Optional[int]:
    try:
        return int(int(value) / 1000)
    except (TypeError, ValueError):
        return None


def parse_trade_date(value: Any) -> Optional[date]:
    """Accept ``date``/``datetime``/``2026-07-01``/``20260701``/ms epoch."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        return ms_to_date(int(value))
    raw = str(value).strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    if raw.isdigit():
        return ms_to_date(int(raw))
    return None


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_cache: Optional[CacheManager] = None
_limiter: Optional[RateLimiter] = None
_limiter_lock = threading.Lock()


def _get_cache() -> CacheManager:
    global _cache
    if _cache is None:
        with _cache_lock:
            if _cache is None:
                _cache = CacheManager()
    return _cache


def _get_limiter() -> RateLimiter:
    """One limiter for the whole process; per-minute budget spread over requests."""
    global _limiter
    if _limiter is None:
        with _limiter_lock:
            if _limiter is None:
                per_minute = max(1, _int_env("HITHINK_FINANCE_RATE_LIMIT_PER_MIN", DEFAULT_RATE_LIMIT_PER_MIN))
                interval = 60.0 / per_minute
                _limiter = RateLimiter(
                    min_interval=interval,
                    jitter_min=0.0,
                    jitter_max=min(0.2, interval),
                )
    return _limiter


@contextmanager
def _bypass_proxy():
    """HiThink is a domestic endpoint: never send it through the market-data proxy."""
    saved = {}
    for key in _PROXY_KEYS:
        value = os.environ.pop(key, None)
        if value is not None:
            saved[key] = value
    try:
        yield
    finally:
        for key, value in saved.items():
            os.environ[key] = value


def _cache_key(path: str, params: Optional[dict]) -> str:
    parts = [f"{key}={params[key]}" for key in sorted(params or {}) if params[key] is not None]
    return "hithink:" + path + ("?" + "&".join(parts) if parts else "")


def _unwrap(payload: dict, path: str) -> dict:
    code = payload.get("code")
    try:
        code_int = int(code)
    except (TypeError, ValueError):
        code_int = -1
    if code_int == 0:
        data = payload.get("data")
        return data if isinstance(data, dict) else {}
    message = str(payload.get("message") or "unknown error")
    detail = f"{SOURCE_NAME} {path} failed: code={code} {message}"
    if code_int == _CODE_INVALID_KEY:
        raise HiThinkUnauthorized(detail, code=code_int, path=path)
    if code_int == _CODE_FORBIDDEN:
        raise HiThinkForbidden(detail, code=code_int, path=path)
    if code_int == _CODE_RATE_LIMITED:
        raise HiThinkRateLimited(detail, code=code_int, path=path)
    if 1000 <= code_int <= 1004:
        raise HiThinkParamError(detail, code=code_int, path=path)
    if code_int >= 5000:
        raise HiThinkUnavailable(detail, code=code_int, path=path)
    raise HiThinkError(detail, code=code_int, path=path)


def _request(
    path: str,
    params: Optional[dict] = None,
    *,
    ttl: Optional[int] = None,
) -> dict:
    """GET one endpoint and return its unwrapped ``data`` payload."""
    if not enabled():
        raise HiThinkNotConfigured("HITHINK_FINANCE_ENABLED is false")
    key = api_key()
    if not key:
        raise HiThinkNotConfigured("HITHINK_FINANCE_API_KEY is not configured")

    cache_key = _cache_key(path, params)
    if ttl:
        cached = _get_cache().get(cache_key)
        if isinstance(cached, dict):
            return cached

    url = f"{base_url()}{path}"
    headers = {
        "X-api-key": key,
        "Accept": "application/json",
        "User-Agent": "QuantDinger/5.2",
    }
    attempts = max_retries()
    last_error: Optional[BaseException] = None
    for attempt in range(1, attempts + 1):
        try:
            assert_fd_available("HiThink request")
            _get_limiter().wait()
            with _bypass_proxy():
                with requests.get(
                    url, params=params or {}, headers=headers, timeout=timeout_seconds()
                ) as response:
                    status = int(response.status_code)
                    if status == 429:
                        raise HiThinkRateLimited(
                            f"{SOURCE_NAME} {path} rate limited (HTTP 429)", path=path
                        )
                    if status >= 500:
                        raise HiThinkUnavailable(
                            f"{SOURCE_NAME} {path} HTTP {status}", path=path
                        )
                    response.raise_for_status()
                    payload = response.json() if response.text else {}
            if not isinstance(payload, dict):
                raise HiThinkUnavailable(f"{SOURCE_NAME} {path} returned a non-object body", path=path)
            data = _unwrap(payload, path)
        except (HiThinkRateLimited, HiThinkUnavailable) as exc:
            last_error = exc
            if attempt >= attempts:
                break
            delay = min(30.0, 1.5 * (2 ** (attempt - 1)))
            logger.warning("%s; retry %s/%s in %.1fs", exc, attempt, attempts, delay)
            time.sleep(delay)
            continue
        except requests.RequestException as exc:
            last_error = HiThinkUnavailable(f"{SOURCE_NAME} {path} transport error: {exc}", path=path)
            if attempt >= attempts:
                break
            delay = min(30.0, 1.5 * (2 ** (attempt - 1)))
            logger.warning("%s; retry %s/%s in %.1fs", last_error, attempt, attempts, delay)
            time.sleep(delay)
            continue

        if ttl:
            _get_cache().set(cache_key, data, ttl=ttl)
        return data

    if isinstance(last_error, HiThinkError):
        raise last_error
    raise HiThinkUnavailable(f"{SOURCE_NAME} {path} failed after {attempts} attempts", path=path)


def _items(data: dict) -> List[dict]:
    items = data.get("item")
    if isinstance(items, dict):
        return [items]
    if isinstance(items, list):
        return [item for item in items if isinstance(item, dict)]
    return []


def _chunks(values: Sequence[str], size: int) -> Iterable[List[str]]:
    for index in range(0, len(values), size):
        yield list(values[index:index + size])


# ---------------------------------------------------------------------------
# Quotes / prices
# ---------------------------------------------------------------------------

def snapshot(thscodes: Iterable[str]) -> List[dict]:
    """Realtime snapshot. Accepts any symbol form; returns raw item dicts."""
    codes = [to_thscode(item) for item in thscodes]
    codes = [code for code in codes if code]
    if not codes:
        return []
    output: List[dict] = []
    for batch in _chunks(codes, 200):
        data = _request(
            "/api/a-share/prices/snapshot",
            {"thscodes": ",".join(batch)},
            ttl=TTL_SNAPSHOT,
        )
        output.extend(_items(data))
    return output


def snapshot_to_ticker(item: dict) -> Dict[str, Any]:
    """Normalize a snapshot item to the project ticker contract."""
    last = _as_float(item.get("last_price"))
    prev = _as_float(item.get("prev_price"))
    change = _as_float(item.get("price_change"))
    if change is None and last is not None and prev is not None:
        change = last - prev
    percent = _as_float(item.get("price_change_ratio_pct"))
    if percent is None and last is not None and prev:
        percent = (last - prev) / prev * 100.0
    return {
        "last": last or 0.0,
        "change": round(change, 4) if change is not None else 0.0,
        "changePercent": round(percent, 2) if percent is not None else 0.0,
        "high": _as_float(item.get("high_price")) or 0.0,
        "low": _as_float(item.get("low_price")) or 0.0,
        "open": _as_float(item.get("open_price")) or 0.0,
        "previousClose": prev or 0.0,
        "volume": _as_float(item.get("volume")) or 0.0,
        "turnover": _as_float(item.get("turnover")) or 0.0,
        "thscode": str(item.get("thscode") or ""),
        "symbol": to_project_symbol(str(item.get("thscode") or "")),
        "source": SOURCE,
    }


def _daily_ttl() -> int:
    now = shanghai_now()
    if now.weekday() < 5 and _MARKET_OPEN <= (now.hour, now.minute) <= _MARKET_CLOSE:
        return TTL_DAILY_INTRADAY
    return TTL_DAILY_CLOSED


_MAX_WINDOW_DAYS = 3600  # upstream caps one request at 10 years


def daily_bars(
    symbol: str,
    start: date,
    end: date,
    *,
    adjust: str = "none",
) -> List[dict]:
    """Daily bars (``interval=1d`` only). Windows >10y are sliced automatically.

    Returns raw rows with ``date`` plus unadjusted prices; ``time`` is added by
    :func:`daily_bars_to_klines`.
    """
    thscode = to_thscode(symbol)
    if not thscode or start > end:
        return []
    rows: List[dict] = []
    cursor = start
    while cursor <= end:
        stop = min(end, cursor + timedelta(days=_MAX_WINDOW_DAYS))
        data = _request(
            "/api/a-share/prices/historical",
            {
                "thscode": thscode,
                "interval": "1d",
                "start": date_to_ms(cursor),
                "end": date_to_ms(stop),
                "adjust": adjust,
            },
            ttl=_daily_ttl(),
        )
        rows.extend(_items(data))
        cursor = stop + timedelta(days=1)
    output: List[dict] = []
    for item in rows:
        day = ms_to_date(item.get("date_ms"))
        close = _as_float(item.get("close_price"))
        if day is None or close is None:
            continue
        output.append(
            {
                "date": day,
                "open": _as_float(item.get("open_price")) or 0.0,
                "high": _as_float(item.get("high_price")) or 0.0,
                "low": _as_float(item.get("low_price")) or 0.0,
                "close": close,
                "volume": _as_float(item.get("volume")) or 0.0,
                "turnover": _as_float(item.get("turnover")) or 0.0,
            }
        )
    output.sort(key=lambda row: row["date"])
    return output


def daily_bars_to_klines(bars: Sequence[dict]) -> List[Dict[str, Any]]:
    """Convert adjusted bars to the ``BaseDataSource`` K-line contract (UTC seconds)."""
    output: List[Dict[str, Any]] = []
    for bar in bars:
        day = bar.get("date")
        if not isinstance(day, date):
            continue
        output.append(
            {
                "time": int(
                    datetime(day.year, day.month, day.day, tzinfo=SHANGHAI_TZ).timestamp()
                ),
                "open": round(float(bar.get("open") or 0.0), 4),
                "high": round(float(bar.get("high") or 0.0), 4),
                "low": round(float(bar.get("low") or 0.0), 4),
                "close": round(float(bar.get("close") or 0.0), 4),
                "volume": round(float(bar.get("volume") or 0.0), 2),
            }
        )
    output.sort(key=lambda row: row["time"])
    return output


def adjustment_factors(
    symbol: str,
    *,
    start: Optional[date] = None,
    end: Optional[date] = None,
) -> List[dict]:
    """Corporate-action event stream, ascending by ex-date."""
    thscode = to_thscode(symbol)
    if not thscode:
        return []
    params: Dict[str, Any] = {"thscode": thscode}
    if start:
        params["from"] = start.isoformat()
    if end:
        params["to"] = end.isoformat()
    data = _request(
        "/api/a-share/corporate-actions/adjustment-factors",
        params,
        ttl=TTL_FINANCIAL,
    )
    rows: List[dict] = []
    for item in _items(data):
        ex_date = ms_to_date(item.get("ex_date_ms"))
        if ex_date is None:
            continue
        rows.append(
            {
                "ex_date": ex_date,
                "dividend_per_share": _as_float(item.get("dividend_per_share")) or 0.0,
                "per_share_bonus": _as_float(item.get("per_share_bonus")) or 0.0,
            }
        )
    rows.sort(key=lambda row: row["ex_date"])
    return rows


# ---------------------------------------------------------------------------
# Additive (difference) forward adjustment
# ---------------------------------------------------------------------------

def forward_additive_offset(
    bars: Sequence[dict],
    events: Sequence[dict],
) -> Dict[date, float]:
    """Per-date subtraction offset for 差值前复权 (Eastmoney/THS software convention).

    For every ex-date ``e`` after date ``d`` the raw price is reduced by
    ``cash_dividend + prev_close_before_e * per_share_bonus``. Using the
    pre-ex-date close keeps both the dividend and the bonus additive, which is
    what the reference charts publish.

    ponytail: dividends are treated as additive constants (duty-free, no
    re-investment compounding). A strategy that needs ratio-based adjustment
    wants ``adjust=forward|backward`` upstream or a different formula.
    """
    closes = [
        (bar["date"], float(bar.get("close") or 0.0))
        for bar in bars
        if isinstance(bar.get("date"), date)
    ]
    if not closes:
        return {}
    last_date = closes[-1][0]
    impacts: List[tuple] = []
    for event in events:
        ex_date = event.get("ex_date")
        if not isinstance(ex_date, date) or ex_date > last_date:
            continue
        dividend = float(event.get("dividend_per_share") or 0.0)
        bonus = float(event.get("per_share_bonus") or 0.0)
        if dividend == 0.0 and bonus == 0.0:
            continue
        previous_close = None
        for day, close in closes:
            if day < ex_date:
                previous_close = close
            else:
                break
        if previous_close is None:
            continue
        impacts.append((ex_date, dividend + previous_close * bonus))
    offsets: Dict[date, float] = {}
    for day, _close in closes:
        total = 0.0
        for ex_date, impact in impacts:
            if ex_date > day:
                total += impact
        offsets[day] = round(total, 4)
    return offsets


def apply_forward_additive(bars: Sequence[dict], events: Sequence[dict]) -> List[dict]:
    """Return bars with additive forward-adjusted prices."""
    offsets = forward_additive_offset(bars, events)
    if not offsets:
        return list(bars)
    adjusted: List[dict] = []
    for bar in bars:
        offset = offsets.get(bar.get("date"), 0.0)
        row = dict(bar)
        for field in ("open", "high", "low", "close"):
            value = bar.get(field)
            row[field] = round(float(value) - offset, 4) if value is not None else value
        adjusted.append(row)
    return adjusted


# Padding applied before the requested window so an ex-date inside the window
# still finds its previous close (needed by the additive formula).
_ADDITIVE_PAD_DAYS = 60


def _window_start(start: date, end: date, events: Sequence[dict]) -> date:
    """Earliest date whose additive offset can still be non-zero.

    Without this the pad could start after an ex-date, silently dropping the
    dividend (and with it the whole price level).
    """
    start_ms = date_to_ms(start)
    eligible = [
        event["ex_date"]
        for event in events
        if isinstance(event.get("ex_date"), date)
        and date_to_ms(event["ex_date"]) > start_ms
        and event["ex_date"] <= end
    ]
    if not eligible:
        return start
    return min(start, min(eligible) - timedelta(days=_ADDITIVE_PAD_DAYS))


def adjusted_daily_bars(symbol: str, start: date, end: date, *, adjust: str = "forward_additive") -> List[dict]:
    """Daily bars honoring ``none`` / ``forward`` / ``backward`` / ``forward_additive``."""
    mode = str(adjust or "none").strip().lower()
    if mode in {"forward_additive", "additive", "qfq_additive"}:
        events = adjustment_factors(symbol, end=end)
        fetch_from = _window_start(start, end, events)
        padded = daily_bars(symbol, fetch_from, end, adjust="none")
        adjusted = apply_forward_additive(padded, events)
        return [bar for bar in adjusted if start <= bar["date"] <= end]
    return daily_bars(symbol, start, end, adjust=mode)


def aggregate_weekly(klines: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Aggregate daily K-lines into weekly bars (bucket time = last trading day)."""
    buckets: Dict[str, Dict[str, Any]] = {}
    for bar in klines:
        moment = datetime.fromtimestamp(int(bar["time"]), tz=timezone.utc) + timedelta(hours=8)
        key = moment.strftime("%G-W%V")
        current = buckets.get(key)
        if current is None:
            buckets[key] = {
                "time": int(bar["time"]),
                "open": float(bar["open"]),
                "high": float(bar["high"]),
                "low": float(bar["low"]),
                "close": float(bar["close"]),
                "volume": float(bar["volume"]),
            }
            continue
        current["time"] = int(bar["time"])
        current["high"] = max(current["high"], float(bar["high"]))
        current["low"] = min(current["low"], float(bar["low"]))
        current["close"] = float(bar["close"])
        current["volume"] += float(bar["volume"])
    output = sorted(buckets.values(), key=lambda row: row["time"])
    for row in output:
        row["volume"] = round(row["volume"], 2)
    return output


# ---------------------------------------------------------------------------
# Valuations / financials
# ---------------------------------------------------------------------------

def valuations(thscodes: Iterable[str]) -> Dict[str, dict]:
    """Valuation snapshot keyed by project symbol.

    The endpoint rejects a whole batch when any thscode is unknown (code=3001,
    e.g. delisted names), so a failing batch is split in half until the bad code
    is isolated. Without this, one stale universe member blanks valuation for the
    other 99 symbols sharing its batch.
    """
    codes = [to_thscode(item) for item in thscodes]
    codes = [code for code in codes if code]
    output: Dict[str, dict] = {}
    for batch in _chunks(codes, 100):
        for item in _valuation_batch(batch):
            symbol = to_project_symbol(str(item.get("thscode") or ""))
            if symbol:
                output[symbol] = item
    return output


def _valuation_batch(batch: List[str]) -> List[dict]:
    """Fetch one batch, bisecting on the provider's whole-batch rejection."""
    if not batch:
        return []
    try:
        data = _request(
            "/api/a-share/valuations/snapshot",
            {"thscodes": ",".join(batch)},
            ttl=TTL_VALUATION,
        )
    except (HiThinkParamError, HiThinkError) as exc:
        # Unknown thscodes surface as code=3001 (generic HiThinkError), while
        # malformed requests use code=1002 (HiThinkParamError). Both mean "this
        # batch is not answerable as-is", so isolate the offender. Rate limiting
        # and provider outages must propagate instead of being bisected.
        if isinstance(exc, (HiThinkRateLimited, HiThinkUnavailable, HiThinkUnauthorized, HiThinkForbidden)):
            raise
        if len(batch) == 1:
            logger.debug("HiThink valuations: skipping unknown thscode %s", batch[0])
            return []
        middle = len(batch) // 2
        return _valuation_batch(batch[:middle]) + _valuation_batch(batch[middle:])
    return _items(data)


def _statements(path: str, symbol: str, *, period: str = "quarterly", limit: int = 8) -> List[dict]:
    thscode = to_thscode(symbol)
    if not thscode:
        return []
    data = _request(
        path,
        {"thscode": thscode, "period": period, "limit": max(1, min(20, int(limit)))},
        ttl=TTL_FINANCIAL,
    )
    return _items(data)


def income_statements(symbol: str, *, period: str = "quarterly", limit: int = 8) -> List[dict]:
    return _statements("/api/a-share/financials/income-statements", symbol, period=period, limit=limit)


def balance_sheets(symbol: str, *, period: str = "quarterly", limit: int = 8) -> List[dict]:
    return _statements("/api/a-share/financials/balance-sheets", symbol, period=period, limit=limit)


def cash_flow_statements(symbol: str, *, period: str = "quarterly", limit: int = 8) -> List[dict]:
    return _statements("/api/a-share/financials/cash-flow-statements", symbol, period=period, limit=limit)


def financial_indicators(symbol: str, report: str) -> Dict[str, Optional[float]]:
    """``report`` is ``yyyy-N`` (N = 1..4). Returns ``{index_id: value}``."""
    thscode = to_thscode(symbol)
    if not thscode or not report:
        return {}
    data = _request(
        "/api/a-share/financials/indicators",
        {"thscode": thscode, "report": str(report)},
        ttl=TTL_FINANCIAL,
    )
    output: Dict[str, Optional[float]] = {}
    abilities = data.get("abilities")
    if not isinstance(abilities, list):
        return output
    for block in abilities:
        if not isinstance(block, dict):
            continue
        for indicator in block.get("indicators") or []:
            if not isinstance(indicator, dict):
                continue
            index_id = str(indicator.get("index_id") or "").strip()
            if index_id:
                output[index_id] = _as_float(indicator.get("value"))
    return output


def report_period(period_end: date) -> str:
    """Map a period end date to the ``yyyy-N`` report token."""
    month = period_end.month
    quarter = 1 if month <= 3 else 2 if month <= 6 else 3 if month <= 9 else 4
    return f"{period_end.year}-{quarter}"


# ---------------------------------------------------------------------------
# Special data: limit pools, ladder, dragon-tiger, hot list, anomalies
# ---------------------------------------------------------------------------

def _paged(path: str, trade_date: Optional[date], params: Dict[str, Any], *, ttl: int, max_pages: int = 30) -> dict:
    query = dict(params)
    if trade_date is not None:
        query["date_ms"] = date_to_ms(trade_date)
    items: List[dict] = []
    pagination: Dict[str, Any] = {}
    page = 1
    while page <= max_pages:
        query["page"] = page
        data = _request(path, query, ttl=ttl)
        items.extend(_items(data))
        pagination = data.get("pagination") if isinstance(data.get("pagination"), dict) else {}
        pages = int(pagination.get("pages") or 1)
        if page >= pages:
            break
        page += 1
    return {"items": items, "pagination": pagination}


def _board_ttl(trade_date: Optional[date]) -> int:
    if trade_date is None:
        return TTL_BOARD_TODAY
    return TTL_BOARD_TODAY if trade_date >= shanghai_today() else TTL_BOARD_HISTORY


def limit_up_pool(
    trade_date: Optional[date] = None,
    *,
    sort_field: str = "continue_day_cnt",
    sort_dir: str = "desc",
) -> List[dict]:
    data = _paged(
        "/api/a-share/special-data/limit-up-pool",
        trade_date,
        {"size": 200, "sort_field": sort_field, "sort_dir": sort_dir},
        ttl=_board_ttl(trade_date),
    )
    return data["items"]


def limit_down_pool(trade_date: Optional[date] = None) -> List[dict]:
    data = _paged(
        "/api/a-share/special-data/limit-down-pool",
        trade_date,
        {"size": 200, "sort_field": "last_limit_time", "sort_dir": "desc"},
        ttl=_board_ttl(trade_date),
    )
    return data["items"]


def limit_break_pool(trade_date: Optional[date] = None) -> List[dict]:
    data = _paged(
        "/api/a-share/special-data/limit-break-pool",
        trade_date,
        {"size": 200, "sort_field": "open_times", "sort_dir": "desc"},
        ttl=_board_ttl(trade_date),
    )
    return data["items"]


def limit_up_ladder() -> dict:
    """Rolling 30-trading-day ladder matrix (no date parameter upstream)."""
    return _request("/api/a-share/special-data/limit-up-ladder", {}, ttl=TTL_LADDER)


def dragon_tiger(board_type: str = "all", trade_date: Optional[date] = None) -> dict:
    params: Dict[str, Any] = {"board_type": str(board_type or "all").strip().lower() or "all"}
    if trade_date is not None:
        params["date"] = trade_date.isoformat()
    return _request(
        "/api/a-share/special-data/dragon-tiger-list",
        params,
        ttl=_board_ttl(trade_date),
    )


def hot_stock_list(period: str = "day") -> List[dict]:
    data = _request(
        "/api/a-share/special-data/hot-stock-list",
        {"period": str(period or "day").strip().lower() or "day"},
        ttl=TTL_BOARD_TODAY,
    )
    return _items(data)


def skyrocket_list(period: str = "day") -> List[dict]:
    data = _request(
        "/api/a-share/special-data/skyrocket-list",
        {"period": str(period or "day").strip().lower() or "day"},
        ttl=TTL_BOARD_TODAY,
    )
    return _items(data)


def hot_list_history(day: date) -> List[dict]:
    data = _request(
        "/api/a-share/special-data/hot-stock-list-history",
        {"date": day.isoformat()},
        ttl=TTL_BOARD_HISTORY,
    )
    return _items(data)


def hot_rank_trend(symbol: str, start: date, end: date) -> List[dict]:
    thscode = to_thscode(symbol)
    if not thscode:
        return []
    data = _request(
        "/api/a-share/special-data/hot-stock-rank-trend",
        {"thscode": thscode, "start_date": start.isoformat(), "end_date": end.isoformat()},
        ttl=TTL_BOARD_HISTORY,
    )
    return _items(data)


def anomaly_list(tag_codes: Optional[Iterable[str]] = None) -> List[dict]:
    params: Dict[str, Any] = {}
    codes = [str(item).strip().upper() for item in (tag_codes or []) if str(item).strip()]
    if codes:
        params["tag_codes"] = ",".join(codes)
    data = _request("/api/a-share/special-data/anomaly-analysis-list", params, ttl=TTL_BOARD_TODAY)
    return _items(data)


def anomaly_by_stocks(thscodes: Iterable[str]) -> List[dict]:
    codes = [to_thscode(item) for item in thscodes]
    codes = [code for code in codes if code]
    if not codes:
        return []
    output: List[dict] = []
    for batch in _chunks(codes, 50):
        data = _request(
            "/api/a-share/special-data/anomaly-analysis-stock",
            {"thscodes": ",".join(batch)},
            ttl=TTL_BOARD_TODAY,
        )
        output.extend(_items(data))
    return output


def trading_days() -> List[date]:
    """Rolling one-year trading calendar, ascending."""
    data = _request("/api/a-share/calendar/trading-days", {}, ttl=TTL_CALENDAR)
    days: List[date] = []
    for item in _items(data):
        day = parse_trade_date(item.get("date")) or ms_to_date(item.get("date_ms"))
        if day is not None:
            days.append(day)
    return sorted(set(days))


def is_trading_day(day: date) -> bool:
    try:
        return day in set(trading_days())
    except HiThinkError as exc:
        logger.warning("trading calendar unavailable (%s); falling back to weekday check", exc)
        return day.weekday() < 5


def _as_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


__all__ = [
    "SOURCE",
    "SOURCE_NAME",
    "HiThinkError",
    "HiThinkNotConfigured",
    "HiThinkUnauthorized",
    "HiThinkForbidden",
    "HiThinkRateLimited",
    "HiThinkParamError",
    "HiThinkUnavailable",
    "enabled",
    "configured",
    "to_thscode",
    "to_project_symbol",
    "to_tencent_code",
    "parse_trade_date",
    "date_to_ms",
    "ms_to_date",
    "ms_to_utc_seconds",
    "shanghai_today",
    "snapshot",
    "snapshot_to_ticker",
    "daily_bars",
    "daily_bars_to_klines",
    "adjusted_daily_bars",
    "aggregate_weekly",
    "adjustment_factors",
    "apply_forward_additive",
    "forward_additive_offset",
    "valuations",
    "income_statements",
    "balance_sheets",
    "cash_flow_statements",
    "financial_indicators",
    "report_period",
    "limit_up_pool",
    "limit_down_pool",
    "limit_break_pool",
    "limit_up_ladder",
    "dragon_tiger",
    "hot_stock_list",
    "skyrocket_list",
    "hot_list_history",
    "hot_rank_trend",
    "anomaly_list",
    "anomaly_by_stocks",
    "trading_days",
    "is_trading_day",
]
