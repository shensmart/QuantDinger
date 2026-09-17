"""Daily HiThink special-data synchronization (limit pools, ladder, dragon-tiger, hot list)."""

from __future__ import annotations

import os

from app.celery_app import celery_app
from app.utils.logger import get_logger

logger = get_logger(__name__)


@celery_app.task(name="quantdinger.tasks.hithink_events_sync", soft_time_limit=1200, time_limit=1500)
def hithink_events_sync():
    from app.services import events_data
    from app.services.hithink_events_sync import run_sync

    return run_sync(
        event_types=events_data.HISTORICAL_EVENT_TYPES + ("limit_up_ladder", "anomaly", "skyrocket"),
        trigger_type="schedule",
    )


@celery_app.task(name="quantdinger.tasks.hithink_events_tick", soft_time_limit=120, time_limit=180)
def hithink_events_tick():
    """Cheap heartbeat that dispatches the real sync once the close has passed."""
    from app.services.hithink_events_sync import schedule_due

    return schedule_due()


__all__ = ["hithink_events_sync", "hithink_events_tick"]
