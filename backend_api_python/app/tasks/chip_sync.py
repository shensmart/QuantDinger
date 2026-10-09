"""Resumable chip-distribution (筹码分布) batch on the maintenance queue.

Each tick advances the active job by a bounded number of symbols so the worker
never blocks the queue for long.  Symbols are gated by the global HTTP throttle
installed in ``rate_limiter``; this task adds no sleeping of its own.
"""
import os

from app.celery_app import celery_app


def _batch_size() -> int:
    try:
        return max(1, int(os.getenv("CHIP_SYNC_BATCH_SIZE", "50")))
    except (TypeError, ValueError):
        return 50


def _defer_seconds() -> float:
    """How long to wait before retrying after a full upstream budget.

    The trailing-hour window is what refills, so a few minutes is enough to
    make progress again without hammering the host.
    """
    try:
        return max(30.0, float(os.getenv("CHIP_SYNC_DEFER_SEC", "300")))
    except (TypeError, ValueError):
        return 300.0


@celery_app.task(name="quantdinger.tasks.chip_sync_tick", soft_time_limit=600, time_limit=660)
def chip_sync_tick(job_id: int = 0):
    from app.data_sources.rate_limiter import install_http_throttle
    from app.services.chip_sync import job_status, run_batch, start_job

    install_http_throttle()

    target = int(job_id or 0)
    if not target:
        # Pick up an unfinished job, or start one covering the whole market.
        from app.services.chip_sync import _query, _row_get

        row = _query(
            "SELECT id FROM qd_chip_sync_jobs WHERE status IN ('queued','running') "
            "ORDER BY id LIMIT 1"
        )
        target = int(_row_get(row, "id", 0) or 0)
        if not target:
            market = os.getenv("CHIP_SYNC_MARKET", "CNStock")
            try:
                target = start_job(market)
            except ValueError:
                return {"skipped": "no symbols"}

    # Stop the batch well before the 600s soft limit: hitting it SIGKILLs the
    # worker, and the item in flight plus the self-rescheduled drain are lost.
    try:
        deadline = max(60.0, float(os.getenv("CHIP_SYNC_DEADLINE_SEC", "480")))
    except (TypeError, ValueError):
        deadline = 480.0

    result = run_batch(target, _batch_size(), max_seconds=deadline)
    result["job_id"] = target
    status = job_status(target) or {}
    result["job_status"] = status.get("status")
    result["symbols_done"] = status.get("symbols_done")
    result["symbols_total"] = status.get("symbols_total")

    # Keep draining while work remains.  A deferred batch means upstream's
    # budget is full, so resume after it refills instead of immediately -- a
    # countdown of 2s here would just re-wait and re-defer in a loop.
    if status.get("status") != "complete" and (
        result.get("processed") or result.get("deferred")
    ):
        countdown = _defer_seconds() if result.get("deferred") else 2
        chip_sync_tick.apply_async(kwargs={"job_id": target}, countdown=countdown)
        result["next_run_in_s"] = countdown
    return result
