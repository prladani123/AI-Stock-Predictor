-- Frozen inputs are separate so a pending event cannot acquire later context.
CREATE TABLE market_event_impact_inputs (
    event_id text NOT NULL, study_version text NOT NULL, event_known_at timestamptz NOT NULL,
    pre_event_state jsonb NOT NULL, event_features jsonb NOT NULL, source_lineage jsonb NOT NULL,
    input_checksum text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(event_id,study_version)
);
CREATE TABLE market_event_impact_studies (
    event_id text NOT NULL, study_version text NOT NULL, event_known_at timestamptz NOT NULL,
    reference_minute timestamptz NOT NULL, reference_price double precision NOT NULL CHECK(reference_price>0),
    pre_event_state jsonb NOT NULL, event_features jsonb NOT NULL, reaction_summary jsonb NOT NULL,
    source_lineage jsonb NOT NULL, study_checksum text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(event_id,study_version),
    FOREIGN KEY(event_id,study_version) REFERENCES market_event_impact_inputs(event_id,study_version),
    CHECK(reference_minute>=event_known_at)
);
CREATE INDEX event_impact_known_time ON market_event_impact_studies(event_known_at,study_version);
CREATE TRIGGER immutable_event_impact_inputs BEFORE UPDATE OR DELETE ON market_event_impact_inputs
    FOR EACH ROW EXECUTE FUNCTION reject_big_move_v2_mutation();
CREATE TRIGGER immutable_event_impact_studies BEFORE UPDATE OR DELETE ON market_event_impact_studies
    FOR EACH ROW EXECUTE FUNCTION reject_big_move_v2_mutation();
INSERT INTO schema_migrations(version) VALUES ('020_event_impact_study') ON CONFLICT DO NOTHING;
