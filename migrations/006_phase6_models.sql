CREATE TABLE IF NOT EXISTS model_definitions (
    model_checksum TEXT PRIMARY KEY,
    algorithm TEXT NOT NULL CHECK (algorithm IN ('LOGISTIC_REGRESSION', 'LIGHTGBM')),
    horizon_minutes SMALLINT NOT NULL CHECK (horizon_minutes IN (5, 10, 15, 20)),
    model_version TEXT NOT NULL,
    dataset_revision_id UUID NOT NULL REFERENCES dataset_revisions(id),
    feature_set_version TEXT NOT NULL REFERENCES feature_set_definitions(version),
    artifact_path TEXT NOT NULL,
    artifact_checksum TEXT NOT NULL,
    manifest JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS training_runs (
    training_manifest_checksum TEXT PRIMARY KEY,
    dataset_revision_id UUID NOT NULL REFERENCES dataset_revisions(id),
    horizon_minutes SMALLINT NOT NULL CHECK (horizon_minutes IN (5, 10, 15, 20)),
    selected_model_checksum TEXT NOT NULL REFERENCES model_definitions(model_checksum),
    manifest JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS model_comparisons (
    comparison_checksum TEXT PRIMARY KEY,
    training_manifest_checksum TEXT NOT NULL REFERENCES training_runs(training_manifest_checksum),
    results JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS model_bundles (
    bundle_checksum TEXT PRIMARY KEY,
    dataset_revision_id UUID NOT NULL REFERENCES dataset_revisions(id),
    status TEXT NOT NULL CHECK (status IN ('CANDIDATE', 'REJECTED')),
    definition JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS model_bundle_members (
    bundle_checksum TEXT NOT NULL REFERENCES model_bundles(bundle_checksum),
    horizon_minutes SMALLINT NOT NULL CHECK (horizon_minutes IN (5, 10, 15, 20)),
    model_checksum TEXT NOT NULL REFERENCES model_definitions(model_checksum),
    PRIMARY KEY (bundle_checksum, horizon_minutes),
    UNIQUE (bundle_checksum, model_checksum)
);
