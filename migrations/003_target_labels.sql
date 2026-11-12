CREATE TABLE IF NOT EXISTS threshold_policies (
    version text PRIMARY KEY,
    thresholds jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS target_labels (
    dataset_revision_id uuid NOT NULL REFERENCES dataset_revisions(id) ON DELETE RESTRICT,
    instrument_id bigint NOT NULL REFERENCES canonical_instruments(id) ON DELETE RESTRICT,
    as_of timestamptz NOT NULL,
    horizon_minutes smallint NOT NULL CHECK (horizon_minutes IN (5, 10, 15, 20)),
    reference_price numeric(18,8) NOT NULL CHECK (reference_price > 0),
    expiry_at timestamptz NOT NULL,
    future_price numeric(18,8),
    threshold_policy_version text NOT NULL REFERENCES threshold_policies(version) ON DELETE RESTRICT,
    threshold_value numeric(18,10) NOT NULL CHECK (threshold_value > 0),
    realised_return numeric(24,12),
    label text NOT NULL CHECK (label IN ('UP', 'DOWN', 'NO_TRADE', 'UNKNOWN')),
    quality_status text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (
      dataset_revision_id, instrument_id, as_of, horizon_minutes, threshold_policy_version
    ),
    CHECK (expiry_at = as_of + make_interval(mins => horizon_minutes)),
    CHECK (
      (label = 'UNKNOWN' AND future_price IS NULL AND realised_return IS NULL)
      OR (label <> 'UNKNOWN' AND future_price > 0 AND realised_return IS NOT NULL)
    )
);

CREATE OR REPLACE FUNCTION reject_threshold_policy_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'threshold policy versions are immutable';
END;
$$;

DROP TRIGGER IF EXISTS immutable_threshold_policies ON threshold_policies;
CREATE TRIGGER immutable_threshold_policies
BEFORE UPDATE OR DELETE ON threshold_policies
FOR EACH ROW EXECUTE FUNCTION reject_threshold_policy_mutation();

CREATE OR REPLACE FUNCTION enforce_accepted_label_revision()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
      SELECT 1 FROM dataset_revisions
      WHERE id = NEW.dataset_revision_id AND status = 'ACCEPTED'
    ) THEN
      RAISE EXCEPTION 'labels require an ACCEPTED dataset revision';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS accepted_revision_labels ON target_labels;
CREATE TRIGGER accepted_revision_labels
BEFORE INSERT ON target_labels
FOR EACH ROW EXECUTE FUNCTION enforce_accepted_label_revision();

CREATE OR REPLACE FUNCTION reject_label_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'target labels are immutable';
END;
$$;

DROP TRIGGER IF EXISTS immutable_target_labels ON target_labels;
CREATE TRIGGER immutable_target_labels
BEFORE UPDATE OR DELETE ON target_labels
FOR EACH ROW EXECUTE FUNCTION reject_label_mutation();

INSERT INTO schema_migrations(version) VALUES ('003_target_labels')
ON CONFLICT (version) DO NOTHING;
