-- Symbol tag system: sector/concept membership moved out of universes, plus
-- derived event and quote-threshold tags.
--
-- Why: the THS industry/concept snapshot created 710 system universes whose
-- only real content was a membership list. A universe is a *strategy input*
-- (point-in-time, ordered, backtestable); a tag is a *label* (screening,
-- UI annotations, ad-hoc membership). Keeping both meant every sector showed
-- up in the universe picker and every pool needed its own readiness metadata.
--
-- qd_symbol_tag_members is a whole-table snapshot per tag (unique on
-- (tag_id, market, symbol)) because the upstream constituent endpoint is
-- present-day only. Event tags are NOT materialized: qd_market_events is
-- already point-in-time (available_at), so they resolve by query.
--
-- ponytail: this migration is destructive (universes are physically removed
-- and there is no rollback script, per the operator's explicit choice).
-- Backups were taken as patches/backups/qd_*_20260918.sql before applying.

CREATE TABLE IF NOT EXISTS qd_symbol_tags (
    id SERIAL PRIMARY KEY,
    code VARCHAR(120) NOT NULL UNIQUE,
    name VARCHAR(160) NOT NULL DEFAULT '',
    category VARCHAR(24) NOT NULL,
    market VARCHAR(32) NOT NULL DEFAULT 'CNStock',
    source VARCHAR(40) NOT NULL DEFAULT 'hithink_finance',
    is_system BOOLEAN NOT NULL DEFAULT TRUE,
    status VARCHAR(24) NOT NULL DEFAULT 'active',
    sort_order INTEGER NOT NULL DEFAULT 0,
    point_in_time BOOLEAN NOT NULL DEFAULT FALSE,
    rule_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_symbol_tags_category
    ON qd_symbol_tags(category, sort_order, code);

CREATE TABLE IF NOT EXISTS qd_symbol_tag_members (
    id BIGSERIAL PRIMARY KEY,
    tag_id INTEGER NOT NULL REFERENCES qd_symbol_tags(id) ON DELETE CASCADE,
    market VARCHAR(32) NOT NULL,
    symbol VARCHAR(80) NOT NULL,
    name VARCHAR(160) NOT NULL DEFAULT '',
    as_of_date DATE NOT NULL,
    source_version VARCHAR(120) NOT NULL DEFAULT '',
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE (tag_id, market, symbol)
);

CREATE INDEX IF NOT EXISTS idx_symbol_tag_members_symbol
    ON qd_symbol_tag_members(market, symbol);
CREATE INDEX IF NOT EXISTS idx_symbol_tag_members_tag
    ON qd_symbol_tag_members(tag_id);

-- Latest full-market snapshot for the quote-threshold tags.
--
-- A 5500-symbol HiThink snapshot takes ~70s (the API caps at 120 req/min), so
-- evaluating a quote threshold straight from upstream would block a screener
-- request for over a minute. The refresh tick writes here and readers hit this
-- table instead; one row per symbol per day.
CREATE TABLE IF NOT EXISTS qd_symbol_quote_snapshots (
    market VARCHAR(32) NOT NULL,
    symbol VARCHAR(80) NOT NULL,
    name VARCHAR(160) NOT NULL DEFAULT '',
    as_of_date DATE NOT NULL,
    change_pct DOUBLE PRECISION,
    amount DOUBLE PRECISION,
    volume DOUBLE PRECISION,
    price DOUBLE PRECISION,
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    PRIMARY KEY (market, symbol, as_of_date)
);

CREATE INDEX IF NOT EXISTS idx_symbol_quote_snapshots_day
    ON qd_symbol_quote_snapshots(as_of_date DESC);

-- ---------------------------------------------------------------------------
-- 1. Sector universes -> industry/concept tags (same codes, so old references
--    stay greppable; they are no longer resolvable as universes).
-- ---------------------------------------------------------------------------
INSERT INTO qd_symbol_tags
  (code, name, category, market, source, is_system, status, sort_order,
   point_in_time, metadata_json)
SELECT
  u.code,
  u.name,
  CASE WHEN u.code LIKE 'cn_concept_%' THEN 'concept' ELSE 'industry' END,
  COALESCE(NULLIF(u.market, ''), 'CNStock'),
  'hithink_finance',
  TRUE,
  'active',
  0,
  FALSE,
  jsonb_build_object(
    'source', 'hithink_finance',
    'thscode', COALESCE(u.source_ref, ''),
    'snapshot_only', TRUE,
    'snapshot_as_of', COALESCE(u.metadata_json ->> 'snapshot_as_of', ''),
    'migrated_from_universe_id', u.id
  )
FROM qd_universes u
WHERE u.is_system
  AND u.source = 'hithink_finance'
  AND (u.code LIKE 'cn_industry_%' OR u.code LIKE 'cn_concept_%')
ON CONFLICT (code) DO NOTHING;

INSERT INTO qd_symbol_tag_members
  (tag_id, market, symbol, name, as_of_date, source_version, metadata_json)
SELECT
  t.id,
  m.market,
  m.symbol,
  m.name,
  COALESCE(NULLIF(u.metadata_json ->> 'snapshot_as_of', '')::date, CURRENT_DATE),
  COALESCE(m.source_version, ''),
  COALESCE(m.metadata_json, '{}'::jsonb)
FROM qd_universe_members m
JOIN qd_universes u ON u.id = m.universe_id
JOIN qd_symbol_tags t ON t.code = u.code
WHERE u.is_system
  AND u.source = 'hithink_finance'
  AND (u.code LIKE 'cn_industry_%' OR u.code LIKE 'cn_concept_%')
ON CONFLICT (tag_id, market, symbol) DO NOTHING;

-- Refuse to delete the source rows unless every member landed in a tag.
DO $$
DECLARE
  source_members INTEGER;
  tag_members INTEGER;
BEGIN
  SELECT COUNT(*) INTO source_members
  FROM qd_universe_members m
  JOIN qd_universes u ON u.id = m.universe_id
  WHERE u.is_system
    AND u.source = 'hithink_finance'
    AND (u.code LIKE 'cn_industry_%' OR u.code LIKE 'cn_concept_%');

  IF source_members = 0 THEN
    RETURN;  -- already migrated; re-running this component must be a no-op
  END IF;

  SELECT COUNT(*) INTO tag_members
  FROM qd_symbol_tag_members tm
  JOIN qd_symbol_tags t ON t.id = tm.tag_id
  WHERE t.category IN ('industry', 'concept');

  IF tag_members < source_members THEN
    RAISE EXCEPTION
      'symbol tag migration incomplete: % tag members < % source members',
      tag_members, source_members;
  END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 2. Event tags (point-in-time, resolved from qd_market_events, not stored).
--    rule_json.event_types maps the tag to qd_market_events.event_type.
-- ---------------------------------------------------------------------------
INSERT INTO qd_symbol_tags
  (code, name, category, market, source, is_system, status, sort_order,
   point_in_time, rule_json, metadata_json)
VALUES
  ('event_limit_up', '涨停池', 'event', 'CNStock', 'hithink_finance', TRUE, 'active', 100, TRUE,
   '{"event_types": ["limit_up"]}'::jsonb, '{"board": "limit_up"}'::jsonb),
  ('event_limit_down', '跌停池', 'event', 'CNStock', 'hithink_finance', TRUE, 'active', 101, TRUE,
   '{"event_types": ["limit_down"]}'::jsonb, '{"board": "limit_down"}'::jsonb),
  ('event_limit_break', '炸板池', 'event', 'CNStock', 'hithink_finance', TRUE, 'active', 102, TRUE,
   '{"event_types": ["limit_break"]}'::jsonb, '{"board": "limit_break"}'::jsonb),
  ('event_limit_up_ladder', '连板梯队', 'event', 'CNStock', 'hithink_finance', TRUE, 'active', 103, TRUE,
   '{"event_types": ["limit_up_ladder"]}'::jsonb, '{"board": "limit_up_ladder"}'::jsonb),
  ('event_dragon_tiger', '龙虎榜', 'event', 'CNStock', 'hithink_finance', TRUE, 'active', 104, TRUE,
   '{"event_types": ["dragon_tiger_all", "dragon_tiger_org", "dragon_tiger_hot_money"]}'::jsonb,
   '{"board": "dragon_tiger"}'::jsonb),
  ('event_hot_rank', '人气榜', 'event', 'CNStock', 'hithink_finance', TRUE, 'active', 105, TRUE,
   '{"event_types": ["hot_rank"]}'::jsonb, '{"board": "hot_rank"}'::jsonb),
  ('event_anomaly', '异动', 'event', 'CNStock', 'hithink_finance', TRUE, 'active', 106, TRUE,
   '{"event_types": ["anomaly", "skyrocket"]}'::jsonb, '{"board": "anomaly"}'::jsonb)
ON CONFLICT (code) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 3. Quote threshold tags (snapshot-only, refreshed from a full-market
--    HiThink snapshot + qd_fundamental_snapshots.shares_outstanding).
--    `data_required` means the metric has no data source yet; the screener
--    hides it and refresh() skips it instead of failing.
-- ---------------------------------------------------------------------------
INSERT INTO qd_symbol_tags
  (code, name, category, market, source, is_system, status, sort_order,
   point_in_time, rule_json, metadata_json)
VALUES
  ('quote_change_up', '涨幅达标', 'quote', 'CNStock', 'hithink_finance', TRUE, 'active', 200, FALSE,
   '{"metric": "change_pct", "op": "gte", "value": 9.8}'::jsonb, '{}'::jsonb),
  ('quote_change_down', '跌幅达标', 'quote', 'CNStock', 'hithink_finance', TRUE, 'active', 201, FALSE,
   '{"metric": "change_pct", "op": "lte", "value": -9.8}'::jsonb, '{}'::jsonb),
  ('quote_turnover_rate', '换手率达标', 'quote', 'CNStock', 'hithink_finance', TRUE, 'active', 202, FALSE,
   '{"metric": "turnover_rate", "op": "gte", "value": 10}'::jsonb, '{}'::jsonb),
  ('quote_amount', '成交额达标', 'quote', 'CNStock', 'hithink_finance', TRUE, 'active', 203, FALSE,
   '{"metric": "amount", "op": "gte", "value": 1000000000}'::jsonb, '{}'::jsonb),
  ('quote_volume_ratio', '量比达标', 'quote', 'CNStock', 'hithink_finance', TRUE, 'data_required', 204, FALSE,
   '{"metric": "volume_ratio", "op": "gte", "value": 2}'::jsonb,
   '{"data_gap": "needs 5-day average volume per symbol"}'::jsonb)
ON CONFLICT (code) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 4. Drop the superseded universes. Members go with them via ON DELETE
--    CASCADE; qd_portfolio_rebalance_plans.universe_id becomes NULL.
--    Event pools (hithink_*) are included: event_* tags replace them, and
--    hithink_events_sync no longer materializes universes.
-- ---------------------------------------------------------------------------
DELETE FROM qd_universes
WHERE is_system
  AND source = 'hithink_finance'
  AND (code LIKE 'cn_industry_%' OR code LIKE 'cn_concept_%' OR code LIKE 'hithink_%');
