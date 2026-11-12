ALTER TABLE experimental_model_outputs
    ALTER COLUMN dataset_revision_id DROP NOT NULL;

ALTER TABLE experimental_model_outputs
    ADD COLUMN IF NOT EXISTS identity_checksum text,
    ADD COLUMN IF NOT EXISTS prediction_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS replay_pool_revision_id uuid REFERENCES dataset_revisions(id),
    ADD COLUMN IF NOT EXISTS input_checksum text,
    ADD COLUMN IF NOT EXISTS reference_state_checksum text,
    ADD COLUMN IF NOT EXISTS configuration_checksum text,
    ADD COLUMN IF NOT EXISTS upstream_revision text,
    ADD COLUMN IF NOT EXISTS model_revision text,
    ADD COLUMN IF NOT EXISTS tokenizer_revision text,
    ADD COLUMN IF NOT EXISTS seed bigint,
    ADD COLUMN IF NOT EXISTS path_count integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS sampling jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS threshold_policy_version text,
    ADD COLUMN IF NOT EXISTS sampling_pool_checksum text,
    ADD COLUMN IF NOT EXISTS decision_at timestamptz,
    ADD COLUMN IF NOT EXISTS available_at timestamptz,
    ADD COLUMN IF NOT EXISTS computed_at timestamptz,
    ADD COLUMN IF NOT EXISTS source_max_timestamp timestamptz,
    ADD COLUMN IF NOT EXISTS point_in_time_status text;

DROP TRIGGER IF EXISTS immutable_experimental_model_outputs ON experimental_model_outputs;

CREATE OR REPLACE FUNCTION strict_market_iso_timestamp(value text, legacy_output_id text,
                                                       field_name text) RETURNS timestamptz AS $$
BEGIN
    IF value IS NULL OR btrim(value) = '' THEN
        RETURN NULL;
    END IF;
    IF value !~ '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$' THEN
        RAISE EXCEPTION 'invalid legacy % timestamp for output %: %',
            field_name, legacy_output_id, value;
    END IF;
    RETURN value::timestamptz;
EXCEPTION
    WHEN datetime_field_overflow OR invalid_datetime_format THEN
        RAISE EXCEPTION 'invalid legacy % timestamp for output %: %',
            field_name, legacy_output_id, value;
END;
$$ LANGUAGE plpgsql IMMUTABLE;

UPDATE experimental_model_outputs
SET available_at = COALESCE(strict_market_iso_timestamp(summary->>'availableAt', output_id, 'availableAt'), as_of),
    computed_at = COALESCE(strict_market_iso_timestamp(summary->>'computedAt', output_id, 'computedAt'), as_of),
    source_max_timestamp = COALESCE(strict_market_iso_timestamp(summary->>'sourceMaxTimestamp', output_id, 'sourceMaxTimestamp'), as_of),
    -- Legacy rows have no committed production identity. Never upgrade them.
    point_in_time_status = 'HISTORICAL_COUNTERFACTUAL_RESEARCH',
    upstream_revision = summary->>'upstreamRevision',
    model_revision = summary->>'modelRevision',
    tokenizer_revision = summary->>'tokenizerRevision',
    seed = NULLIF(summary->>'seed', '')::bigint,
    path_count = COALESCE(NULLIF(summary->>'pathCount', '')::integer, 0),
    sampling = COALESCE(summary->'sampling', '{}'::jsonb),
    threshold_policy_version = summary->>'thresholdPolicyVersion',
    sampling_pool_checksum = summary->>'samplingPoolChecksum'
WHERE available_at IS NULL OR computed_at IS NULL OR source_max_timestamp IS NULL
   OR point_in_time_status IS NULL;

DROP FUNCTION strict_market_iso_timestamp(text, text, text);

ALTER TABLE experimental_model_outputs
    ALTER COLUMN available_at SET NOT NULL,
    ALTER COLUMN computed_at SET NOT NULL,
    ALTER COLUMN source_max_timestamp SET NOT NULL,
    ALTER COLUMN point_in_time_status SET NOT NULL;

ALTER TABLE experimental_model_outputs
    DROP CONSTRAINT IF EXISTS experimental_output_status_check;

ALTER TABLE experimental_model_outputs
    ADD CONSTRAINT experimental_output_status_check
    CHECK (status = 'EXPERIMENTAL_MODEL_OUTPUT');

ALTER TABLE experimental_model_outputs
    DROP CONSTRAINT IF EXISTS experimental_output_pit_status_check;

ALTER TABLE experimental_model_outputs
    ADD CONSTRAINT experimental_output_pit_status_check
    CHECK (point_in_time_status IN (
        'PROSPECTIVE_DECISION_AVAILABLE', 'PROSPECTIVE_POST_DECISION',
        'HISTORICAL_COUNTERFACTUAL_RESEARCH'));

ALTER TABLE experimental_model_outputs
    DROP CONSTRAINT IF EXISTS experimental_output_live_revision_check;

