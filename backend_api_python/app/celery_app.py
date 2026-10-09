"""Celery application with lazy Flask application context integration."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# The ``celery`` console script starts with sys.path[0] set to the bin directory,
# not the project root, so ``from scripts... import`` (used by the sector-tag
# refresh and the public-universe snapshots) raised ModuleNotFoundError inside
# workers while working fine in the API process.  Put the backend root on the
# path explicitly: celery's own cwd_in_path() only adds it for the duration of
# an import, and it *removes* it again in a finally block -- so this must not be
# guarded on membership, or the removal would win and the fix would be a no-op.
_BACKEND_ROOT = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, _BACKEND_ROOT)

from celery import Celery, Task
from app.config.redis_urls import celery_broker_url, celery_result_backend_url


class FlaskContextTask(Task):
    abstract = True
    _flask_app = None

    def __call__(self, *args, **kwargs):
        if self._flask_app is None:
            os.environ["QD_PROCESS_ROLE"] = "celery"
            from app import create_app

            self._flask_app = create_app(register_http_routes=False)
        with self._flask_app.app_context():
            return self.run(*args, **kwargs)


celery_app = Celery("quantdinger", task_cls=FlaskContextTask)
celery_app.conf.update(
    broker_url=celery_broker_url(),
    result_backend=celery_result_backend_url(),
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone=os.getenv("TZ", "Asia/Shanghai"),
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=max(1, int(os.getenv("CELERY_WORKER_PREFETCH", "1"))),
    worker_max_tasks_per_child=max(1, int(os.getenv("CELERY_MAX_TASKS_PER_CHILD", "100"))),
    task_soft_time_limit=max(60, int(os.getenv("CELERY_TASK_SOFT_TIME_LIMIT", "3300"))),
    task_time_limit=max(120, int(os.getenv("CELERY_TASK_TIME_LIMIT", "3600"))),
    result_expires=max(3600, int(os.getenv("CELERY_RESULT_EXPIRES", "86400"))),
    broker_transport_options={
        "visibility_timeout": max(3600, int(os.getenv("CELERY_VISIBILITY_TIMEOUT", "7200"))),
    },
    imports=(
        "app.tasks.agent_jobs",
        "app.tasks.fast_analysis",
        "app.tasks.maintenance",
        "app.tasks.fundamental_sync",
        "app.tasks.hithink_events",
        "app.tasks.symbol_tags",
        "app.tasks.chip_sync",
        "app.tasks.ops_alert",
    ),
    task_routes={
        "quantdinger.tasks.fast_analysis": {"queue": "ai"},
        "quantdinger.tasks.agent_job": {"queue": "jobs"},
        "quantdinger.tasks.expire_agent_jobs": {"queue": "maintenance"},
        "quantdinger.tasks.reflection": {"queue": "maintenance"},
        "quantdinger.tasks.ai_calibration": {"queue": "maintenance"},
        "quantdinger.tasks.market_catalog_sync": {"queue": "maintenance"},
        "quantdinger.tasks.fundamental_sync_tick": {"queue": "maintenance"},
        "quantdinger.tasks.hithink_events_tick": {"queue": "maintenance"},
        "quantdinger.tasks.hithink_events_sync": {"queue": "maintenance"},
        "quantdinger.tasks.symbol_tag_tick": {"queue": "maintenance"},
        "quantdinger.tasks.symbol_tag_sync": {"queue": "maintenance"},
        "quantdinger.tasks.chip_sync_tick": {"queue": "maintenance"},
        "quantdinger.tasks.ops_watchdog_tick": {"queue": "maintenance"},
        "quantdinger.tasks.ops_test_alert": {"queue": "maintenance"},
        "quantdinger.tasks.worker_heartbeat": {"queue": "maintenance"},
        "quantdinger.tasks.cleanup_runtime_metadata": {"queue": "maintenance"},
    },
    beat_schedule={
        "fundamental-sync": {
            "task": "quantdinger.tasks.fundamental_sync_tick",
            "schedule": 60.0,
        },
        "hithink-events-sync": {
            "task": "quantdinger.tasks.hithink_events_tick",
            "schedule": max(300, int(os.getenv("HITHINK_EVENTS_SYNC_TICK_SEC", "600"))),
        },
        # One tick drives both families: sector constituents once after the
        # close, quote thresholds every SYMBOL_TAG_QUOTE_TICK_SEC while trading.
        "symbol-tag-sync": {
            "task": "quantdinger.tasks.symbol_tag_tick",
            "schedule": max(60, int(os.getenv("SYMBOL_TAG_QUOTE_TICK_SEC", "300"))),
        },
        "expire-billed-agent-jobs": {
            "task": "quantdinger.tasks.expire_agent_jobs",
            "schedule": 60.0,
        },
        "reflection-cycle": {
            "task": "quantdinger.tasks.reflection",
            "schedule": max(300, int(os.getenv("REFLECTION_WORKER_INTERVAL_SEC", "86400"))),
        },
        "ai-calibration-cycle": {
            "task": "quantdinger.tasks.ai_calibration",
            "schedule": max(3600, int(os.getenv("AI_CALIBRATION_INTERVAL_SEC", "86400"))),
        },
        "market-catalog-sync": {
            "task": "quantdinger.tasks.market_catalog_sync",
            "schedule": max(900, int(os.getenv("MARKET_CATALOG_SYNC_INTERVAL_SEC", "86400"))),
        },
        "celery-worker-heartbeat": {
            "task": "quantdinger.tasks.worker_heartbeat",
            "schedule": 10.0,
        },
        "runtime-metadata-cleanup": {
            "task": "quantdinger.tasks.cleanup_runtime_metadata",
            "schedule": 86400.0,
        },
        # Chip distribution only changes after the close; one pass a day is plenty.
        "chip-distribution-sync": {
            "task": "quantdinger.tasks.chip_sync_tick",
            "schedule": max(3600, int(os.getenv("CHIP_SYNC_INTERVAL_SEC", "86400"))),
        },
        # Collector healthcheck. Runs often; ops_alert dedupes so a persistent
        # problem reminds at most once per OPS_ALERT_REMIND_SEC.
        "ops-watchdog": {
            "task": "quantdinger.tasks.ops_watchdog_tick",
            "schedule": max(300, int(os.getenv("OPS_ALERT_TICK_SEC", "900"))),
        },
    },
)

__all__ = ["celery_app"]
