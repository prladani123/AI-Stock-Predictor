CREATE TABLE IF NOT EXISTS calibration_runs (
    calibration_checksum TEXT PRIMARY KEY,
    model_checksum TEXT NOT NULL REFERENCES model_definitions(model_checksum),
    horizon_minutes SMALLINT NOT NULL CHECK (horizon_minutes IN (5, 10, 15, 20)),
    method TEXT NOT NULL CHECK (method IN ('SIGMOID', 'ISOTONIC', 'UNAVAILABLE')),
    version TEXT NOT NULL,
    parameters JSONB NOT NULL,
    metrics JSONB NOT NULL,
    rejection_reasons JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS abstention_policies (
    policy_checksum TEXT PRIMARY KEY,
    version TEXT NOT NULL UNIQUE,
    definition JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ml_predictions (
    prediction_id TEXT PRIMARY KEY,
    dataset_revision_id UUID NOT NULL REFERENCES dataset_revisions(id),
    feature_set_version TEXT NOT NULL REFERENCES feature_set_definitions(version),
    model_checksum TEXT NOT NULL REFERENCES model_definitions(model_checksum),
    calibration_checksum TEXT REFERENCES calibration_runs(calibration_checksum),
    policy_checksum TEXT NOT NULL REFERENCES abstention_policies(policy_checksum),
    training_manifest_checksum TEXT NOT NULL REFERENCES training_runs(training_manifest_checksum),
    fold_id TEXT NOT NULL,
    instrument_id BIGINT NOT NULL REFERENCES canonical_instruments(id),
    horizon_minutes SMALLINT NOT NULL CHECK (horizon_minutes IN (5, 10, 15, 20)),
    as_of TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    raw_probabilities JSONB NOT NULL,
    calibrated_probabilities JSONB,
    raw_class TEXT NOT NULL CHECK (raw_class IN ('UP', 'DOWN', 'NO_TRADE')),
    final_class TEXT NOT NULL CHECK (final_class IN ('UP', 'DOWN', 'NO_TRADE')),
    abstained BOOLEAN NOT NULL,
    reason_codes JSONB NOT NULL,
    created_replay_clock TIMESTAMPTZ NOT NULL,
    UNIQUE (model_checksum, instrument_id, horizon_minutes, as_of, policy_checksum)
);

CREATE TABLE IF NOT EXISTS ml_prediction_outcomes (
    prediction_id TEXT PRIMARY KEY REFERENCES ml_predictions(prediction_id),
    actual_class TEXT NOT NULL CHECK (actual_class IN ('UP', 'DOWN', 'NO_TRADE', 'UNKNOWN')),
    quality_status TEXT NOT NULL,
    attached_replay_clock TIMESTAMPTZ NOT NULL
);

CREATE OR REPLACE FUNCTION enforce_ml_outcome_after_expiry() RETURNS trigger AS $$
BEGIN
    IF NEW.attached_replay_clock < (
        SELECT expires_at FROM ml_predictions WHERE prediction_id = NEW.prediction_id
    ) THEN
        RAISE EXCEPTION 'prediction outcome cannot be attached before expiry';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS ml_outcome_after_expiry ON ml_prediction_outcomes;
CREATE TRIGGER ml_outcome_after_expiry
BEFORE INSERT ON ml_prediction_outcomes
FOR EACH ROW EXECUTE FUNCTION enforce_ml_outcome_after_expiry();
