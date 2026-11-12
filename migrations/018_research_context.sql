CREATE TABLE research_context_runs (
    run_id text PRIMARY KEY,
    provider text NOT NULL,
    date_from date NOT NULL,
    date_to date NOT NULL CHECK (date_to >= date_from),
    context_version text NOT NULL,
    configuration jsonb NOT NULL,
    source_manifest jsonb NOT NULL,
    status text NOT NULL,
    raw_checksum text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id,context_version)
);
CREATE TABLE research_context_minutes (
    run_id text NOT NULL REFERENCES research_context_runs(run_id),
    session_date date NOT NULL,
    as_of timestamptz NOT NULL,
    context_version text NOT NULL,
    index_context jsonb NOT NULL,
    options_context jsonb NOT NULL,
    volatility_context jsonb NOT NULL,
    global_context jsonb NOT NULL,
    source_lineage jsonb NOT NULL,
    quality_status text NOT NULL CHECK (quality_status IN ('PASS','PARTIAL','SOURCE_CONFLICT')),
    context_checksum text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id,as_of,context_version),
    FOREIGN KEY (run_id,context_version) REFERENCES research_context_runs(run_id,context_version),
    UNIQUE (run_id,context_checksum),
    CHECK ((as_of AT TIME ZONE 'Asia/Kolkata')::date = session_date),
    CHECK (date_trunc('minute',as_of) = as_of)
);
CREATE INDEX research_context_session ON research_context_minutes(session_date,context_version);
CREATE INDEX research_context_as_of ON research_context_minutes(as_of DESC,context_version);
CREATE INDEX research_context_version ON research_context_minutes(context_version,session_date,as_of);
CREATE TRIGGER immutable_research_context_runs BEFORE UPDATE OR DELETE ON research_context_runs
    FOR EACH ROW EXECUTE FUNCTION reject_big_move_v2_mutation();
CREATE TRIGGER immutable_research_context_minutes BEFORE UPDATE OR DELETE ON research_context_minutes
    FOR EACH ROW EXECUTE FUNCTION reject_big_move_v2_mutation();
INSERT INTO schema_migrations(version) VALUES ('018_research_context') ON CONFLICT DO NOTHING;
