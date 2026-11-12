from __future__ import annotations

import os
from pathlib import Path
import uuid

import pytest

from banknifty_foundation.repository import PostgresRepository


def test_phase_migration_contains_all_required_tables_and_utc_types():
    migration = (
        Path(__file__).resolve().parents[1] / "migrations" / "001_phase_0_1.sql"
    ).read_text(encoding="utf-8").lower()
    for table in (
        "trading_sessions",
        "instrument_snapshots",
        "canonical_instruments",
        "historical_import_jobs",
        "one_minute_candles",
        "quality_issues",
    ):
        assert f"create table if not exists {table}" in migration
    assert "timestamptz" in migration
    assert "on conflict(instrument_id, candle_at) do nothing" not in migration  # upsert lives in repository


def test_phase_2_and_3_migrations_enforce_immutable_revisions_and_labels():
    root = Path(__file__).resolve().parents[1] / "migrations"
    phase_2 = (root / "002_accepted_datasets.sql").read_text(encoding="utf-8").lower()
    phase_3 = (root / "003_target_labels.sql").read_text(encoding="utf-8").lower()
    for table in ("dataset_revisions", "dataset_revision_instruments", "dataset_revision_candles"):
        assert f"create table if not exists {table}" in phase_2
    assert "immutable_dataset_revision" in phase_2
    assert "immutable_revision_candles" in phase_2
    for table in ("threshold_policies", "target_labels"):
        assert f"create table if not exists {table}" in phase_3
    assert "accepted_revision_labels" in phase_3
    assert "immutable_target_labels" in phase_3


def test_phase_4_and_5_migrations_enforce_point_in_time_and_prediction_immutability():
    root = Path(__file__).resolve().parents[1] / "migrations"
    phase_4 = (root / "004_point_in_time_features.sql").read_text(encoding="utf-8").lower()
    phase_5 = (root / "005_walk_forward_replay.sql").read_text(encoding="utf-8").lower()
    for table in ("feature_set_definitions", "feature_rows"):
        assert f"create table if not exists {table}" in phase_4
    assert "source_max_timestamp <= as_of" in phase_4
    assert "immutable_feature_rows" in phase_4
    for table in (
        "baseline_definitions",
        "research_runs",
        "walk_forward_folds",
        "research_predictions",
        "prediction_outcomes",
        "evaluation_report_manifests",
    ):
        assert f"create table if not exists {table}" in phase_5
    assert "immutable_research_predictions" in phase_5
    assert "immutable_prediction_outcomes" in phase_5


def test_phase_6_and_7_migrations_cover_models_calibration_lineage_and_delayed_outcomes():
    root = Path(__file__).resolve().parents[1] / "migrations"
    phase_6 = (root / "006_phase6_models.sql").read_text(encoding="utf-8").lower()
    phase_7 = (root / "007_phase7_calibration_predictions.sql").read_text(encoding="utf-8").lower()
    for table in (
        "model_definitions", "training_runs", "model_comparisons",
        "model_bundles", "model_bundle_members",
    ):
        assert f"create table if not exists {table}" in phase_6
    assert "check (status in ('candidate', 'rejected'))" in phase_6
    for table in (
        "calibration_runs", "abstention_policies",
        "ml_predictions", "ml_prediction_outcomes",
    ):
        assert f"create table if not exists {table}" in phase_7
    assert "enforce_ml_outcome_after_expiry" in phase_7
    assert "raw_probabilities" in phase_7
    assert "calibrated_probabilities" in phase_7


def test_integrity_repair_migration_makes_every_phase67_record_immutable():
    migration = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "008_phase0_7_integrity_repair.sql"
    ).read_text(encoding="utf-8").lower()
    assert "calibration_reliability_buckets" in migration
    assert "system_abstained" in migration
    assert "calibrated_class" in migration
    for trigger in (
        "immutable_model_definitions",
        "immutable_training_runs",
        "immutable_model_comparisons",
        "immutable_model_bundles",
        "immutable_model_bundle_members",
        "immutable_calibration_runs",
        "immutable_calibration_buckets",
        "immutable_abstention_policies",
        "immutable_ml_predictions",
        "immutable_ml_prediction_outcomes",
    ):
        assert trigger in migration


def test_apply_migrations_fails_when_directory_missing_or_empty(tmp_path):
    repository = object.__new__(PostgresRepository)
    repository.migration_dir = tmp_path / "missing"
    repository._connection = None
    with pytest.raises(ValueError, match="does not exist"):
        PostgresRepository.apply_migrations(repository)
    repository.migration_dir = tmp_path / "empty"
    repository.migration_dir.mkdir()
    with pytest.raises(ValueError, match="no .sql files"):
        PostgresRepository.apply_migrations(repository)