ALTER TABLE experimental_model_outputs
    ADD CONSTRAINT experimental_output_live_revision_check
    CHECK (dataset_revision_id IS NOT NULL
           OR point_in_time_status IN ('PROSPECTIVE_DECISION_AVAILABLE',
                                       'PROSPECTIVE_POST_DECISION'));

DO $$
DECLARE constraint_name text;
BEGIN
    SELECT conname INTO constraint_name
    FROM pg_constraint
    WHERE conrelid = 'experimental_model_outputs'::regclass
      AND contype = 'u'
      AND pg_get_constraintdef(oid) LIKE '%dataset_revision_id, instrument_id, as_of, model_name, model_version%';
    IF constraint_name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE experimental_model_outputs DROP CONSTRAINT %I', constraint_name);
    END IF;
END;
$$;

CREATE UNIQUE INDEX IF NOT EXISTS experimental_output_identity_unique
    ON experimental_model_outputs(identity_checksum)
    WHERE identity_checksum IS NOT NULL;

CREATE INDEX IF NOT EXISTS experimental_output_instrument_reference
    ON experimental_model_outputs(instrument_id, as_of DESC, model_name);

CREATE OR REPLACE FUNCTION enforce_experimental_output_revision() RETURNS trigger AS $$
DECLARE linked_id text;
DECLARE market_cutoff timestamptz;
BEGIN
    market_cutoff := (NEW.summary->>'marketAsOf')::timestamptz;
    IF NEW.identity_checksum IS NULL OR NEW.input_checksum IS NULL
       OR NEW.configuration_checksum IS NULL OR market_cutoff IS NULL
       OR NEW.source_max_timestamp > market_cutoff
       OR market_cutoff > NEW.computed_at OR NEW.computed_at > NEW.available_at
       OR NEW.path_count < 1 THEN
        RAISE EXCEPTION 'invalid external research identity or market cutoff';
    END IF;
    IF NEW.point_in_time_status IN ('PROSPECTIVE_DECISION_AVAILABLE', 'PROSPECTIVE_POST_DECISION') THEN
        IF NEW.dataset_revision_id IS NOT NULL OR NEW.decision_at IS NULL
           OR jsonb_array_length(NEW.prediction_ids) = 0 OR market_cutoff > NEW.decision_at
           OR (NEW.point_in_time_status='PROSPECTIVE_DECISION_AVAILABLE'
               AND NEW.available_at > NEW.decision_at)
           OR (NEW.point_in_time_status='PROSPECTIVE_POST_DECISION'
               AND NEW.available_at <= NEW.decision_at) THEN
            RAISE EXCEPTION 'invalid prospective research lineage';
        END IF;
        FOR linked_id IN SELECT jsonb_array_elements_text(NEW.prediction_ids) LOOP
            IF NOT EXISTS (
                SELECT 1 FROM big_move_predictions p JOIN big_move_evidence_journal j USING(prediction_id)
                WHERE p.prediction_id=linked_id AND p.instrument_id=NEW.instrument_id
                  AND p.as_of=NEW.as_of AND j.market_as_of=market_cutoff
                  AND j.decision_at=NEW.decision_at
            ) THEN
                RAISE EXCEPTION 'external output does not match committed prediction evidence';
            END IF;
        END LOOP;
    ELSIF NEW.dataset_revision_id IS NULL OR jsonb_array_length(NEW.prediction_ids) <> 0 THEN
        RAISE EXCEPTION 'historical research requires its dataset, not a live prediction identity';
    END IF;
    IF NEW.model_name='BANKNIFTY_TIME_MACHINE_V1' AND NEW.replay_pool_revision_id IS NULL THEN
        RAISE EXCEPTION 'Time Machine requires an explicit replay pool';
    END IF;
    IF NEW.dataset_revision_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM dataset_revisions
        WHERE id = NEW.dataset_revision_id AND status = 'ACCEPTED'
    ) THEN
        RAISE EXCEPTION 'experimental research requires an ACCEPTED dataset revision';
    END IF;
    IF NEW.replay_pool_revision_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM dataset_revisions
        WHERE id = NEW.replay_pool_revision_id AND status = 'ACCEPTED'
    ) THEN
        RAISE EXCEPTION 'Time Machine replay pool requires an ACCEPTED dataset revision';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS accepted_revision_experimental_outputs ON experimental_model_outputs;
CREATE TRIGGER accepted_revision_experimental_outputs
BEFORE INSERT ON experimental_model_outputs
FOR EACH ROW EXECUTE FUNCTION enforce_experimental_output_revision();

DROP TRIGGER IF EXISTS immutable_experimental_model_outputs ON experimental_model_outputs;
CREATE TRIGGER immutable_experimental_model_outputs
BEFORE UPDATE OR DELETE ON experimental_model_outputs
FOR EACH ROW EXECUTE FUNCTION reject_big_move_v2_mutation();

INSERT INTO schema_migrations(version) VALUES ('022_experimental_research_identity')
ON CONFLICT (version) DO NOTHING;
