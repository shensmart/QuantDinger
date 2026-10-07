# -*- coding: utf-8 -*-
"""Rate limiting and retry helpers for external market-data sources."""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from functools import wraps
from typing import Any, Callable, Dict, Optional, Tuple, Type

from app.utils.resource_guard import (
    is_fd_exhaustion,
    mark_fd_exhausted,
    ResourceExhaustedError,
)

logger = logging.getLogger(__name__)


def _is_too_many_open_files(exc: BaseException) -> bool:
    """Return True when an exception chain indicates process FD exhaustion."""
    return is_fd_exhaustion(exc)


USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]


def get_random_user_agent() -> str:
    """Return a random User-Agent."""
    return random.choice(USER_AGENTS)


def get_request_headers(referer: Optional[str] = None) -> dict:
    """Build request headers for external market-data calls."""
    headers = {
        "User-Agent": get_random_user_agent(),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "close",
    }
    if referer:
        headers["Referer"] = referer
    return headers


def random_sleep(
    min_seconds: float = 1.0,
    max_seconds: float = 3.0,
    log: bool = False,
) -> None:
    """Sleep for a random jitter interval."""
    sleep_time = random.uniform(min_seconds, max_seconds)
    if log:
        logger.debug("Sleeping for %.2fs before external request", sleep_time)
    time.sleep(sleep_time)


class RateLimiter:
    """Simple process-local rate limiter with jitter."""

    def __init__(
        self,
        min_interval: float = 1.0,
        jitter_min: float = 0.5,
        jitter_max: float = 1.5,
    ):
        self.min_interval = min_interval
        self.jitter_min = jitter_min
        self.jitter_max = jitter_max
        self._last_request_time: Optional[float] = None

    def wait(self) -> float:
        """Wait until the next request is allowed and return slept seconds."""
        wait_time = 0.0
        if self._last_request_time is not None:
            elapsed = time.time() - self._last_request_time
            if elapsed < self.min_interval:
                wait_time = self.min_interval - elapsed
                time.sleep(wait_time)

        jitter = random.uniform(self.jitter_min, self.jitter_max)
        time.sleep(jitter)
        wait_time += jitter
        self._last_request_time = time.time()
        return wait_time

    def reset(self) -> None:
        self._last_request_time = None


def retry_with_backoff(
    max_attempts: int = 3,
    base_delay: float = 2.0,
    max_delay: float = 30.0,
    exponential_base: float = 2.0,
    exceptions: Tuple[Type[Exception], ...] = (Exception,),
    on_retry: Optional[Callable[[int, Exception], None]] = None,
):
    """Retry a callable with exponential backoff.

    File-descriptor exhaustion is never retried because retries create more
    sockets and log writes, making the failure mode worse.
    """

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            last_exception: Optional[Exception] = None

            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    last_exception = exc

                    if _is_too_many_open_files(exc):
                        mark_fd_exhausted(exc)
                        logger.error(
                            "[retry] %s aborted: process file descriptors are exhausted: %s",
                            func.__name__,
                            exc,
                        )
                        raise ResourceExhaustedError(
                            f"{func.__name__} aborted: process file descriptors exhausted"
                        ) from exc

                    if attempt >= max_attempts:
                        logger.error(
                            "[retry] %s reached max attempts (%s); giving up",
                            func.__name__,
                            max_attempts,
                        )
                        raise

                    delay = min(
                        base_delay * (exponential_base ** (attempt - 1)),
                        max_delay,
                    )
                    delay *= random.uniform(0.8, 1.2)
                    logger.warning(
                        "[retry] %s failed %s/%s: %s; retrying in %.1fs",
                        func.__name__,
                        attempt,
                        max_attempts,
                        exc,
                        delay,
                    )

                    if on_retry:
                        on_retry(attempt, exc)

                    time.sleep(delay)

            if last_exception is not None:
                raise last_exception
            return None

        return wrapper

    return decorator


_eastmoney_limiter = RateLimiter(min_interval=2.0, jitter_min=1.0, jitter_max=3.0)
_tencent_limiter = RateLimiter(min_interval=1.0, jitter_min=0.5, jitter_max=1.5)
_akshare_limiter = RateLimiter(min_interval=2.0, jitter_min=1.5, jitter_max=3.5)


def get_eastmoney_limiter() -> RateLimiter:
    """Return the Eastmoney rate limiter."""
    return _eastmoney_limiter


def get_tencent_limiter() -> RateLimiter:
    """Return the Tencent rate limiter."""
    return _tencent_limiter


def get_akshare_limiter() -> RateLimiter:
    """Return the AkShare rate limiter."""
    return _akshare_limiter


# ---------------------------------------------------------------------------
# Domain-level throttle for every outbound HTTP call made by AkShare.
#
# AkShare has ~150 entry points and each one opens its own ``requests`` call,
# so wrapping individual call sites is both fragile and easy to miss.  Hooking
# ``HTTPAdapter.send`` throttles all of them from one place, keyed by host, and
# also normalises the browser-ish headers AkShare forgets to send.
#
# Rationale: Eastmoney's push2*/datacenter hosts close the connection without
# response (``RemoteDisconnected``) once a client exceeds their tolerance,
# which historically made whole sync runs fail permanently.  The intervals here
# are deliberately conservative; they are env-tunable so operators can relax
# them on an unfiltered egress without touching code.
# ---------------------------------------------------------------------------

