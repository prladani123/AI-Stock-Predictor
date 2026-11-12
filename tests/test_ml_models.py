from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import inspect
import json

import numpy as np
import pytest

from banknifty_foundation.features import FEATURE_NAMES
from banknifty_foundation.ml_models import (
    CLASSES,
    LightGBMResearchModel,
    LogisticResearchModel,
    ModelArtifactStore,
    classification_metrics,
    fit_small_lightgbm_grid,
)
from banknifty_foundation.model_training import ChronologicalPartitions, create_model_bundle


def _data(samples: int = 120):
    rng = np.random.default_rng(1729)
    x = rng.normal(size=(samples, len(FEATURE_NAMES)))
    score = x[:, 0] + 0.7 * x[:, 1]
    y = np.where(score > 0.45, "UP", np.where(score < -0.45, "DOWN", "NO_TRADE"))
    return x, y


def test_logistic_common_interface_is_deterministic_and_probabilities_are_ordered():
    x, y = _data()
    first = LogisticResearchModel(5).fit(x, y)
    second = LogisticResearchModel(5).fit(x, y)
    assert first.metadata()["feature_schema"] == list(FEATURE_NAMES)
    assert np.allclose(first.predict_proba(x), second.predict_proba(x))
    assert set(first.predict(x)) <= set(CLASSES)
    assert np.allclose(first.predict_proba(x).sum(axis=1), 1)


def test_logistic_rejects_partial_rows_and_schema_mismatch():
    x, y = _data()
    x[0, 0] = np.nan
    with pytest.raises(ValueError, match="finite"):
        LogisticResearchModel(5).fit(x, y)
    with pytest.raises(ValueError, match="schema"):
        LogisticResearchModel(5).fit(np.zeros((30, 2)), ["UP", "DOWN", "NO_TRADE"] * 10)


def test_estimators_accept_only_bounded_matrices_and_metadata():
    for model_type in (LogisticResearchModel, LightGBMResearchModel):
        source = inspect.getsource(model_type)
        assert "repository" not in source.lower()
        assert "connection" not in source.lower()
        fit_parameters = tuple(inspect.signature(model_type.fit).parameters)
        assert fit_parameters[:3] == ("self", "x", "y")
        assert set(fit_parameters) <= {
            "self", "x", "y", "validation_x", "validation_y", "_"
        }


def test_training_requires_all_three_classes():
    x, _ = _data(30)
    with pytest.raises(ValueError, match="three"):
        LogisticResearchModel(5).fit(x, ["UP", "DOWN"] * 15)


def test_safe_logistic_artifact_round_trip_and_tamper_failure(tmp_path):
    x, y = _data()
    store = ModelArtifactStore(tmp_path)
    manifest = LogisticResearchModel(10).fit(x, y).save(store)
    loaded = store.load(manifest["manifest_path"])
    assert np.allclose(loaded.predict_proba(x[:5]), LogisticResearchModel(10).fit(x, y).predict_proba(x[:5]))
    artifact = tmp_path / manifest["artifact_path"]
    artifact.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="checksum"):
        store.load(manifest["manifest_path"])


def test_artifact_load_enforces_expected_horizon_algorithm_and_immutability(tmp_path):
    x, y = _data()
    store = ModelArtifactStore(tmp_path)
    model = LogisticResearchModel(10).fit(x, y)
    manifest = model.save(store)
    model.save(store)
    with pytest.raises(ValueError, match="horizon"):
        store.load(manifest["manifest_path"], expected_horizon=15)
    with pytest.raises(ValueError, match="algorithm"):
        store.load(manifest["manifest_path"], expected_algorithm="LIGHTGBM")
    artifact = tmp_path / manifest["artifact_path"]
    artifact.write_text("different", encoding="utf-8")
    with pytest.raises(ValueError, match="immutable"):
        model.save(store)


