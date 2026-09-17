-- HiThink (同花顺) special-data event stream and sync bookkeeping.
--
-- qd_market_events is a generic point-in-time event log: one row per
-- (market, symbol, event_type, trade_date, source). The unique key makes
-- re-runs idempotent; payload_json keeps the upstream fields verbatim so a
-- strategy only has to read the few aggregate columns it needs.
--
-- available_at is always the trade date's post-close instant so a backtest
-- cannot see a board before it was published.

CREATE TABLE IF NOT EXISTS qd_market_events (
    id BIGSERIAL PRIMARY KEY,
    market VARCHAR(32) NOT NULL,
    symbol VARCHAR(80) NOT NULL,
    event_type VARCHAR(40) NOT NULL,
    trade_date DATE NOT NULL,
    available_at TIMESTAMPTZ NOT NULL,
    name VARCHAR(120) NOT NULL DEFAULT '',
    rank INTEGER,
    score DOUBLE PRECISION,
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    source VARCHAR(40) NOT NULL DEFAULT 'hithink_finance',
    source_version VARCHAR(40) NOT NULL DEFAULT '',
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (market, symbol, event_type, trade_date, source)
);

CREATE INDEX IF NOT EXISTS idx_market_events_symbol_type_date
    ON qd_market_events(market, symbol, event_type, trade_date);
CREATE INDEX IF NOT EXISTS idx_market_events_type_date
    ON qd_market_events(event_type, trade_date DESC);
CREATE INDEX IF NOT EXISTS idx_market_events_available
    ON qd_market_events(market, event_type, available_at);

CREATE TABLE IF NOT EXISTS qd_market_event_sync_runs (
    id BIGSERIAL PRIMARY KEY,
    trigger_type VARCHAR(20) NOT NULL DEFAULT 'manual',
    source VARCHAR(40) NOT NULL DEFAULT 'hithink_finance',
    status VARCHAR(20) NOT NULL DEFAULT 'running',
    event_types JSONB NOT NULL DEFAULT '[]'::jsonb,
    trade_date_start DATE,
    trade_date_end DATE,
    rows_written INTEGER NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    detail_json JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_market_event_sync_runs_started
    ON qd_market_event_sync_runs(started_at DESC);