# Requests per hour budget per host.  0 disables throttling for that host.
_DEFAULT_THROTTLE: Dict[str, Tuple[float, int]] = {
    "eastmoney.com": (1.2, 240),   # push2his / push2 / datacenter-web / data
    "sina.com.cn": (0.4, 900),     # hq.sinajs.cn / finance.sina.com.cn
    "sinajs.cn": (0.4, 900),
    "10jqka.com.cn": (1.0, 300),   # Tonghuashun mirror
    "xueqiu.com": (1.0, 300),
}

_THROTTLE_LOG_INTERVAL = 60.0
_throttle_lock = threading.Lock()
_throttle_states: Dict[str, dict] = {}
_throttle_installed = False


def _throttle_env_float(name: str, default: float) -> float:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return default
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return default


def throttle_settings() -> Tuple[bool, float]:
    """Return ``(enabled, scale)`` for the outbound AkShare throttle.

    ``AKSHARE_THROTTLE_ENABLED=false`` switches the throttle off.
    ``AKSHARE_THROTTLE_SCALE`` multiplies every interval (``2`` = twice as slow).
    """
    raw = str(os.getenv("AKSHARE_THROTTLE_ENABLED") or "true").strip().lower()
    enabled = raw not in {"0", "false", "no", "off"}
    scale = _throttle_env_float("AKSHARE_THROTTLE_SCALE", 1.0) or 1.0
    return enabled, scale


def _host_budget(host: str) -> Optional[Tuple[float, int]]:
    host = (host or "").lower()
    if not host:
        return None
    for suffix, budget in _DEFAULT_THROTTLE.items():
        if host == suffix or host.endswith("." + suffix):
            return budget
    return None


def _throttle_wait(host: str) -> float:
    """Sleep as required to respect this host's interval and hourly budget.

    Returns the slept duration.  Caller must hold no lock.
    """
    enabled, scale = throttle_settings()
    if not enabled:
        return 0.0
    budget = _host_budget(host)
    if budget is None:
        return 0.0
    min_interval, hourly = budget
    min_interval = min_interval * scale
    if min_interval <= 0 and hourly <= 0:
        return 0.0

    slept = 0.0
    while True:
        with _throttle_lock:
            now = time.time()
            state = _throttle_states.setdefault(host, {"last": None, "stamps": []})
            if min_interval > 0 and state["last"] is not None:
                gap = min_interval - (now - state["last"])
            else:
                gap = 0.0
            if gap > 0:
                wait = min(gap, 5.0)
            else:
                # Prune to the trailing hour and enforce the hourly budget.
                cutoff = now - 3600.0
                stamps = [s for s in state["stamps"] if s > cutoff]
                state["stamps"] = stamps
                if hourly > 0 and len(stamps) >= hourly:
                    wait = min(3600.0 - (now - stamps[0]) + 0.05, 30.0)
                    wait = max(wait, 0.05)
                else:
                    state["last"] = now
                    state["stamps"].append(now)
                    return slept
        time.sleep(wait)
        slept += wait


def install_http_throttle() -> bool:
    """Throttle every ``requests`` call by target host.  Idempotent.

    Must be called before AkShare traffic starts.  Safe to call from each
    process role; each process installs its own patch.
    """
    global _throttle_installed
    with _throttle_lock:
        if _throttle_installed:
            return True

    try:
        from requests.adapters import HTTPAdapter
    except Exception as exc:  # pragma: no cover - requests always present
        logger.warning("HTTP throttle not installed (no requests): %s", exc)
        return False

    original_send = HTTPAdapter.send
    if getattr(original_send, "_qd_throttled", False):
        with _throttle_lock:
            _throttle_installed = True
        return True

    last_log = {"at": 0.0}

    def throttled_send(self, request, **kwargs):
        host = ""
        try:
            host = request.url.split("//", 1)[-1].split("/", 1)[0].split("@")[-1].split(":")[0]
        except Exception:
            host = ""
        slept = _throttle_wait(host)
        if slept >= 1.0:
            now = time.time()
            if now - last_log["at"] >= _THROTTLE_LOG_INTERVAL:
                last_log["at"] = now
                logger.info("Throttled AkShare request to %s by %.1fs", host, slept)
        return original_send(self, request, **kwargs)

    throttled_send._qd_throttled = True  # type: ignore[attr-defined]
    HTTPAdapter.send = throttled_send  # type: ignore[assignment]
    with _throttle_lock:
        _throttle_installed = True
    enabled, scale = throttle_settings()
    logger.info("AkShare HTTP throttle installed (enabled=%s scale=%s)", enabled, scale)
    return True


def throttle_snapshot() -> Dict[str, Any]:
    """Return per-host throttle counters for diagnostics."""
    now = time.time()
    with _throttle_lock:
        return {
            host: {
                "last_ago_s": None if st["last"] is None else round(now - st["last"], 1),
                "last_hour": len([s for s in st["stamps"] if s > now - 3600.0]),
            }
            for host, st in _throttle_states.items()
        }
