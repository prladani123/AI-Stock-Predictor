CREATE TABLE banknifty_constituent_snapshots (
    snapshot_id text PRIMARY KEY, effective_from date NOT NULL, effective_to date NOT NULL,
    source text NOT NULL, source_url text, source_observed_at timestamptz NOT NULL,
    source_quality text NOT NULL CHECK(source_quality IN ('OFFICIAL_INDEX','TRACKING_PROXY','UNVERIFIED')),
    source_checksum text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
    CHECK(effective_to >= effective_from)
);
CREATE TABLE banknifty_constituent_members (
    snapshot_id text REFERENCES banknifty_constituent_snapshots(snapshot_id), symbol text NOT NULL,
    instrument_key text, isin text, weight numeric CHECK(weight BETWEEN 0 AND 1),
    weight_source_quality text NOT NULL CHECK(weight_source_quality IN ('OFFICIAL_INDEX','TRACKING_PROXY','NONE')),
    PRIMARY KEY(snapshot_id,symbol), CHECK(weight IS NULL OR weight_source_quality <> 'NONE')
);
CREATE TABLE market_structure_context_runs (
    run_id text PRIMARY KEY, date_from date NOT NULL, date_to date NOT NULL,
    context_version text NOT NULL, source_manifest jsonb NOT NULL,
    status text NOT NULL, checksum text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
    CHECK(date_to >= date_from)
);
CREATE TABLE market_structure_context_minutes (
    run_id text REFERENCES market_structure_context_runs(run_id), session_date date NOT NULL,
    as_of timestamptz NOT NULL, constituent_context jsonb NOT NULL, futures_context jsonb NOT NULL,
    market_structure jsonb NOT NULL, source_lineage jsonb NOT NULL, quality_status text NOT NULL,
    context_checksum text NOT NULL, PRIMARY KEY(run_id,as_of),
    CHECK((as_of AT TIME ZONE 'Asia/Kolkata')::date = session_date),
    CHECK(date_trunc('minute',as_of) = as_of)
);
CREATE INDEX market_structure_context_day ON market_structure_context_minutes(session_date,as_of);
CREATE TABLE runtime_research_observations (
    observation_id text PRIMARY KEY, instrument text NOT NULL, observed_at timestamptz NOT NULL,
    effective_at timestamptz, first_seen_at timestamptz NOT NULL, provider_latency double precision,
    source text NOT NULL, quality_status text NOT NULL, payload jsonb NOT NULL,
    CHECK(effective_at IS NULL OR effective_at >= observed_at)
);
CREATE INDEX runtime_research_cutoff ON runtime_research_observations(instrument,first_seen_at,effective_at);
CREATE TRIGGER immutable_constituent_snapshots BEFORE UPDATE OR DELETE ON banknifty_constituent_snapshots
    FOR EACH ROW EXECUTE FUNCTION reject_big_move_v2_mutation();
CREATE TRIGGER immutable_constituent_members BEFORE UPDATE OR DELETE ON banknifty_constituent_members
    FOR EACH ROW EXECUTE FUNCTION reject_big_move_v2_mutation();
CREATE TRIGGER immutable_structure_runs BEFORE UPDATE OR DELETE ON market_structure_context_runs
    FOR EACH ROW EXECUTE FUNCTION reject_big_move_v2_mutation();
CREATE TRIGGER immutable_structure_minutes BEFORE UPDATE OR DELETE ON market_structure_context_minutes
    FOR EACH ROW EXECUTE FUNCTION reject_big_move_v2_mutation();
CREATE TRIGGER immutable_runtime_research BEFORE UPDATE OR DELETE ON runtime_research_observations
    FOR EACH ROW EXECUTE FUNCTION reject_big_move_v2_mutation();
INSERT INTO schema_migrations(version) VALUES ('019_market_structure_context') ON CONFLICT DO NOTHING;