def test_migration_022_runtime_constraints_when_postgres_available():
    database_url = os.getenv("TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("Set TEST_POSTGRES_URL to run migration 022 runtime checks")
    psycopg = pytest.importorskip("psycopg")
    migration = (Path(__file__).resolve().parents[1] / "migrations"
                 / "022_experimental_research_identity.sql").read_text(encoding="utf-8")
    schema = f"migration022_{uuid.uuid4().hex[:12]}"
    revision = uuid.uuid4()
    replay = uuid.uuid4()
    with psycopg.connect(database_url) as db:
        db.execute(f'CREATE SCHEMA "{schema}"')
        db.execute(f'SET search_path TO "{schema}"')
        db.execute("CREATE TABLE schema_migrations(version text PRIMARY KEY, applied_at timestamptz DEFAULT now())")
        db.execute("CREATE TABLE dataset_revisions(id uuid PRIMARY KEY, status text NOT NULL)")
        db.execute("CREATE TABLE big_move_predictions(prediction_id text PRIMARY KEY, instrument_id bigint NOT NULL, as_of timestamptz NOT NULL)")
        db.execute("""CREATE TABLE big_move_evidence_journal(
            prediction_id text NOT NULL,
            market_as_of timestamptz NOT NULL,
            decision_at timestamptz NOT NULL)""")
        db.execute("""CREATE TABLE experimental_model_outputs(
            output_id text PRIMARY KEY,
            dataset_revision_id uuid NOT NULL,
            instrument_id bigint NOT NULL,
            as_of timestamptz NOT NULL,
            model_name text NOT NULL,
            model_version text NOT NULL,
            pred_len integer NOT NULL DEFAULT 30,
            forecast_path jsonb NOT NULL DEFAULT '[]'::jsonb,
            summary jsonb NOT NULL DEFAULT '{}'::jsonb,
            status text NOT NULL DEFAULT 'EXPERIMENTAL_MODEL_OUTPUT')""")
        db.execute("CREATE OR REPLACE FUNCTION reject_big_move_v2_mutation() RETURNS trigger AS $$ BEGIN RAISE EXCEPTION 'IMMUTABLE'; END; $$ LANGUAGE plpgsql")
        db.execute(migration)
        db.execute("INSERT INTO dataset_revisions(id, status) VALUES (%s, 'ACCEPTED'), (%s, 'ACCEPTED')", (revision, replay))
        as_of = "2026-01-05T04:00:00+00:00"
        decision_at = "2026-01-05T04:01:00+00:00"
        db.execute("INSERT INTO big_move_predictions(prediction_id, instrument_id, as_of) VALUES ('p1', 1, %s)", (as_of,))
        db.execute("INSERT INTO big_move_evidence_journal(prediction_id, market_as_of, decision_at) VALUES ('p1', %s, %s)", (as_of, decision_at))

        db.execute("""INSERT INTO experimental_model_outputs(
            output_id, dataset_revision_id, instrument_id, as_of, model_name, model_version,
            forecast_path, summary, status, identity_checksum, prediction_ids, replay_pool_revision_id,
            input_checksum, configuration_checksum, seed, path_count, sampling, available_at, computed_at,
            source_max_timestamp, point_in_time_status, decision_at)
            VALUES ('live-ok', NULL, 1, %s, 'KRONOS_MINI_ZERO_SHOT_V1', 'v', '[]'::jsonb,
            %s::jsonb, 'EXPERIMENTAL_MODEL_OUTPUT', 'id-live', '["p1"]'::jsonb, NULL, 'in', 'cfg',
            1, 1, '{}'::jsonb, %s, %s, %s, 'PROSPECTIVE_DECISION_AVAILABLE', %s)""",
            (as_of, '{"marketAsOf":"2026-01-05T04:00:00+00:00"}', decision_at, decision_at, as_of, decision_at))

        with pytest.raises(psycopg.Error):
            db.execute("""INSERT INTO experimental_model_outputs(
                output_id, dataset_revision_id, instrument_id, as_of, model_name, model_version,
                forecast_path, summary, status, identity_checksum, prediction_ids, replay_pool_revision_id,
                input_checksum, configuration_checksum, seed, path_count, sampling, available_at, computed_at,
                source_max_timestamp, point_in_time_status, decision_at)
                VALUES ('hist-bad', NULL, 1, %s, 'KRONOS_MINI_ZERO_SHOT_V1', 'v', '[]'::jsonb,
                %s::jsonb, 'EXPERIMENTAL_MODEL_OUTPUT', 'id-hist', '[]'::jsonb, NULL, 'in', 'cfg',
                1, 1, '{}'::jsonb, %s, %s, %s, 'HISTORICAL_COUNTERFACTUAL_RESEARCH', NULL)""",
                (as_of, '{"marketAsOf":"2026-01-05T04:00:00+00:00"}', decision_at, decision_at, as_of))

        with pytest.raises(psycopg.Error):
            db.execute("""INSERT INTO experimental_model_outputs(
                output_id, dataset_revision_id, instrument_id, as_of, model_name, model_version,
                forecast_path, summary, status, identity_checksum, prediction_ids, replay_pool_revision_id,
                input_checksum, configuration_checksum, seed, path_count, sampling, available_at, computed_at,
                source_max_timestamp, point_in_time_status)
                VALUES ('tm-bad', %s, 1, %s, 'BANKNIFTY_TIME_MACHINE_V1', 'v', '[]'::jsonb,
                %s::jsonb, 'EXPERIMENTAL_MODEL_OUTPUT', 'id-tm', '[]'::jsonb, NULL, 'in', 'cfg',
                1, 1, '{}'::jsonb, %s, %s, %s, 'HISTORICAL_COUNTERFACTUAL_RESEARCH')""",
                (revision, as_of, '{"marketAsOf":"2026-01-05T04:00:00+00:00"}', decision_at, decision_at, as_of))

        with pytest.raises(psycopg.Error):
            db.execute("UPDATE experimental_model_outputs SET model_version='changed' WHERE output_id='live-ok'")
