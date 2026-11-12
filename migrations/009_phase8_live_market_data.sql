CREATE TABLE IF NOT EXISTS live_connection_events (
    event_key text PRIMARY KEY,
    payload jsonb NOT NULL,
    state text GENERATED ALWAYS AS (payload->>'state') STORED,
    occurred_at timestamptz GENERATED ALWAYS AS (market_iso_timestamp(payload->>'event_at')) STORED
);

CREATE TABLE IF NOT EXISTS live_subscription_generations (
    generation bigint PRIMARY KEY,
    payload jsonb NOT NULL,
    created_at timestamptz GENERATED ALWAYS AS (market_iso_timestamp(payload->>'created_at')) STORED
);

CREATE TABLE IF NOT EXISTS live_gaps (
    gap_id text PRIMARY KEY,
    payload jsonb NOT NULL,
    started_at timestamptz GENERATED ALWAYS AS (market_iso_timestamp(payload->>'started_at')) STORED,
    resolved_at timestamptz GENERATED ALWAYS AS (market_iso_timestamp(payload->>'resolved_at')) STORED,
    state text GENERATED ALWAYS AS (payload->>'state') STORED
);

CREATE TABLE IF NOT EXISTS live_raw_ticks (
    tick_id text PRIMARY KEY,
    payload jsonb NOT NULL,
    instrument_id bigint GENERATED ALWAYS AS ((payload->>'instrument_id')::bigint) STORED
      REFERENCES canonical_instruments(id) ON DELETE RESTRICT,
    instrument_token bigint GENERATED ALWAYS AS ((payload->>'instrument_token')::bigint) STORED,
    event_at timestamptz GENERATED ALWAYS AS (market_iso_timestamp(payload->>'event_at')) STORED,
    received_at timestamptz GENERATED ALWAYS AS (market_iso_timestamp(payload->>'received_at')) STORED,
    connection_id text GENERATED ALWAYS AS (payload->>'connection_id') STORED
);

CREATE TABLE IF NOT EXISTS live_option_universes (
    universe_id text PRIMARY KEY,
    payload jsonb NOT NULL,
    selected_at timestamptz GENERATED ALWAYS AS (market_iso_timestamp(payload->>'selected_at')) STORED,
    expiry date GENERATED ALWAYS AS (market_iso_date(payload->>'expiry')) STORED
);

