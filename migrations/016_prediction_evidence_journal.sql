-- Unversioned news payload edits must not be mistaken for earlier knowledge.
-- This visibility watermark does not change the live dashboard or retain outcomes.
ALTER TABLE market_news_events ADD COLUMN evidence_payload_seen_at timestamptz NOT NULL DEFAULT clock_timestamp();
CREATE FUNCTION stamp_news_evidence_payload() RETURNS trigger AS $$
BEGIN
    IF NEW.event_payload IS DISTINCT FROM OLD.event_payload THEN
        NEW.evidence_payload_seen_at=clock_timestamp();
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER news_evidence_payload_visibility BEFORE UPDATE OF event_payload ON market_news_events
FOR EACH ROW EXECUTE FUNCTION stamp_news_evidence_payload();

CREATE TABLE big_move_evidence_journal (
    journal_id text PRIMARY KEY,
    prediction_id text NOT NULL UNIQUE REFERENCES big_move_predictions(prediction_id),
    instrument_id bigint NOT NULL REFERENCES canonical_instruments(id),
    market_as_of timestamptz NOT NULL,
    decision_at timestamptz NOT NULL CHECK (decision_at >= market_as_of + interval '1 minute'),
    horizon_minutes smallint NOT NULL CHECK (horizon_minutes IN (10,20,30)),
    model_version text NOT NULL,
    move_model_checksum text NOT NULL REFERENCES big_move_model_definitions(model_checksum),
    direction_model_checksum text NOT NULL REFERENCES big_move_model_definitions(model_checksum),
    evaluation_checksum text REFERENCES big_move_evaluation_runs(evaluation_checksum),
    approval_checksum text REFERENCES big_move_approval_decisions(approval_checksum),
    approval_policy_version text,
    baseline_prediction jsonb NOT NULL,
    market_evidence jsonb NOT NULL,
    numerical_memory jsonb NOT NULL,
    news_memory jsonb NOT NULL,
    evidence_alignment jsonb NOT NULL,
    source_watermarks jsonb NOT NULL,
    journal_checksum text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX evidence_market_as_of ON big_move_evidence_journal(market_as_of DESC);
CREATE INDEX evidence_decision_at ON big_move_evidence_journal(decision_at DESC);
CREATE INDEX evidence_horizon_time ON big_move_evidence_journal(horizon_minutes,market_as_of DESC);
CREATE INDEX evidence_model_version ON big_move_evidence_journal(model_version);
CREATE TRIGGER immutable_big_move_evidence_journal BEFORE UPDATE OR DELETE ON big_move_evidence_journal
FOR EACH ROW EXECUTE FUNCTION reject_big_move_v2_mutation();

CREATE FUNCTION enforce_prediction_evidence_identity() RETURNS trigger AS $$
DECLARE p big_move_predictions%ROWTYPE;
BEGIN
    SELECT * INTO STRICT p FROM big_move_predictions WHERE prediction_id=NEW.prediction_id;
    IF p.as_of<>NEW.market_as_of OR p.instrument_id<>NEW.instrument_id
       OR p.horizon_minutes<>NEW.horizon_minutes OR p.model_version<>NEW.model_version
       OR p.move_model_checksum<>NEW.move_model_checksum OR p.direction_model_checksum<>NEW.direction_model_checksum
       OR p.quality_status<>'PASS' OR p.created_at<NEW.decision_at
       OR p.session_bias->'runtime_lineage'->>'generated_at' IS NULL
       OR (p.session_bias->'runtime_lineage'->>'generated_at')::timestamptz<>NEW.decision_at
       OR NEW.evaluation_checksum IS DISTINCT FROM p.session_bias->'runtime_lineage'->>'evaluation_checksum'
       OR NEW.approval_checksum IS DISTINCT FROM p.session_bias->'runtime_lineage'->>'approval_checksum'
    THEN RAISE EXCEPTION 'Evidence must match the original prediction decision'; END IF;
    IF (NEW.source_watermarks->>'selectedNewsEventFirstSeenAt')::timestamptz>NEW.decision_at
    THEN RAISE EXCEPTION 'News first seen after decision cannot enter evidence'; END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER prediction_evidence_identity BEFORE INSERT ON big_move_evidence_journal
FOR EACH ROW EXECUTE FUNCTION enforce_prediction_evidence_identity();
INSERT INTO schema_migrations(version) VALUES ('016_prediction_evidence_journal') ON CONFLICT DO NOTHING;
