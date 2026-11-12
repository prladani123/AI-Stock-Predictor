CREATE TABLE IF NOT EXISTS baseline_definitions (
    version text PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN ('MAJORITY', 'PERSISTENCE')),
    horizon_minutes smallint NOT NULL CHECK (horizon_minutes IN (5, 10, 15, 20)),
    config jsonb NOT NULL,
    checksum char(64) NOT NULL
);

CREATE TABLE IF NOT EXISTS research_runs (
    id text PRIMARY KEY,
    manifest jsonb NOT NULL,
    manifest_checksum char(64) NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS walk_forward_folds (
    run_id text NOT NULL REFERENCES research_runs(id) ON DELETE RESTRICT,
    fold_id text NOT NULL,
    fold_values jsonb NOT NULL,
    PRIMARY KEY (run_id, fold_id)
);

CREATE TABLE IF NOT EXISTS research_predictions (
    id text PRIMARY KEY,
    run_id text NOT NULL,
    fold_id text NOT NULL,
    baseline_version text NOT NULL REFERENCES baseline_definitions(version) ON DELETE RESTRICT,
    feature_set_version text NOT NULL REFERENCES feature_set_definitions(version) ON DELETE RESTRICT,
    dataset_revision_id uuid NOT NULL REFERENCES dataset_revisions(id) ON DELETE RESTRICT,
    instrument_id bigint NOT NULL REFERENCES canonical_instruments(id) ON DELETE RESTRICT,
    horizon_minutes smallint NOT NULL CHECK (horizon_minutes IN (5, 10, 15, 20)),
    as_of timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    predicted_class text NOT NULL
      CHECK (predicted_class IN ('UP', 'DOWN', 'NO_TRADE', 'UNAVAILABLE', 'ERROR')),
    confidence numeric(18,10),
    prediction_reason text NOT NULL,
    created_replay_clock timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (run_id, fold_id)
      REFERENCES walk_forward_folds(run_id, fold_id) ON DELETE RESTRICT,
    FOREIGN KEY (dataset_revision_id, instrument_id, as_of, feature_set_version)
      REFERENCES feature_rows(
        dataset_revision_id, instrument_id, as_of, feature_set_version
      ) ON DELETE RESTRICT,
    CHECK (expires_at = as_of + make_interval(mins => horizon_minutes)),
    CHECK (created_replay_clock = as_of),
    CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    UNIQUE (run_id, fold_id, baseline_version, horizon_minutes, as_of)
);

CREATE TABLE IF NOT EXISTS prediction_outcomes (
    prediction_id text PRIMARY KEY REFERENCES research_predictions(id) ON DELETE RESTRICT,
    attached_replay_clock timestamptz NOT NULL,
    actual_class text NOT NULL CHECK (actual_class IN ('UP', 'DOWN', 'NO_TRADE', 'UNKNOWN')),
    quality_status text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS evaluation_report_manifests (
    run_id text PRIMARY KEY REFERENCES research_runs(id) ON DELETE RESTRICT,
    report jsonb NOT NULL,
    checksum char(64) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION reject_research_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'research definitions, predictions, outcomes and reports are immutable';
END;
$$;

CREATE OR REPLACE FUNCTION enforce_prediction_boundaries()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
      SELECT 1 FROM dataset_revisions
      WHERE id = NEW.dataset_revision_id AND status = 'ACCEPTED'
    ) THEN
      RAISE EXCEPTION 'predictions require an ACCEPTED dataset revision';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS accepted_revision_predictions ON research_predictions;
CREATE TRIGGER accepted_revision_predictions
BEFORE INSERT ON research_predictions
FOR EACH ROW EXECUTE FUNCTION enforce_prediction_boundaries();

CREATE OR REPLACE FUNCTION enforce_outcome_expiry()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    required_expiry timestamptz;
BEGIN
    SELECT expires_at INTO required_expiry
    FROM research_predictions WHERE id = NEW.prediction_id;
    IF required_expiry IS NULL OR NEW.attached_replay_clock < required_expiry THEN
      RAISE EXCEPTION 'prediction outcomes may be attached only at or after expiry';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS delayed_prediction_outcomes ON prediction_outcomes;
CREATE TRIGGER delayed_prediction_outcomes
BEFORE INSERT ON prediction_outcomes
FOR EACH ROW EXECUTE FUNCTION enforce_outcome_expiry();

DROP TRIGGER IF EXISTS immutable_baseline_definitions ON baseline_definitions;
CREATE TRIGGER immutable_baseline_definitions
BEFORE UPDATE OR DELETE ON baseline_definitions
FOR EACH ROW EXECUTE FUNCTION reject_research_mutation();

DROP TRIGGER IF EXISTS immutable_research_runs ON research_runs;
CREATE TRIGGER immutable_research_runs
BEFORE UPDATE OR DELETE ON research_runs
FOR EACH ROW EXECUTE FUNCTION reject_research_mutation();

DROP TRIGGER IF EXISTS immutable_walk_forward_folds ON walk_forward_folds;
CREATE TRIGGER immutable_walk_forward_folds
BEFORE UPDATE OR DELETE ON walk_forward_folds
FOR EACH ROW EXECUTE FUNCTION reject_research_mutation();

DROP TRIGGER IF EXISTS immutable_research_predictions ON research_predictions;
CREATE TRIGGER immutable_research_predictions
BEFORE UPDATE OR DELETE ON research_predictions
FOR EACH ROW EXECUTE FUNCTION reject_research_mutation();

DROP TRIGGER IF EXISTS immutable_prediction_outcomes ON prediction_outcomes;
CREATE TRIGGER immutable_prediction_outcomes
BEFORE UPDATE OR DELETE ON prediction_outcomes
FOR EACH ROW EXECUTE FUNCTION reject_research_mutation();

DROP TRIGGER IF EXISTS immutable_evaluation_reports ON evaluation_report_manifests;
CREATE TRIGGER immutable_evaluation_reports
BEFORE UPDATE OR DELETE ON evaluation_report_manifests
FOR EACH ROW EXECUTE FUNCTION reject_research_mutation();

INSERT INTO schema_migrations(version) VALUES ('005_walk_forward_replay')
ON CONFLICT (version) DO NOTHING;
