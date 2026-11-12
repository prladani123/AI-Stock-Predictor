DO $$
BEGIN
    IF EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_name='ml_predictions' AND column_name='abstained'
    ) THEN
      ALTER TABLE ml_predictions RENAME COLUMN abstained TO system_abstained;
    END IF;
    IF EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_name='ml_predictions' AND column_name='reason_codes'
    ) THEN
      ALTER TABLE ml_predictions RENAME COLUMN reason_codes TO abstention_reasons;
    END IF;
END;
$$;

ALTER TABLE ml_predictions
    ADD COLUMN IF NOT EXISTS calibrated_class text
      CHECK (calibrated_class IN ('UP', 'DOWN', 'NO_TRADE')),
    ADD COLUMN IF NOT EXISTS reliability_bucket jsonb;

ALTER TABLE baseline_definitions
    DROP CONSTRAINT IF EXISTS baseline_definitions_kind_check;
ALTER TABLE baseline_definitions
    ADD CONSTRAINT baseline_definitions_kind_check
    CHECK (kind IN ('MAJORITY', 'PERSISTENCE'));

CREATE TABLE IF NOT EXISTS calibration_reliability_buckets (
    calibration_checksum text NOT NULL
      REFERENCES calibration_runs(calibration_checksum) ON DELETE RESTRICT,
    bucket_index smallint NOT NULL CHECK (bucket_index BETWEEN 0 AND 9),
    lower_bound numeric(8,6) NOT NULL,
    upper_bound numeric(8,6) NOT NULL,
    sample_count integer NOT NULL CHECK (sample_count >= 0),
    mean_confidence numeric(12,10),
    observed_accuracy numeric(12,10),
    directional_count integer NOT NULL CHECK (directional_count >= 0),
    observed_directional_precision numeric(12,10),
    PRIMARY KEY (calibration_checksum, bucket_index),
    CHECK (upper_bound > lower_bound)
);

CREATE OR REPLACE FUNCTION reject_phase67_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Phase 6/7 research lineage is immutable';
END;
$$;

DROP TRIGGER IF EXISTS immutable_model_definitions ON model_definitions;
CREATE TRIGGER immutable_model_definitions
BEFORE UPDATE OR DELETE ON model_definitions
FOR EACH ROW EXECUTE FUNCTION reject_phase67_mutation();

DROP TRIGGER IF EXISTS immutable_training_runs ON training_runs;
CREATE TRIGGER immutable_training_runs
BEFORE UPDATE OR DELETE ON training_runs
FOR EACH ROW EXECUTE FUNCTION reject_phase67_mutation();

DROP TRIGGER IF EXISTS immutable_model_comparisons ON model_comparisons;
CREATE TRIGGER immutable_model_comparisons
BEFORE UPDATE OR DELETE ON model_comparisons
FOR EACH ROW EXECUTE FUNCTION reject_phase67_mutation();

DROP TRIGGER IF EXISTS immutable_model_bundles ON model_bundles;
CREATE TRIGGER immutable_model_bundles
BEFORE UPDATE OR DELETE ON model_bundles
FOR EACH ROW EXECUTE FUNCTION reject_phase67_mutation();

DROP TRIGGER IF EXISTS immutable_model_bundle_members ON model_bundle_members;
CREATE TRIGGER immutable_model_bundle_members
BEFORE UPDATE OR DELETE ON model_bundle_members
FOR EACH ROW EXECUTE FUNCTION reject_phase67_mutation();

DROP TRIGGER IF EXISTS immutable_calibration_runs ON calibration_runs;
CREATE TRIGGER immutable_calibration_runs
BEFORE UPDATE OR DELETE ON calibration_runs
FOR EACH ROW EXECUTE FUNCTION reject_phase67_mutation();

DROP TRIGGER IF EXISTS immutable_calibration_buckets ON calibration_reliability_buckets;
CREATE TRIGGER immutable_calibration_buckets
BEFORE UPDATE OR DELETE ON calibration_reliability_buckets
FOR EACH ROW EXECUTE FUNCTION reject_phase67_mutation();

DROP TRIGGER IF EXISTS immutable_abstention_policies ON abstention_policies;
CREATE TRIGGER immutable_abstention_policies
BEFORE UPDATE OR DELETE ON abstention_policies
FOR EACH ROW EXECUTE FUNCTION reject_phase67_mutation();

DROP TRIGGER IF EXISTS immutable_ml_predictions ON ml_predictions;
CREATE TRIGGER immutable_ml_predictions
BEFORE UPDATE OR DELETE ON ml_predictions
FOR EACH ROW EXECUTE FUNCTION reject_phase67_mutation();

DROP TRIGGER IF EXISTS immutable_ml_prediction_outcomes ON ml_prediction_outcomes;
CREATE TRIGGER immutable_ml_prediction_outcomes
BEFORE UPDATE OR DELETE ON ml_prediction_outcomes
FOR EACH ROW EXECUTE FUNCTION reject_phase67_mutation();

INSERT INTO schema_migrations(version) VALUES ('008_phase0_7_integrity_repair')
ON CONFLICT (version) DO NOTHING;
