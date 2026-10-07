-- Chip distribution (筹码分布) snapshots.
--
-- Eastmoney computes this client-side from daily OHLC + turnover, and its
-- push2his endpoints reject our egress, so we calculate it locally (see
-- app/services/chip_distribution.py) from sources that do work.
--
-- point-in-time discipline: `available_at` is the first moment the row could
-- have been known.  Backtests must filter on it, never on trade_date alone.

CREATE TABLE IF NOT EXISTS qd_chip_distribution (
    id BIGSERIAL PRIMARY KEY,
    market VARCHAR(32) NOT NULL,
    symbol VARCHAR(40) NOT NULL,
    trade_date DATE NOT NULL,
    -- First instant this snapshot was knowable (data published after close).
    available_at TIMESTAMPTZ NOT NULL,

    close_price DOUBLE PRECISION,
    -- Share of chips currently in profit, 0..1.
    benefit_ratio DOUBLE PRECISION,
    avg_cost DOUBLE PRECISION,

    cost90_low DOUBLE PRECISION,
    cost90_high DOUBLE PRECISION,
    concentration90 DOUBLE PRECISION,
    cost70_low DOUBLE PRECISION,
    cost70_high DOUBLE PRECISION,
    concentration70 DOUBLE PRECISION,

    -- Provenance so a bad source can be traced and recomputed.
    source VARCHAR(64) NOT NULL DEFAULT 'computed_sina',
    source_version VARCHAR(64) NOT NULL DEFAULT '',
    bars_used INTEGER NOT NULL DEFAULT 0,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Idempotent upsert key: one row per symbol/day/source.
CREATE UNIQUE INDEX IF NOT EXISTS qd_chip_distribution_uniq
    ON qd_chip_distribution (market, symbol, trade_date, source);

-- Point-in-time lookup for backtests.
CREATE INDEX IF NOT EXISTS idx_chip_distribution_pit
    ON qd_chip_distribution (market, symbol, available_at);

CREATE INDEX IF NOT EXISTS idx_chip_distribution_symbol_date
    ON qd_chip_distribution (market, symbol, trade_date DESC);

-- Batch jobs: which symbols still need fetching, resumable per symbol.
CREATE TABLE IF NOT EXISTS qd_chip_sync_jobs (
    id BIGSERIAL PRIMARY KEY,
    market VARCHAR(32) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'queued',
    symbols_total INTEGER NOT NULL DEFAULT 0,
    symbols_done INTEGER NOT NULL DEFAULT 0,
    symbols_failed INTEGER NOT NULL DEFAULT 0,
    lookback_days INTEGER NOT NULL DEFAULT 220,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS qd_chip_sync_items (
    id BIGSERIAL PRIMARY KEY,
    job_id BIGINT NOT NULL REFERENCES qd_chip_sync_jobs(id) ON DELETE CASCADE,
    market VARCHAR(32) NOT NULL,
    symbol VARCHAR(40) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    rows_written INTEGER NOT NULL DEFAULT 0,
    error VARCHAR(300) NOT NULL DEFAULT '',
    -- Earliest time this item may be retried.  Without it a failing symbol is
    -- retried on the very next claim and burns the whole batch budget.
    retry_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Added after the first release; keep upgrades for existing deployments working.
ALTER TABLE qd_chip_sync_items
    ADD COLUMN IF NOT EXISTS retry_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE UNIQUE INDEX IF NOT EXISTS qd_chip_sync_items_uniq
    ON qd_chip_sync_items (job_id, market, symbol);

CREATE INDEX IF NOT EXISTS idx_chip_sync_items_pending
    ON qd_chip_sync_items (job_id, status, retry_at);
