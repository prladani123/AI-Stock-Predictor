from __future__ import annotations

from banknifty_foundation import cli
from banknifty_foundation.cli import (
    build_parser,
    build_phase45_fixture_repository,
    main,
    run_phase07_fixture,
)
from banknifty_foundation.config import Settings
from banknifty_foundation.ml_models import ModelArtifactStore
from banknifty_foundation.ml_walk_forward import DailyMLWalkForward, DailyWalkForwardConfig
from dataclasses import replace
from datetime import date


def test_full_phase07_fixture_is_daily_minute_level_four_horizon_and_deterministic(tmp_path):
    first = run_phase07_fixture(tmp_path / "first.json", tmp_path / "models")
    second = run_phase07_fixture(tmp_path / "second.json", tmp_path / "models")
    assert first == second
    assert first["mode"] == "SYNTHETIC_FIXTURE_VERIFICATION_ONLY"
    assert first["zerodha_network_calls"] == first["real_broker_calls"] == 0
    assert first["minute_by_minute"] is True
    assert first["four_horizons_per_eligible_minute"] is True
    assert first["outer_test_labels_hidden_until_outcome_pass"] is True
    assert [item["test_date"] for item in first["folds"]] == [
        "2024-07-23", "2024-07-24"
    ]
    assert all(item["prediction_count"] == item["eligible_minutes"] * 4 for item in first["folds"])
    assert all(item["predictions_per_eligible_minute"] == 4 for item in first["folds"])
    assert first["prediction_count"] > 8
    assert first["prediction_count"] == first["outcome_count"]
    assert len(first["training"]) == len(first["calibration"]) == 8
    assert all(
        item["fit_range"]["to"] < item["selection_range"]["from"]
        for item in first["calibration"]
    )
    assert (tmp_path / "first.json").read_bytes() == (tmp_path / "second.json").read_bytes()


def test_each_test_day_has_its_own_pretest_context(tmp_path):
    report = run_phase07_fixture(tmp_path / "report.json", tmp_path / "models")
    first, second = report["folds"]
    assert first["partitions"] != second["partitions"]
    assert first["calibration_selection_dates"] == ["2024-07-22"]
    assert second["calibration_selection_dates"] == ["2024-07-23"]
    assert "2024-07-23" not in first["partitions"]["train_dates"]
    assert "2024-07-24" not in second["partitions"]["train_dates"]
    assert all(
        item["test_partition_accessed_during_training"] is False
        for item in report["training"]
    )


def test_fixture_and_production_cli_paths_are_explicitly_separate():
    parser = build_parser()
    fixture = parser.parse_args(["fixture-phase0-7"])
    production = parser.parse_args([
        "run-model-walk-forward",
        "--revision", "00000000-0000-0000-0000-000000000001",
        "--instrument-id", "1",
        "--test-date", "2024-07-24",
    ])
    assert fixture.func.__name__ == "command_phase07_fixture"
    assert production.func.__name__ == "command_model_walk_forward"
    assert production.revision.endswith("0001")


def test_fixture_report_inspection_commands_remain_read_only(tmp_path, capsys):
    report = tmp_path / "report.json"
    models = tmp_path / "models"
    payload = run_phase07_fixture(report, models)
    assert main(["compare-models", "--report", str(report)]) == 0
    assert main([
        "inspect-calibration", "--report", str(report), "--horizon", "20"
    ]) == 0
    selected = payload["training"][0]["candidate_artifacts"]
    key = (
        "lightgbm"
        if payload["training"][0]["selected_algorithm"] == "LIGHTGBM"
        else "logistic"
    )
    assert main([
        "inspect-model", "--artifact-dir", str(models),
        "--manifest", selected[key]["manifest_path"],
    ]) == 0
    assert "model_version" in capsys.readouterr().out


def test_production_cli_runs_through_accepted_repository_abstraction(
    tmp_path, monkeypatch
):
    repository, revision_id, instrument_id = build_phase45_fixture_repository()
    monkeypatch.setattr(cli, "PostgresRepository", lambda *_args, **_kwargs: repository)
    monkeypatch.setattr(
        cli.Settings,
        "from_env",
        classmethod(lambda cls, **_kwargs: Settings(database_url="postgresql://fixture")),
    )
    output = tmp_path / "accepted.json"
    assert main([
        "run-model-walk-forward",
        "--revision", revision_id,
        "--instrument-id", str(instrument_id),
        "--test-date", "2024-07-23",
        "--test-date", "2024-07-24",
        "--minimum-training-days", "3",
        "--validation-days", "1",
        "--calibration-fit-days", "1",
        "--calibration-selection-days", "1",
        "--artifact-dir", str(tmp_path / "models"),
        "--output", str(output),
    ]) == 0
    payload = __import__("json").loads(output.read_text(encoding="utf-8"))
    assert payload["mode"] == "ACCEPTED_DATASET_POSTGRESQL"
    assert payload["prediction_count"] > 8


def test_outer_test_labels_cannot_change_models_calibrators_or_predictions(tmp_path):
    first_repo, revision_id, instrument_id = build_phase45_fixture_repository()
    second_repo, second_revision, second_instrument = build_phase45_fixture_repository()
    for key, label in list(second_repo.labels.items()):
        if label.as_of.date() == date(2024, 7, 24) and label.label in {"UP", "DOWN"}:
            second_repo.labels[key] = replace(
                label, label="DOWN" if label.label == "UP" else "UP"
            )
    config = DailyWalkForwardConfig(
        test_dates=(date(2024, 7, 24),),
        minimum_training_days=3,
        validation_days=1,
        calibration_fit_days=1,
        calibration_selection_days=1,
    )
    first = DailyMLWalkForward(
        first_repo, ModelArtifactStore(tmp_path / "first-models")
    ).run(revision_id, instrument_id, config)
    second = DailyMLWalkForward(
        second_repo, ModelArtifactStore(tmp_path / "second-models")
    ).run(second_revision, second_instrument, config)
    assert [
        item["training_manifest_checksum"] for item in first["training"]
    ] == [
        item["training_manifest_checksum"] for item in second["training"]
    ]
    assert [item["checksum"] for item in first["calibration"]] == [
        item["checksum"] for item in second["calibration"]
    ]
    assert list(first_repo.ml_predictions.values()) == list(
        second_repo.ml_predictions.values()
    )
