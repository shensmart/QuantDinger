"""Self-check for the three 2026-10-09 collector-outage fixes.

All three bugs shared one shape: the failure was silent, and the job kept
reporting success or ``running``.

1. ``scripts`` was unimportable in a celery worker, so the sector-tag refresh
   raised ModuleNotFoundError 77 times a day and industry/concept tags sat
   stale for 20 days.
2. Chip sync hit celery's time limit, which SIGKILLs the pool child: the item
   in flight stayed ``running`` forever (``claim_item`` only looks at
   ``pending``) and the self-rescheduled drain was lost with the process.
3. AkShare passes no timeout, so one stalled upstream holds a worker until
   that same time limit kills it.
4. ``_throttle_wait`` looped until a host's hourly budget freed a slot, with no
   ceiling -- so a batch parked at "processed: 1" for up to an hour and died the
   same way.  Sending anyway once a cap was hit was worse: it spends an already
   exhausted budget faster, which is what gets this egress blocked.
5. ``_int_env("SYMBOL_TAG_SECTOR_LIMIT", 0)`` returned 1, because that helper
   floors at 1.  The daily sector refresh therefore synced exactly one sector.
   Bug 1 had been masking it: the code never got far enough to be wrong.

Run:  python3 scripts/test_worker_resilience.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_ROOT)  # same bootstrap as test_akshare_throttle.py


def test_worker_can_import_scripts():
    """celery_app must leave the backend root on sys.path.

    The ``celery`` console script starts with sys.path[0] = the bin directory,
    and celery's ``cwd_in_path()`` inserts the cwd only for the duration of its
    own import, removing it again in a ``finally`` block.  So the fix must not
    be guarded on path membership: an earlier draft was, lost that race, and
    silently did nothing.

    Checked in a subprocess because sys.path is process-global; nothing here
    may depend on how this script itself was launched.
    """
    probe = (
        "import os, sys\n"
        "root = os.getcwd()\n"          # this subprocess runs with cwd=BACKEND_ROOT
        "sys.path[0] = '/usr/local/bin'\n"
        "cwd = os.getcwd()\n"
        "sys.path.insert(0, cwd)\n"          # celery's cwd_in_path() enters
        "import app.celery_app\n"
        "sys.path.remove(cwd)\n"             # celery's cwd_in_path() finally
        "assert root in sys.path, 'root dropped: %r' % (sys.path[:3],)\n"
        "import scripts.sync_ths_sector_tags\n"  # the import that used to fail
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout
    print("ok  worker keeps the backend root on sys.path")


def _install_probe_adapter():
    """Swap HTTPAdapter.send for a recorder.  Returns (seen, restore)."""
    from requests.adapters import HTTPAdapter

    seen: dict = {}
    original = HTTPAdapter.send

    def probe_send(self, request, **kwargs):
        seen["timeout"] = kwargs.get("timeout")

        class _Response:
            status_code = 200
            is_redirect = False
            content = b""
            headers: dict = {}
            raw = None
            history: list = []
            reason = "OK"
            encoding = "utf-8"

            def __init__(self):
                self.url = request.url

        return _Response()

    HTTPAdapter.send = probe_send
    return seen, lambda: setattr(HTTPAdapter, "send", original)


def test_akshare_host_gets_a_default_timeout():
    """A stalled upstream must surface as a timeout, not a SIGKILL."""
    from app.data_sources import rate_limiter
    from requests import Request, Session

    seen, restore = _install_probe_adapter()
    try:
        rate_limiter._throttle_installed = False  # re-install over the probe
        assert rate_limiter.install_http_throttle() is True
        Session().send(Request("GET", "https://push2his.eastmoney.com/api/x").prepare())
        assert seen["timeout"] == 30.0, seen
    finally:
        restore()
    print("ok  managed host gets timeout=%s" % seen["timeout"])


def test_unmanaged_host_gets_no_timeout():
    """install_http_throttle runs in every process role, including ones that
    make streaming LLM calls.  A blanket read timeout would turn this fix into
    a new bug, so the default stays scoped to the hosts the throttle owns."""
    from app.data_sources import rate_limiter
    from requests import Request, Session

    seen, restore = _install_probe_adapter()
    try:
        rate_limiter._throttle_installed = False
        assert rate_limiter.install_http_throttle() is True
        Session().send(Request("GET", "https://api.openai.com/v1/chat").prepare())
        assert seen["timeout"] is None, seen
    finally:
        restore()
    print("ok  unmanaged host gets no injected timeout")


# ---------------------------------------------------------------------------
# Chip sync — needs the live DB, so it builds and removes its own job.
# ---------------------------------------------------------------------------


def _run_chip_checks():
    from app.services import chip_sync

    job_id = chip_sync.start_job(
        "CNStock", symbols=["000001.SZ", "600000.SH", "000002.SZ"]
    )
    try:
        # A SIGKILLed worker leaves its item running and older than the
        # staleness window; it must come back as pending.
        chip_sync._query(
            "UPDATE qd_chip_sync_items SET status='running', "
            "updated_at = NOW() - INTERVAL '2 hours' "
            "WHERE job_id = %s AND symbol = '000002.SZ'",
            (job_id,),
        )
        assert chip_sync.reclaim_stale_items(job_id) == 1
        row = chip_sync._query(
            "SELECT status FROM qd_chip_sync_items "
            "WHERE job_id = %s AND symbol = '000002.SZ'",
            (job_id,),
        )
        assert chip_sync._row_get(row, "status", 0) == "pending", row
        print("ok  stale running item reclaimed -> pending")

        # A running item may be held by a healthy worker right now: leave it.
        chip_sync._query(
            "UPDATE qd_chip_sync_items SET status='running', updated_at = NOW() "
            "WHERE job_id = %s AND symbol = '600000.SH'",
            (job_id,),
        )
        assert chip_sync.reclaim_stale_items(job_id) == 0
        print("ok  fresh running item left alone")

        # Hitting celery's time limit loses the drain chain with the process,
        # so a long batch must end on its own terms.
        result = chip_sync.run_batch(job_id, 3, max_seconds=0.001)
        assert result["processed"] <= 1, result
        assert result["reclaimed"] == 0, result
        assert "elapsed_s" in result, result
        print("ok  batch deadline honoured (processed=%d)" % result["processed"])
    finally:
        chip_sync._query("DELETE FROM qd_chip_sync_jobs WHERE id = %s", (job_id,))


def test_throttle_wait_is_bounded_and_defers():
    """A full hourly budget must end the wait, not extend it forever.

    The old loop re-slept until the trailing-hour window freed a slot, which is
    how a chip batch sat at "processed: 1" until celery killed it.  The fix
    raises ThrottleBudgetExhausted so the caller can stop and resume later;
    sending the request anyway would be worse, because exhausting the budget is
    what gets this egress blocked.

    The budget is scaled down so saturation happens in seconds; the loop under
    test is unchanged.
    """
    from app.data_sources import rate_limiter

    saved = dict(rate_limiter._DEFAULT_THROTTLE)
    rate_limiter._DEFAULT_THROTTLE = {"sina.com.cn": (0.4, 2)}
    rate_limiter._throttle_states.clear()
    try:
        started = time.time()
        for _ in range(6):
            try:
                rate_limiter._throttle_wait("finance.sina.com.cn")
            except rate_limiter.ThrottleBudgetExhausted:
                break
        else:
            raise AssertionError("expected ThrottleBudgetExhausted")
        elapsed = time.time() - started
        cap = rate_limiter._throttle_max_wait()
        assert elapsed <= cap + 35.0, "waited %.0fs, cap is %.0fs" % (elapsed, cap)
    finally:
        rate_limiter._DEFAULT_THROTTLE = saved
        rate_limiter._throttle_states.clear()
    print("ok  throttle wait bounded, defers after %.0fs" % elapsed)


def test_deferred_item_keeps_its_retries():
    """A symbol abandoned for a full budget must not burn a retry.

    Nothing was attempted, so counting it would fail symbols permanently
    without ever calling upstream.
    """
    from app.services import chip_sync

    job_id = chip_sync.start_job("CNStock", symbols=["000001.SZ"])
    try:
        item = chip_sync.claim_item(job_id)
        assert item["attempts"] == 1
        chip_sync.release_item(item["id"], "deferred: test")
        row = chip_sync._query(
            "SELECT status, attempts FROM qd_chip_sync_items WHERE id = %s", (item["id"],)
        )
        assert chip_sync._row_get(row, "status", 0) == "pending", row
        assert chip_sync._row_get(row, "attempts", 1) == 0, row
    finally:
        chip_sync._query("DELETE FROM qd_chip_sync_jobs WHERE id = %s", (job_id,))
    print("ok  deferred item returns to pending without burning a retry")


def test_sector_limit_defaults_to_everything():
    """``SYMBOL_TAG_SECTOR_LIMIT`` unset must mean "all sectors".

    ``_int_env`` floors at 1, so passing it a 0 default silently produced a
    limit of 1 and the daily refresh synced one sector out of 320.
    """
    from app.services.symbol_tags import _int_env, _int_env_allow_zero

    assert _int_env("SYMBOL_TAG_SECTOR_LIMIT_UNSET_FOR_TEST", 0) == 1, (
        "the trap this test guards against has changed shape"
    )
    assert _int_env_allow_zero("SYMBOL_TAG_SECTOR_LIMIT_UNSET_FOR_TEST", 0) == 0
    assert (_int_env_allow_zero("SYMBOL_TAG_SECTOR_LIMIT_UNSET_FOR_TEST", 0) or None) is None
    print("ok  sector limit defaults to no limit, not 1")


if __name__ == "__main__":
    test_worker_can_import_scripts()
    test_akshare_host_gets_a_default_timeout()
    test_unmanaged_host_gets_no_timeout()
    _run_chip_checks()
    test_throttle_wait_is_bounded_and_defers()
    test_deferred_item_keeps_its_retries()
    test_sector_limit_defaults_to_everything()
    print("\nAll worker-resilience checks passed.")
