"""Self-check for the AkShare outbound HTTP throttle (no pytest fixtures).

Run:  python3 scripts/test_akshare_throttle.py
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.data_sources.rate_limiter import (  # noqa: E402
    _host_budget,
    install_http_throttle,
    throttle_settings,
    throttle_snapshot,
)


def test_host_budget_matching():
    assert _host_budget("push2his.eastmoney.com") is not None
    assert _host_budget("datacenter-web.eastmoney.com") is not None
    assert _host_budget("hq.sinajs.cn") is not None
    assert _host_budget("finance.sina.com.cn") is not None
    # Unknown hosts are never throttled (avoid breaking unrelated traffic).
    assert _host_budget("example.com") is None
    assert _host_budget("") is None
    # Must not match a lookalike suffix.
    assert _host_budget("noteastmoney.com") is None
    print("ok  host budget matching")


def test_toggle():
    os.environ["AKSHARE_THROTTLE_ENABLED"] = "false"
    assert throttle_settings()[0] is False
    os.environ["AKSHARE_THROTTLE_ENABLED"] = "true"
    assert throttle_settings()[0] is True
    os.environ.pop("AKSHARE_THROTTLE_ENABLED", None)
    assert throttle_settings()[0] is True
    print("ok  enable/disable toggle")


def test_install_is_idempotent_and_enforces_interval():
    from requests.adapters import HTTPAdapter

    assert install_http_throttle() is True
    assert install_http_throttle() is True, "second install must be a no-op"
    assert getattr(HTTPAdapter.send, "_qd_throttled", False) is True, "send must be patched"
    print("ok  install idempotent, HTTPAdapter.send patched")

    # The throttle wraps whatever send existed at install time, so exercise the
    # wait logic directly rather than swapping send out from under it.
    from app.data_sources.rate_limiter import _throttle_wait

    _throttle_wait("datacenter-web.eastmoney.com")  # prime the host
    start = time.time()
    slept = _throttle_wait("datacenter-web.eastmoney.com")
    elapsed = time.time() - start
    assert slept >= 1.0, "expected >=1s throttle, slept %.2fs" % slept
    assert elapsed >= 1.0, "expected >=1s wall clock, got %.2fs" % elapsed
    print("ok  interval enforced (%.2fs)" % elapsed)

    # Unlisted hosts pass through untouched.
    start = time.time()
    slept = _throttle_wait("example.com")
    assert slept == 0.0, "unlisted host must sleep 0, got %.2f" % slept
    assert time.time() - start < 0.5, "unlisted host must not be throttled"
    print("ok  unlisted host not throttled")

    # Disabling lifts the wait entirely.
    os.environ["AKSHARE_THROTTLE_ENABLED"] = "false"
    try:
        _throttle_wait("datacenter-web.eastmoney.com")
        start = time.time()
        assert _throttle_wait("datacenter-web.eastmoney.com") == 0.0
        assert time.time() - start < 0.5
        print("ok  disabled throttle sleeps 0")
    finally:
        os.environ.pop("AKSHARE_THROTTLE_ENABLED", None)


def test_snapshot_shape():
    snap = throttle_snapshot()
    assert isinstance(snap, dict)
    for host, info in snap.items():
        assert "last_ago_s" in info and "last_hour" in info
    print("ok  snapshot shape (%d hosts)" % len(snap))


if __name__ == "__main__":
    test_host_budget_matching()
    test_toggle()
    test_install_is_idempotent_and_enforces_interval()
    test_snapshot_shape()
    print("\nAll rate-limiter checks passed.")
