"""Ops watchdog on the maintenance queue.

Scans recorded collector failures and pushes a digest to the configured IM
channel.  Cheap enough to run often; de-duplication lives in ops_alert so a
persistent problem does not spam the channel.
"""
import os

from app.celery_app import celery_app


@celery_app.task(name="quantdinger.tasks.ops_watchdog_tick", soft_time_limit=120, time_limit=150)
def ops_watchdog_tick():
    from app.services.ops_alert import alerts_configured, run_watchdog

    if not alerts_configured():
        # Nothing to send to; skip the DB scan entirely.
        return {"skipped": "no_channels"}

    try:
        window = int(os.getenv("OPS_ALERT_WINDOW_HOURS", "24"))
    except (TypeError, ValueError):
        window = 24

    return run_watchdog(window_hours=window)


@celery_app.task(name="quantdinger.tasks.ops_test_alert", soft_time_limit=60, time_limit=90)
def ops_test_alert():
    """Send a one-off message so an operator can verify channel wiring."""
    from app.services.ops_alert import alerts_configured, deliver

    if not alerts_configured():
        return {"sent": False, "reason": "no_channels_configured"}

    ok, detail = deliver(
        "QuantDinger 告警通道测试",
        "这是一条测试消息。如果你看到它，说明采集告警通道已配置正确。",
    )
    return {"sent": ok, "delivery": detail}
