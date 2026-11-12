-- Research-only append-only evidence; outcomes remain in the existing V2 table.
CREATE TABLE big_move_shadow_observations (
    shadow_id text PRIMARY KEY,
    prediction_id text NOT NULL UNIQUE REFERENCES big_move_predictions(prediction_id),
    journal_id text NOT NULL REFERENCES big_move_evidence_journal(journal_id),
    instrument_id bigint NOT NULL REFERENCES canonical_instruments(id),
    market_as_of timestamptz NOT NULL,
    decision_at timestamptz NOT NULL CHECK (decision_at >= market_as_of),
    horizon_minutes smallint NOT NULL CHECK (horizon_minutes IN (10,20,30)),
    context_v3_evidence jsonb NOT NULL,
    microstructure_evidence jsonb NOT NULL,
    greeks_evidence jsonb NOT NULL,
    source_watermarks jsonb NOT NULL,
    schema_versions jsonb NOT NULL,
    shadow_checksum text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX shadow_market_as_of ON big_move_shadow_observations(market_as_of DESC);
CREATE FUNCTION reject_shadow_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'IMMUTABLE_SHADOW_OBSERVATION';
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER immutable_shadow BEFORE UPDATE OR DELETE ON big_move_shadow_observations
FOR EACH ROW EXECUTE FUNCTION reject_shadow_mutation();
CREATE FUNCTION validate_shadow_identity() RETURNS trigger AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM big_move_predictions p JOIN big_move_evidence_journal j USING(prediction_id)
        WHERE p.prediction_id=NEW.prediction_id AND j.journal_id=NEW.journal_id
          AND p.instrument_id=NEW.instrument_id AND p.horizon_minutes=NEW.horizon_minutes
          AND p.quality_status='PASS' AND j.decision_at=NEW.decision_at
          AND NEW.market_as_of=j.market_as_of AND j.market_as_of=p.as_of) THEN
        RAISE EXCEPTION 'SHADOW_REQUIRES_VALID_COMMITTED_PREDICTION_AND_JOURNAL';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER shadow_identity BEFORE INSERT ON big_move_shadow_observations
FOR EACH ROW EXECUTE FUNCTION validate_shadow_identity();
