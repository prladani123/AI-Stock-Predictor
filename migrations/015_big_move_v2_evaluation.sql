-- Stage D evidence is append-only, separate from legacy evaluation/calibration.
CREATE TABLE big_move_evaluation_runs (
    evaluation_id text PRIMARY KEY,
    dataset_revision_id uuid NOT NULL REFERENCES dataset_revisions(id),
    instrument_id bigint NOT NULL REFERENCES canonical_instruments(id),
    candidate_model_checksum text NOT NULL REFERENCES big_move_model_definitions(model_checksum),
    model_version text NOT NULL,
    artifact_checksum text NOT NULL,
    algorithm text NOT NULL CHECK (algorithm IN ('LOGISTIC_REGRESSION','LIGHTGBM')),
    horizon_minutes smallint NOT NULL CHECK (horizon_minutes IN (10,20,30)),
    feature_set_version text NOT NULL REFERENCES feature_set_definitions(version),
    started_data_date date NOT NULL,
    ended_data_date date NOT NULL CHECK (ended_data_date >= started_data_date),
    fold_count integer NOT NULL CHECK (fold_count > 0),
    sample_count integer NOT NULL CHECK (sample_count >= 0),
    metrics jsonb NOT NULL,
    stability jsonb NOT NULL,
    configuration jsonb NOT NULL,
    evaluation_checksum text NOT NULL UNIQUE CHECK (evaluation_checksum = evaluation_id),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE big_move_model_comparisons (
    comparison_id text PRIMARY KEY,
    dataset_revision_id uuid NOT NULL REFERENCES dataset_revisions(id),
    instrument_id bigint NOT NULL REFERENCES canonical_instruments(id),
    horizon_minutes smallint NOT NULL CHECK (horizon_minutes IN (10,20,30)),
    selected_algorithm text CHECK (selected_algorithm IN ('LOGISTIC_REGRESSION','LIGHTGBM')),
    candidate_results jsonb NOT NULL,
    selection_reason jsonb NOT NULL,
    comparison_checksum text NOT NULL UNIQUE CHECK (comparison_checksum = comparison_id),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE big_move_approval_decisions (
    approval_id text PRIMARY KEY,
    candidate_model_checksum text NOT NULL REFERENCES big_move_model_definitions(model_checksum),
    evaluation_checksum text NOT NULL REFERENCES big_move_evaluation_runs(evaluation_checksum),
    comparison_checksum text NOT NULL REFERENCES big_move_model_comparisons(comparison_checksum),
    policy_version text NOT NULL,
    policy_definition jsonb NOT NULL,
    decision text NOT NULL CHECK (decision IN ('PASS','FAIL','INSUFFICIENT_EVIDENCE')),
    reasons jsonb NOT NULL,
    metrics_snapshot jsonb NOT NULL,
    approval_checksum text NOT NULL UNIQUE CHECK (approval_checksum = approval_id),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX big_move_evaluation_lookup ON big_move_evaluation_runs(candidate_model_checksum, created_at DESC);
CREATE INDEX big_move_comparison_lookup ON big_move_model_comparisons(dataset_revision_id,instrument_id,horizon_minutes,created_at DESC);
CREATE INDEX big_move_approval_lookup ON big_move_approval_decisions(candidate_model_checksum,created_at DESC);

DO $$
DECLARE relation_name text;
BEGIN
    FOREACH relation_name IN ARRAY ARRAY['big_move_evaluation_runs','big_move_model_comparisons','big_move_approval_decisions'] LOOP
        EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION reject_big_move_v2_mutation()',
            'immutable_' || relation_name, relation_name);
    END LOOP;
END;
$$;

CREATE FUNCTION enforce_big_move_evidence_promotion() RETURNS trigger AS $$
BEGIN
    IF NEW.status = 'APPROVED' AND NOT EXISTS (
        SELECT 1 FROM big_move_approval_decisions a
        JOIN big_move_evaluation_runs e ON e.evaluation_checksum = a.evaluation_checksum
        JOIN big_move_model_definitions c ON c.model_checksum = a.candidate_model_checksum
        WHERE a.approval_checksum = NEW.manifest->>'approval_decision_checksum'
          AND a.decision = 'PASS'
          AND a.policy_version = NEW.manifest->>'approval_policy_version'
          AND e.evaluation_checksum = NEW.manifest->>'evaluation_checksum'
          AND c.model_checksum = NEW.manifest->>'approved_from_model_checksum'
          AND c.artifact_checksum = NEW.manifest->>'approved_from_artifact_checksum'
          AND c.status = 'CANDIDATE' AND c.stage = 'MOVE_DETECTOR'
          AND e.candidate_model_checksum = c.model_checksum
          AND e.artifact_checksum = c.artifact_checksum
          AND c.dataset_revision_id = NEW.dataset_revision_id
          AND c.horizon_minutes = NEW.horizon_minutes
          AND c.algorithm = NEW.algorithm
          AND c.feature_set_version = NEW.feature_set_version
    ) THEN
        RAISE EXCEPTION 'V2 approval requires PASS evidence for the exact candidate';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER evidence_gated_big_move_promotion BEFORE INSERT ON big_move_model_definitions
FOR EACH ROW EXECUTE FUNCTION enforce_big_move_evidence_promotion();

INSERT INTO schema_migrations(version) VALUES ('015_big_move_v2_evaluation') ON CONFLICT DO NOTHING;
