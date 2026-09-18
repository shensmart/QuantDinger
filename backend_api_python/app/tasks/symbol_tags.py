"""Celery entry points for the symbol-tag refresh."""

from __future__ import annotations

from app.celery_app import celery_app
from app.utils.logger import get_logger


logger = get_logger(__name__)


@celery_app.task(name="quantdinger.tasks.symbol_tag_tick")
def symbol_tag_tick():
    """Beat entry point: refresh whatever tag family is due right now."""
    from app.services.symbol_tag_schedule import due_categories
    from app.services.symbol_tags import get_symbol_tag_service

    wanted = due_categories()
    if not wanted:
        return {"skipped": "not_due"}
    report = get_symbol_tag_service().refresh(categories=wanted)
    logger.info("Symbol tag tick %s -> %s", wanted, report.get("quote") or report.get("sectors"))
    return report


@celery_app.task(name="quantdinger.tasks.symbol_tag_sync")
def symbol_tag_sync(categories=None):
    """Manual full refresh, also used by the admin sync endpoint."""
    from app.services.symbol_tags import (
        MATERIALIZED_CATEGORIES,
        get_symbol_tag_service,
    )

    report = get_symbol_tag_service().refresh(
        categories=categories or list(MATERIALIZED_CATEGORIES)
    )
    return report


__all__ = ["symbol_tag_sync", "symbol_tag_tick"]