def test_artifact_store_rejects_traversal_untrusted_manifest_and_schema(tmp_path):
    store = ModelArtifactStore(tmp_path)
    with pytest.raises(ValueError, match="outside"):
        store.load("../model.json")
    (tmp_path / "bad.json").write_text('{"algorithm":"LOGISTIC_REGRESSION"}', encoding="utf-8")
    with pytest.raises(ValueError, match="untrusted"):
        store.load("bad.json")
    x, y = _data()
    manifest = LogisticResearchModel(15).fit(x, y).save(store)
    path = tmp_path / manifest["manifest_path"]
    content = json.loads(path.read_text())
    content["feature_schema"] = ["wrong"]
    path.write_text(json.dumps(content), encoding="utf-8")
    with pytest.raises(ValueError, match="checksum|schema"):
        store.load(path.name)


def test_no_pickle_or_joblib_artifact_is_accepted(tmp_path):
    store = ModelArtifactStore(tmp_path)
    (tmp_path / "foreign.joblib").write_bytes(b"\x80\x04untrusted")
    with pytest.raises(ValueError, match="outside"):
        store.load("foreign.joblib")


def test_lightgbm_threads_are_hard_bounded():
    with pytest.raises(ValueError, match="threads"):
        LightGBMResearchModel(5, {}, max_threads=3)


def test_small_lightgbm_grid_records_every_sequential_combination():
    x, y = _data()
    model, records = fit_small_lightgbm_grid(5, x[:80], y[:80], x[80:], y[80:], threads=1)
    assert len(records) == 3
    assert all(item["best_iteration"] > 0 for item in records)
    assert model.metadata()["max_threads"] == 1
    assert np.allclose(model.predict_proba(x[:4]).sum(axis=1), 1)


def test_classification_metrics_include_required_comparison_values():
    metrics = classification_metrics(["UP", "DOWN", "NO_TRADE"], np.eye(3)[[2, 0, 1]])
    assert metrics["accuracy"] == metrics["macro_f1"] == metrics["weighted_f1"] == 1.0
    assert metrics["log_loss"] == 0.0
    assert metrics["confusion_matrix"]["UP"]["UP"] == 1
    assert metrics["directional_coverage"] == pytest.approx(2 / 3)


def test_partitions_are_strict_disjoint_chronological_and_purged():
    good = ChronologicalPartitions(
        (date(2024, 7, 15),), (date(2024, 7, 16),), (date(2024, 7, 17),), (date(2024, 7, 18),)
    )
    assert good.to_dict()["purge_minutes_at_each_boundary"] == 20
    with pytest.raises(ValueError, match="chronological"):
        ChronologicalPartitions(
            (date(2024, 7, 16),), (date(2024, 7, 15),), (date(2024, 7, 17),), (date(2024, 7, 18),)
        )
    with pytest.raises(ValueError, match="at least"):
        replace(good, purge_minutes=19)


def _manifest(horizon: int, revision: str = "revision"):
    return {
        "horizon_minutes": horizon,
        "dataset_revision_id": revision,
        "selected_algorithm": "LOGISTIC_REGRESSION",
        "selected_model_checksum": f"model-{horizon}",
    }


def test_bundle_requires_exact_four_horizons_and_candidate_or_rejected_only():
    bundle = create_model_bundle("revision", [_manifest(h) for h in (5, 10, 15, 20)])
    assert set(bundle["models"]) == {"5", "10", "15", "20"}
    with pytest.raises(ValueError, match="exactly"):
        create_model_bundle("revision", [_manifest(h) for h in (5, 10, 15)])
    with pytest.raises(ValueError, match="promoted"):
        create_model_bundle("revision", [_manifest(h) for h in (5, 10, 15, 20)], status="ACCEPTED")
    with pytest.raises(ValueError, match="lineage"):
        create_model_bundle("revision", [_manifest(5, "other")] + [_manifest(h) for h in (10, 15, 20)])
