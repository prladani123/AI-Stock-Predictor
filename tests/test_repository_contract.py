import inspect

from banknifty_foundation.repository import MemoryRepository, PostgresRepository, Repository


def test_memory_and_postgres_expose_the_complete_repository_protocol():
    required = {
        name
        for name, value in Repository.__dict__.items()
        if callable(value) and not name.startswith("_")
    }
    assert not required - set(dir(MemoryRepository))
    assert not required - set(dir(PostgresRepository))


def test_postgres_phase4_to7_methods_are_defined_on_postgres_class():
    for name in (
        "save_feature_rows", "save_predictions", "save_model_definition",
        "save_training_manifest", "save_model_bundle", "save_calibration_run",
        "save_abstention_policy", "save_ml_predictions",
        "append_ml_prediction_outcome",
    ):
        assert name in PostgresRepository.__dict__
