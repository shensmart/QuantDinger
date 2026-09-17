"""Idempotently seed the public marketplace with bundled QuantDinger assets."""

from __future__ import annotations

import json
from typing import Any

from app.services.community_service import CommunityService
from app.utils.db import get_db_connection
from app.utils.logger import get_logger


logger = get_logger(__name__)

BUILTIN_INDICATOR_NAME = "[Sample] SuperTrend Trend-Following"


def _as_json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    return {}


def _publish_builtin_indicator(user_id: int) -> int:
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            SELECT id
            FROM qd_indicator_codes
            WHERE user_id = ?
              AND name = ?
              AND COALESCE(asset_type, 'indicator') = 'indicator'
            ORDER BY id ASC
            LIMIT 1
            """,
            (user_id, BUILTIN_INDICATOR_NAME),
        )
        row = cur.fetchone()
        if not row:
            cur.close()
            logger.warning("Builtin indicator %r was not found", BUILTIN_INDICATOR_NAME)
            return 0

        indicator_id = int(row.get("id") if isinstance(row, dict) else row[0])
        cur.execute(
            """
            UPDATE qd_indicator_codes
            SET publish_to_community = 1,
                pricing_type = 'free',
                price = 0,
                is_encrypted = 0,
                vip_free = FALSE,
                review_status = 'approved',
                review_note = '',
                reviewed_at = COALESCE(reviewed_at, NOW()),
                reviewed_by = ?,
                updated_at = NOW()
            WHERE id = ?
            """,
            (user_id, indicator_id),
        )
        db.commit()
        cur.close()
    return indicator_id


def _publish_script_templates(user_id: int) -> tuple[int, int]:
    service = CommunityService()
    published = 0
    failed = 0

    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            SELECT id, template_key, title, description, code, param_schema
            FROM qd_script_templates
            WHERE is_active = TRUE
            ORDER BY sort_order ASC, id ASC
            """
        )
        templates = [dict(row) for row in (cur.fetchall() or [])]

        for template in templates:
            title = str(template.get("title") or "").strip()
            code = str(template.get("code") or "").strip()
            if not title or not code:
                continue

            cur.execute(
                """
                SELECT id
                FROM qd_indicator_codes
                WHERE user_id = ?
                  AND name = ?
                  AND COALESCE(asset_type, 'indicator') = 'script_template'
                ORDER BY CASE WHEN publish_to_community = 1 THEN 0 ELSE 1 END, id ASC
                LIMIT 1
                """,
                (user_id, title),
            )
            existing = cur.fetchone()
            existing_id = int(
                existing.get("id") if isinstance(existing, dict) else existing[0]
            ) if existing else 0

            ok, message, _ = service.publish_script_template_from_strategy(
                user_id=user_id,
                strategy_id=0,
                code=code,
                name=title,
                description=str(template.get("description") or ""),
                pricing_type="free",
                price=0,
                vip_free=False,
                code_hidden=False,
                is_admin=True,
                existing_indicator_id=existing_id,
                source_id=0,
                param_schema=_as_json_dict(template.get("param_schema")),
            )
            if ok:
                published += 1
            else:
                failed += 1
                logger.warning(
                    "Marketplace seed failed template=%s: %s",
                    template.get("template_key") or title,
                    message,
                )

        cur.close()

    return published, failed


def main() -> int:
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            SELECT id
            FROM qd_users
            WHERE role = 'admin'
            ORDER BY id ASC
            LIMIT 1
            """
        )
        row = cur.fetchone()
        cur.close()

    if not row:
        logger.error("No administrator account found; marketplace seed cannot continue")
        return 1

    user_id = int(row.get("id") if isinstance(row, dict) else row[0])
    indicator_id = _publish_builtin_indicator(user_id)
    published, failed = _publish_script_templates(user_id)
    logger.info(
        "Marketplace seed complete: user_id=%s indicator_id=%s templates=%s failed=%s",
        user_id,
        indicator_id,
        published,
        failed,
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
