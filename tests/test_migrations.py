from __future__ import annotations

from pathlib import Path


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
