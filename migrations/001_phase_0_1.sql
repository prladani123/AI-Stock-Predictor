CREATE TABLE IF NOT EXISTS schema_migrations (
    version text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS trading_sessions (
    session_date date PRIMARY KEY,
    market_open_utc timestamptz NOT NULL,
    market_close_utc timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('OPEN', 'CLOSED', 'SPECIAL')),
    source text NOT NULL DEFAULT 'configured',
    CHECK (market_close_utc > market_open_utc)
);

CREATE TABLE IF NOT EXISTS instrument_snapshots (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    snapshot_date date NOT NULL,
    captured_at timestamptz NOT NULL,
    source_sha256 char(64) NOT NULL UNIQUE,
    record_count integer NOT NULL CHECK (record_count >= 0),
    raw_payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS canonical_instruments (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    snapshot_id bigint NOT NULL REFERENCES instrument_snapshots(id) ON DELETE RESTRICT,
    instrument_token bigint NOT NULL,
    exchange text NOT NULL,
    tradingsymbol text NOT NULL,
    name text NOT NULL,
    segment text NOT NULL,
    instrument_type text NOT NULL,
    expiry date,
    tick_size numeric(18,8) NOT NULL CHECK (tick_size > 0),
    lot_size integer NOT NULL CHECK (lot_size > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (snapshot_id, instrument_token),
    UNIQUE (snapshot_id, exchange, tradingsymbol)
);

CREATE INDEX IF NOT EXISTS canonical_instruments_lookup_idx
    ON canonical_instruments (exchange, tradingsymbol, snapshot_id);

CREATE TABLE IF NOT EXISTS historical_import_jobs (
    id uuid PRIMARY KEY,
    instrument_id bigint NOT NULL REFERENCES canonical_instruments(id) ON DELETE RESTRICT,
    requested_from timestamptz NOT NULL,
    requested_to timestamptz NOT NULL,
    checkpoint_at timestamptz,
    status text NOT NULL CHECK (status IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    rows_received integer NOT NULL DEFAULT 0 CHECK (rows_received >= 0),
    rows_inserted integer NOT NULL DEFAULT 0 CHECK (rows_inserted >= 0),
    error_code text,
    error_message text,
    started_at timestamptz,
    completed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (requested_to >= requested_from),
    UNIQUE (instrument_id, requested_from, requested_to)
);

CREATE TABLE IF NOT EXISTS one_minute_candles (
    instrument_id bigint NOT NULL REFERENCES canonical_instruments(id) ON DELETE RESTRICT,
    candle_at timestamptz NOT NULL,
    open numeric(18,8) NOT NULL CHECK (open > 0),
    high numeric(18,8) NOT NULL CHECK (high > 0),
    low numeric(18,8) NOT NULL CHECK (low > 0),
    close numeric(18,8) NOT NULL CHECK (close > 0),
    volume bigint NOT NULL CHECK (volume >= 0),
    open_interest bigint CHECK (open_interest >= 0),
    source_job_id uuid NOT NULL REFERENCES historical_import_jobs(id) ON DELETE RESTRICT,
    payload_sha256 char(64) NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (instrument_id, candle_at),
    CHECK (high >= GREATEST(open, close, low)),
    CHECK (low <= LEAST(open, close, high))
);

CREATE INDEX IF NOT EXISTS one_minute_candles_time_idx
    ON one_minute_candles (candle_at);

CREATE TABLE IF NOT EXISTS quality_issues (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id uuid REFERENCES historical_import_jobs(id) ON DELETE RESTRICT,
    instrument_id bigint REFERENCES canonical_instruments(id) ON DELETE RESTRICT,
    session_date date,
    candle_at timestamptz,
    code text NOT NULL,
    severity text NOT NULL CHECK (severity IN ('INFO', 'WARNING', 'ERROR')),
    message text NOT NULL,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    fingerprint char(64) NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS quality_issues_session_idx
    ON quality_issues (instrument_id, session_date, code);

INSERT INTO schema_migrations(version) VALUES ('001_phase_0_1')
ON CONFLICT (version) DO NOTHING;
