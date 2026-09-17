"""Idempotently expose the CNStock symbol master as an A-share universe."""

from __future__ import annotations

from app.utils.db import get_db_connection
from app.utils.logger import get_logger


logger = get_logger(__name__)

UNIVERSE_CODE = "cn_all"
UNIVERSE_NAME = "A-Shares"


def main() -> int:
    with get_db_connection() as db:
        cur = db.cursor()

        # Prefer the venue-qualified duplicate and remove legacy blank-venue rows.
        cur.execute(
            """
            DELETE FROM qd_market_symbols blank
            USING qd_market_symbols qualified
            WHERE blank.market = 'CNStock'
              AND qualified.market = 'CNStock'
              AND blank.symbol = qualified.symbol
              AND COALESCE(blank.exchange, '') = ''
              AND qualified.exchange = 'CN'
            """
        )
        removed_duplicates = cur.rowcount

        cur.execute(
            """
            UPDATE qd_market_symbols
            SET asset_class = 'equity',
                product_type = 'equity'
            WHERE market = 'CNStock'
              AND is_active = 1
              AND (asset_class <> 'equity' OR product_type <> 'equity')
            """
        )
        reclassified = cur.rowcount

        cur.execute(
            """
            INSERT INTO qd_universes
              (user_id, code, name, name_i18n_key, market, universe_type,
               source, source_ref, is_system, status, metadata_json)
            VALUES
              (NULL, %s, %s, %s, 'CNStock', 'market',
               'symbol_master', 'CNStock:all:equity', TRUE, 'active', %s::jsonb)
            ON CONFLICT (code) WHERE is_system = TRUE
            DO UPDATE SET
              user_id = NULL,
              name = EXCLUDED.name,
              name_i18n_key = EXCLUDED.name_i18n_key,
              market = EXCLUDED.market,
              universe_type = EXCLUDED.universe_type,
              source = EXCLUDED.source,
              source_ref = EXCLUDED.source_ref,
              status = EXCLUDED.status,
              metadata_json = EXCLUDED.metadata_json,
              updated_at = NOW()
            RETURNING id
            """,
            (
                UNIVERSE_CODE,
                UNIVERSE_NAME,
                "universe.catalog.cnAll",
                '{"description":"All active A-share symbols from the CNStock symbol master"}',
            ),
        )
        universe = cur.fetchone() or {}

        cur.execute(
            """
            SELECT COUNT(*) AS rows, COUNT(DISTINCT symbol) AS unique_symbols
            FROM qd_market_symbols
            WHERE market = 'CNStock'
              AND is_active = 1
              AND asset_class = 'equity'
            """
        )
        counts = cur.fetchone() or {}
        db.commit()
        cur.close()

    universe_id = int(universe.get("id") or 0)
    row_count = int(counts.get("rows") or 0)
    unique_symbols = int(counts.get("unique_symbols") or 0)
    logger.info(
        "CN universe bootstrap complete: universe_id=%s removed_duplicates=%s "
        "reclassified=%s rows=%s unique_symbols=%s",
        universe_id,
        removed_duplicates,
        reclassified,
        row_count,
        unique_symbols,
    )
    if not universe_id or unique_symbols < 5000 or row_count != unique_symbols:
        logger.error(
            "CN universe validation failed: universe_id=%s rows=%s unique_symbols=%s",
            universe_id,
            row_count,
            unique_symbols,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
