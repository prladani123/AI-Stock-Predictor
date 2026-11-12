CREATE TABLE IF NOT EXISTS feature_set_definitions (
    version text PRIMARY KEY,
    created_at timestamptz NOT NULL,
    definition jsonb NOT NULL,
    checksum char(64) NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS feature_rows (
    dataset_revision_id uuid NOT NULL,
    instrument_id bigint NOT NULL,
    as_of timestamptz NOT NULL,
    feature_set_version text NOT NULL
      REFERENCES feature_set_definitions(version) ON DELETE RESTRICT,
    source_max_timestamp timestamptz NOT NULL,
    feature_values jsonb NOT NULL,
    quality_status text NOT NULL
      CHECK (quality_status IN ('AVAILABLE', 'PARTIAL_LOOKBACK', 'UNAVAILABLE', 'INVALID')),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (dataset_revision_id, instrument_id, as_of, feature_set_version),
    FOREIGN KEY (dataset_revision_id, instrument_id)
      REFERENCES dataset_revision_instruments(revision_id, instrument_id) ON DELETE RESTRICT,
    CHECK (source_max_timestamp <= as_of)
);

CREATE OR REPLACE FUNCTION reject_feature_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'feature definitions and rows are immutable';
END;
$$;

CREATE OR REPLACE FUNCTION enforce_accepted_feature_revision()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
      SELECT 1 FROM dataset_revisions
      WHERE id = NEW.dataset_revision_id AND status = 'ACCEPTED'
    ) THEN
      RAISE EXCEPTION 'feature rows require an ACCEPTED dataset revision';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS accepted_revision_features ON feature_rows;
CREATE TRIGGER accepted_revision_features
BEFORE INSERT ON feature_rows
FOR EACH ROW EXECUTE FUNCTION enforce_accepted_feature_revision();

DROP TRIGGER IF EXISTS immutable_feature_sets ON feature_set_definitions;
CREATE TRIGGER immutable_feature_sets
BEFORE UPDATE OR DELETE ON feature_set_definitions
FOR EACH ROW EXECUTE FUNCTION reject_feature_mutation();

DROP TRIGGER IF EXISTS immutable_feature_rows ON feature_rows;
CREATE TRIGGER immutable_feature_rows
BEFORE UPDATE OR DELETE ON feature_rows
FOR EACH ROW EXECUTE FUNCTION reject_feature_mutation();

INSERT INTO schema_migrations(version) VALUES ('004_point_in_time_features')
ON CONFLICT (version) DO NOTHING;