CREATE TABLE IF NOT EXISTS live_futures_selections (
    selection_id text PRIMARY KEY,
    payload jsonb NOT NULL,
    selected_at timestamptz GENERATED ALWAYS AS (market_iso_timestamp(payload->>'selected_at')) STORED,
    instrument_id bigint GENERATED ALWAYS AS ((payload->>'instrument_id')::bigint) STORED
      REFERENCES canonical_instruments(id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS live_minutes_1m (
    minute_id text PRIMARY KEY,
    payload jsonb NOT NULL,
    instrument_id bigint GENERATED ALWAYS AS ((payload->>'instrument_id')::bigint) STORED
      REFERENCES canonical_instruments(id) ON DELETE RESTRICT,
    minute_at timestamptz GENERATED ALWAYS AS (market_iso_timestamp(payload->>'minute_at')) STORED,
    revision integer GENERATED ALWAYS AS ((payload->>'revision')::integer) STORED,
    UNIQUE(instrument_id, minute_at, revision)
);

CREATE TABLE IF NOT EXISTS live_option_snapshots_1m (
    snapshot_id text PRIMARY KEY,
    payload jsonb NOT NULL,
    instrument_id bigint GENERATED ALWAYS AS ((payload->>'instrument_id')::bigint) STORED
      REFERENCES canonical_instruments(id) ON DELETE RESTRICT,
    minute_at timestamptz GENERATED ALWAYS AS (market_iso_timestamp(payload->>'minute_at')) STORED,
    universe_id text GENERATED ALWAYS AS (payload->>'universe_id') STORED,
    UNIQUE(instrument_id, minute_at, universe_id)
);

CREATE TABLE IF NOT EXISTS live_quality_issues (
    issue_id text PRIMARY KEY,
    payload jsonb NOT NULL,
    detected_at timestamptz GENERATED ALWAYS AS (market_iso_timestamp(payload->>'detected_at')) STORED,
    code text GENERATED ALWAYS AS (payload->>'code') STORED
);

CREATE TABLE IF NOT EXISTS live_minute_corrections (
    correction_id text PRIMARY KEY,
    payload jsonb NOT NULL,
    instrument_id bigint GENERATED ALWAYS AS ((payload->>'instrument_id')::bigint) STORED
      REFERENCES canonical_instruments(id) ON DELETE RESTRICT,
    minute_at timestamptz GENERATED ALWAYS AS (market_iso_timestamp(payload->>'minute_at')) STORED,
    received_at timestamptz GENERATED ALWAYS AS (market_iso_timestamp(payload->>'received_at')) STORED
);

CREATE TABLE IF NOT EXISTS live_verified_ranges (
    range_id bigserial PRIMARY KEY,
    range_start timestamptz NOT NULL,
    range_end timestamptz NOT NULL,
    aggregate_checksum text NOT NULL,
    verified_at timestamptz NOT NULL DEFAULT now(),
    CHECK (range_end > range_start),
    UNIQUE(range_start, range_end, aggregate_checksum)
);

CREATE INDEX IF NOT EXISTS live_raw_ticks_instrument_event_idx
    ON live_raw_ticks(instrument_id, event_at);
CREATE INDEX IF NOT EXISTS live_raw_ticks_received_idx ON live_raw_ticks(received_at);
CREATE INDEX IF NOT EXISTS live_gaps_state_started_idx ON live_gaps(state, started_at);
CREATE INDEX IF NOT EXISTS live_minutes_instrument_time_idx ON live_minutes_1m(instrument_id, minute_at);
CREATE INDEX IF NOT EXISTS live_options_time_idx ON live_option_snapshots_1m(minute_at, instrument_id);
CREATE INDEX IF NOT EXISTS live_quality_time_idx ON live_quality_issues(detected_at, code);
CREATE INDEX IF NOT EXISTS live_corrections_time_idx ON live_minute_corrections(instrument_id, minute_at);

CREATE OR REPLACE FUNCTION reject_live_append_only_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Phase 8 market-data evidence is append-only';
END;
$$;

CREATE OR REPLACE FUNCTION permit_gap_resolution_only()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Phase 8 gap evidence cannot be deleted';
    END IF;
    IF OLD.payload->>'resolved_at' IS NULL
       AND NEW.payload->>'resolved_at' IS NOT NULL
       AND NEW.payload->>'state' = 'RESOLVED'
       AND (OLD.payload - 'resolved_at' - 'state' - 'affected_instruments' - 'affected_minutes' - 'reconnect_attempts')
           = (NEW.payload - 'resolved_at' - 'state' - 'affected_instruments' - 'affected_minutes' - 'reconnect_attempts') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'Only one-way gap resolution is allowed';
END;
$$;

DROP TRIGGER IF EXISTS immutable_live_connections ON live_connection_events;
CREATE TRIGGER immutable_live_connections BEFORE UPDATE OR DELETE ON live_connection_events
FOR EACH ROW EXECUTE FUNCTION reject_live_append_only_mutation();
DROP TRIGGER IF EXISTS immutable_live_subscriptions ON live_subscription_generations;
CREATE TRIGGER immutable_live_subscriptions BEFORE UPDATE OR DELETE ON live_subscription_generations
FOR EACH ROW EXECUTE FUNCTION reject_live_append_only_mutation();
DROP TRIGGER IF EXISTS resolve_live_gaps_only ON live_gaps;
CREATE TRIGGER resolve_live_gaps_only BEFORE UPDATE OR DELETE ON live_gaps
FOR EACH ROW EXECUTE FUNCTION permit_gap_resolution_only();
DROP TRIGGER IF EXISTS immutable_live_ticks ON live_raw_ticks;
CREATE OR REPLACE FUNCTION permit_verified_tick_retention_only()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' AND EXISTS (
      SELECT 1 FROM live_verified_ranges
      WHERE range_start <= OLD.received_at AND range_end > OLD.received_at
    ) THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'Raw ticks can only be deleted by verified retention';
END;
$$;
CREATE TRIGGER immutable_live_ticks BEFORE UPDATE OR DELETE ON live_raw_ticks
FOR EACH ROW EXECUTE FUNCTION permit_verified_tick_retention_only();
DROP TRIGGER IF EXISTS immutable_live_universes ON live_option_universes;
CREATE TRIGGER immutable_live_universes BEFORE UPDATE OR DELETE ON live_option_universes
FOR EACH ROW EXECUTE FUNCTION reject_live_append_only_mutation();
DROP TRIGGER IF EXISTS immutable_live_futures ON live_futures_selections;
CREATE TRIGGER immutable_live_futures BEFORE UPDATE OR DELETE ON live_futures_selections
FOR EACH ROW EXECUTE FUNCTION reject_live_append_only_mutation();
DROP TRIGGER IF EXISTS immutable_live_minutes ON live_minutes_1m;
CREATE TRIGGER immutable_live_minutes BEFORE UPDATE OR DELETE ON live_minutes_1m
FOR EACH ROW EXECUTE FUNCTION reject_live_append_only_mutation();
DROP TRIGGER IF EXISTS immutable_live_option_snapshots ON live_option_snapshots_1m;
CREATE TRIGGER immutable_live_option_snapshots BEFORE UPDATE OR DELETE ON live_option_snapshots_1m
FOR EACH ROW EXECUTE FUNCTION reject_live_append_only_mutation();
DROP TRIGGER IF EXISTS immutable_live_quality ON live_quality_issues;
CREATE TRIGGER immutable_live_quality BEFORE UPDATE OR DELETE ON live_quality_issues
FOR EACH ROW EXECUTE FUNCTION reject_live_append_only_mutation();
DROP TRIGGER IF EXISTS immutable_live_corrections ON live_minute_corrections;
CREATE TRIGGER immutable_live_corrections BEFORE UPDATE OR DELETE ON live_minute_corrections
FOR EACH ROW EXECUTE FUNCTION reject_live_append_only_mutation();
DROP TRIGGER IF EXISTS immutable_verified_ranges ON live_verified_ranges;
CREATE TRIGGER immutable_verified_ranges BEFORE UPDATE OR DELETE ON live_verified_ranges
FOR EACH ROW EXECUTE FUNCTION reject_live_append_only_mutation();

INSERT INTO schema_migrations(version) VALUES ('009_phase8_live_market_data')
ON CONFLICT (version) DO NOTHING;
