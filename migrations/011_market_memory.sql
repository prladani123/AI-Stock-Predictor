CREATE TABLE IF NOT EXISTS market_memory_days (
    trading_date DATE PRIMARY KEY,
    session_type TEXT NOT NULL CHECK (session_type IN ('NORMAL','SPECIAL')),
    expiry_context TEXT NOT NULL CHECK (expiry_context IN ('WEEKLY_EXPIRY','MONTHLY_EXPIRY','NON_EXPIRY','UNKNOWN')),
    open_price NUMERIC, high_price NUMERIC, low_price NUMERIC, close_price NUMERIC,
    gap_percent NUMERIC, range_percent NUMERIC, realized_volatility NUMERIC,
    primary_regime TEXT NOT NULL, session_bias TEXT NOT NULL,
    futures_buildup TEXT NOT NULL, futures_premium NUMERIC, pcr_oi NUMERIC,
    ce_oi_concentration NUMERIC, pe_oi_concentration NUMERIC,
    strong_ce_strike NUMERIC, strong_pe_strike NUMERIC,
    atm_straddle_change NUMERIC, india_vix NUMERIC,
    important_event_count INTEGER NOT NULL DEFAULT 0,
    tags JSONB NOT NULL DEFAULT '[]', sources JSONB NOT NULL DEFAULT '[]',
    markdown_path TEXT NOT NULL, source_quality TEXT NOT NULL CHECK (source_quality IN ('COMPLETE','PARTIAL')),
    sources_count INTEGER NOT NULL DEFAULT 0, checksum TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_memory_days_regime ON market_memory_days(primary_regime);
CREATE INDEX IF NOT EXISTS idx_memory_days_expiry ON market_memory_days(expiry_context);
CREATE INDEX IF NOT EXISTS idx_memory_days_bias ON market_memory_days(session_bias);

CREATE TABLE IF NOT EXISTS market_memory_events (
    event_id TEXT PRIMARY KEY,
    session_date DATE NOT NULL REFERENCES market_memory_days(trading_date) ON DELETE CASCADE,
    known_at TIMESTAMPTZ, category TEXT NOT NULL, summary TEXT NOT NULL,
    relevance TEXT, source JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_memory_events_date ON market_memory_events(session_date);

CREATE TABLE IF NOT EXISTS market_memory_episodes (
    episode_id TEXT PRIMARY KEY,
    session_date DATE NOT NULL REFERENCES market_memory_days(trading_date) ON DELETE CASCADE,
    as_of TIMESTAMPTZ NOT NULL, state JSONB NOT NULL, outcome JSONB NOT NULL,
    regime TEXT NOT NULL, expiry_context TEXT NOT NULL, source_quality TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(session_date, as_of)
);
CREATE INDEX IF NOT EXISTS idx_memory_episodes_date ON market_memory_episodes(session_date);
CREATE INDEX IF NOT EXISTS idx_memory_episodes_as_of ON market_memory_episodes(as_of);
CREATE INDEX IF NOT EXISTS idx_memory_episodes_regime ON market_memory_episodes(regime);
CREATE INDEX IF NOT EXISTS idx_memory_episodes_expiry ON market_memory_episodes(expiry_context);
