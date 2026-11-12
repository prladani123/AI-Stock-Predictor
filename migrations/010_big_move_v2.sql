CREATE TABLE IF NOT EXISTS big_move_threshold_policies (
    version text PRIMARY KEY,
    definition jsonb NOT NULL,
    checksum text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS big_move_labels (
    dataset_revision_id uuid NOT NULL REFERENCES dataset_revisions(id),
    instrument_id bigint NOT NULL REFERENCES canonical_instruments(id),
    as_of timestamptz NOT NULL,
    horizon_minutes smallint NOT NULL CHECK (horizon_minutes IN (10, 20, 30)),
    expiry_at timestamptz NOT NULL,
    reference_price numeric(18,6) NOT NULL CHECK (reference_price > 0),
    threshold_policy_version text NOT NULL REFERENCES big_move_threshold_policies(version),
    threshold_value numeric(14,10) NOT NULL CHECK (threshold_value > 0),
    max_future_high numeric(18,6),
    min_future_low numeric(18,6),
    maximum_up_excursion numeric(14,10),
    maximum_down_excursion numeric(14,10),
    maximum_favorable_excursion numeric(14,10),
    maximum_adverse_excursion numeric(14,10),
    close_return_at_horizon numeric(14,10),
    realized_range numeric(14,10),
    first_barrier text CHECK (first_barrier IN ('UP', 'DOWN', 'BOTH_SAME_MINUTE')),
    first_barrier_at timestamptz,
    label text NOT NULL CHECK (label IN ('BIG_UP', 'BIG_DOWN', 'NO_BIG_MOVE', 'AMBIGUOUS', 'UNKNOWN')),
    quality_status text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (dataset_revision_id, instrument_id, as_of, horizon_minutes, threshold_policy_version),
    CHECK (expiry_at = as_of + make_interval(mins => horizon_minutes)),
    CHECK (
      (label = 'UNKNOWN' AND max_future_high IS NULL AND min_future_low IS NULL)
      OR (label <> 'UNKNOWN' AND max_future_high IS NOT NULL AND min_future_low IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS big_move_model_definitions (
    model_checksum text PRIMARY KEY,
    model_family text NOT NULL CHECK (model_family = 'BIG_MOVE_V2'),
    stage text NOT NULL CHECK (stage IN ('MOVE_DETECTOR', 'DIRECTION')),
    horizon_minutes smallint NOT NULL CHECK (horizon_minutes IN (10, 20, 30)),
    algorithm text NOT NULL CHECK (algorithm IN ('LOGISTIC_REGRESSION', 'LIGHTGBM')),
    model_version text NOT NULL,
    dataset_revision_id uuid NOT NULL REFERENCES dataset_revisions(id),
    feature_set_version text NOT NULL REFERENCES feature_set_definitions(version),
    artifact_path text NOT NULL,
    artifact_checksum text NOT NULL,
    manifest jsonb NOT NULL,
    status text NOT NULL CHECK (status IN ('CANDIDATE', 'APPROVED', 'REJECTED')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (stage, horizon_minutes, model_version)
);

CREATE TABLE IF NOT EXISTS big_move_predictions (
    prediction_id text PRIMARY KEY,
    dataset_revision_id uuid NOT NULL REFERENCES dataset_revisions(id),
    instrument_id bigint NOT NULL REFERENCES canonical_instruments(id),
    feature_set_version text NOT NULL REFERENCES feature_set_definitions(version),
    as_of timestamptz NOT NULL,
    horizon_minutes smallint NOT NULL CHECK (horizon_minutes IN (10, 20, 30)),
    expires_at timestamptz NOT NULL,
    reference_price numeric(18,6) NOT NULL CHECK (reference_price > 0),
    move_model_checksum text NOT NULL REFERENCES big_move_model_definitions(model_checksum),
    direction_model_checksum text NOT NULL REFERENCES big_move_model_definitions(model_checksum),
    big_move_probability numeric(12,10) NOT NULL CHECK (big_move_probability BETWEEN 0 AND 1),
    quiet_probability numeric(12,10) NOT NULL CHECK (quiet_probability BETWEEN 0 AND 1),
    state text NOT NULL CHECK (state IN ('BIG_MOVE', 'NO_BIG_MOVE', 'WAIT')),
    direction text CHECK (direction IN ('UP', 'DOWN')),
    direction_probability numeric(12,10) CHECK (direction_probability BETWEEN 0 AND 1),
    up_given_big_move_probability numeric(12,10) CHECK (up_given_big_move_probability BETWEEN 0 AND 1),
    down_given_big_move_probability numeric(12,10) CHECK (down_given_big_move_probability BETWEEN 0 AND 1),
    expected_move_points numeric(18,6),
    expected_move_percent numeric(14,10),
    market_regime text NOT NULL,
    session_bias jsonb NOT NULL,
    quality_status text NOT NULL,
    model_version text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (move_model_checksum, direction_model_checksum, instrument_id, horizon_minutes, as_of)
);

CREATE TABLE IF NOT EXISTS experimental_model_outputs (
    output_id text PRIMARY KEY,
    dataset_revision_id uuid NOT NULL REFERENCES dataset_revisions(id),
    instrument_id bigint NOT NULL REFERENCES canonical_instruments(id),
    as_of timestamptz NOT NULL,
    model_name text NOT NULL,
    model_version text NOT NULL,
    pred_len smallint NOT NULL CHECK (pred_len = 30),
    forecast_path jsonb NOT NULL,
    summary jsonb NOT NULL,
    status text NOT NULL CHECK (status = 'EXPERIMENTAL_MODEL_OUTPUT'),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (dataset_revision_id, instrument_id, as_of, model_name, model_version)
);

CREATE TABLE IF NOT EXISTS big_move_prediction_outcomes (
    prediction_id text PRIMARY KEY REFERENCES big_move_predictions(prediction_id),
    actual_label text NOT NULL CHECK (actual_label IN ('BIG_UP', 'BIG_DOWN', 'NO_BIG_MOVE', 'AMBIGUOUS', 'UNKNOWN')),
    quality_status text NOT NULL,
    attached_at timestamptz NOT NULL,
    realized_summary jsonb NOT NULL
);

CREATE OR REPLACE FUNCTION reject_big_move_v2_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'Big Move V2 research lineage is immutable';
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION enforce_accepted_big_move_revision() RETURNS trigger AS $$
BEGIN
    IF NOT EXISTS (
      SELECT 1 FROM dataset_revisions
      WHERE id = NEW.dataset_revision_id AND status = 'ACCEPTED'
    ) THEN
      RAISE EXCEPTION 'Big Move V2 lineage requires an ACCEPTED dataset revision';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION enforce_big_move_outcome_after_expiry() RETURNS trigger AS $$
BEGIN
    IF NEW.attached_at < (SELECT expires_at FROM big_move_predictions WHERE prediction_id = NEW.prediction_id) THEN
      RAISE EXCEPTION 'Big Move V2 outcome cannot be attached before prediction expiry';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS accepted_revision_big_move_labels ON big_move_labels;
CREATE TRIGGER accepted_revision_big_move_labels
BEFORE INSERT ON big_move_labels
FOR EACH ROW EXECUTE FUNCTION enforce_accepted_big_move_revision();

DROP TRIGGER IF EXISTS accepted_revision_big_move_models ON big_move_model_definitions;
CREATE TRIGGER accepted_revision_big_move_models
BEFORE INSERT ON big_move_model_definitions
FOR EACH ROW EXECUTE FUNCTION enforce_accepted_big_move_revision();

DROP TRIGGER IF EXISTS accepted_revision_big_move_predictions ON big_move_predictions;
CREATE TRIGGER accepted_revision_big_move_predictions
BEFORE INSERT ON big_move_predictions
FOR EACH ROW EXECUTE FUNCTION enforce_accepted_big_move_revision();

DROP TRIGGER IF EXISTS accepted_revision_experimental_outputs ON experimental_model_outputs;
CREATE TRIGGER accepted_revision_experimental_outputs
BEFORE INSERT ON experimental_model_outputs
FOR EACH ROW EXECUTE FUNCTION enforce_accepted_big_move_revision();

DROP TRIGGER IF EXISTS big_move_outcome_after_expiry ON big_move_prediction_outcomes;
CREATE TRIGGER big_move_outcome_after_expiry
BEFORE INSERT ON big_move_prediction_outcomes
FOR EACH ROW EXECUTE FUNCTION enforce_big_move_outcome_after_expiry();

DO $$
DECLARE table_name text;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'big_move_threshold_policies', 'big_move_labels', 'big_move_model_definitions',
        'big_move_predictions', 'big_move_prediction_outcomes', 'experimental_model_outputs'
    ] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I', 'immutable_' || table_name, table_name);
        EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION reject_big_move_v2_mutation()', 'immutable_' || table_name, table_name);
    END LOOP;
END;
$$;

INSERT INTO schema_migrations(version) VALUES ('010_big_move_v2')
ON CONFLICT (version) DO NOTHING;
