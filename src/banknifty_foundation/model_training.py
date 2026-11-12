from __future__ import annotations

from dataclasses import dataclass
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any, Sequence

import numpy as np
import lightgbm
import sklearn

from .features import FEATURE_NAMES, FEATURE_SET_VERSION
from .ml_models import (
    CLASSES,
    LogisticResearchModel,
    ModelArtifactStore,
    classification_metrics,
    composite_score,
    fit_small_lightgbm_grid,
    rows_to_matrix,
)
from .repository import Repository, digest


@dataclass(frozen=True)
class ChronologicalPartitions:
    train_dates: tuple[date, ...]
    validation_dates: tuple[date, ...]
    calibration_dates: tuple[date, ...]
    test_dates: tuple[date, ...]
    purge_minutes: int = 20

    def __post_init__(self) -> None:
        groups = (self.train_dates, self.validation_dates, self.calibration_dates, self.test_dates)
        if any(not group for group in groups):
            raise ValueError("train, validation, calibration and test partitions are required")
        flattened = [item for group in groups for item in group]
        if flattened != sorted(flattened) or len(flattened) != len(set(flattened)):
            raise ValueError("partitions must be strictly chronological and disjoint")
        if self.purge_minutes < 20:
            raise ValueError("purge must be at least the maximum 20-minute horizon")

    def to_dict(self) -> dict[str, Any]:
        return {
            "train_dates": [item.isoformat() for item in self.train_dates],
            "validation_dates": [item.isoformat() for item in self.validation_dates],
            "calibration_dates": [item.isoformat() for item in self.calibration_dates],
            "test_dates": [item.isoformat() for item in self.test_dates],
            "purge_minutes_at_each_boundary": self.purge_minutes,
        }


def join_available_rows(
    repository: Repository,
    revision_id: str,
    instrument_id: int,
    horizon: int,
    *,
    before_date: date | None = None,
):
    revision = repository.get_revision(revision_id)
    if revision.status != "ACCEPTED":
        raise ValueError("models may train only from an ACCEPTED dataset revision")
    rows = repository.feature_rows(revision_id, instrument_id, FEATURE_SET_VERSION)
    labels = {
        (item.as_of, item.horizon_minutes): item
        for item in repository.target_labels(revision_id, instrument_id)
        if item.quality_status == "VALID" and item.label in CLASSES
    }
    result = []
    for row in rows:
        if before_date is not None and row.as_of.date() >= before_date:
            continue
        label = labels.get((row.as_of, horizon))
        if (
            label
            and row.quality_status == "AVAILABLE"
            and row.source_max_timestamp <= row.as_of
            and all(row.values.get(name) is not None for name in FEATURE_NAMES)
        ):
            result.append((row, label.label))
    return sorted(result, key=lambda item: item[0].as_of)


def _partition(records, allowed_dates: Sequence[date], purge_minutes: int, boundary: date | None):
    selected = [item for item in records if item[0].as_of.date() in set(allowed_dates)]
    if boundary is not None:
        if not selected:
            raise ValueError("partition has no eligible feature/label rows")
        cutoff = datetime.combine(
            boundary, datetime.min.time(), selected[0][0].as_of.tzinfo
        ) - timedelta(minutes=purge_minutes)
        selected = [item for item in selected if item[0].as_of < cutoff]
    if not selected:
        raise ValueError("partition has no eligible feature/label rows")
    return rows_to_matrix([item[0] for item in selected]), [item[1] for item in selected], selected


class HorizonTrainingService:
    """Trains one independent horizon without allowing test data into fitting or selection."""

    def __init__(self, repository: Repository, artifact_store: ModelArtifactStore, *, seed: int = 1729, threads: int = 2):
        self.repository = repository
        self.artifact_store = artifact_store
        self.seed = seed
        self.threads = threads

    def train(
        self,
        revision_id: str,
        instrument_id: int,
        horizon: int,
        partitions: ChronologicalPartitions,
        *,
        evaluate_test: bool = True,
    ) -> dict[str, Any]:
        if horizon not in {5, 10, 15, 20}:
            raise ValueError("unsupported model horizon")
        records = join_available_rows(
            self.repository,
            revision_id,
            instrument_id,
            horizon,
            before_date=None if evaluate_test else partitions.test_dates[0],
        )
        train_x, train_y, _ = _partition(records, partitions.train_dates, partitions.purge_minutes, partitions.validation_dates[0])
        valid_x, valid_y, valid_records = _partition(
            records, partitions.validation_dates, partitions.purge_minutes, partitions.calibration_dates[0]
        )
        calibration_x, calibration_y, calibration_records = _partition(
            records, partitions.calibration_dates, partitions.purge_minutes, partitions.test_dates[0]
        )
        if evaluate_test:
            test_x, test_y, test_records = _partition(
                records, partitions.test_dates, partitions.purge_minutes, None
            )
        else:
            test_x = np.empty((0, len(FEATURE_NAMES)))
            test_y = []
            test_records = []

        majority = max(CLASSES, key=lambda name: (train_y.count(name), -CLASSES.index(name)))
        majority_prob = np.zeros((len(valid_y), 3), dtype=float)
        majority_prob[:, CLASSES.index(majority)] = 1.0
        majority_metrics = classification_metrics(valid_y, majority_prob)
        persistence_prob = np.zeros((len(valid_y), 3), dtype=float)
        for index, (row, _) in enumerate(valid_records):
            value = float(row.values["return_5m"])
            predicted = "UP" if value > 0.001 else "DOWN" if value < -0.001 else "NO_TRADE"
            persistence_prob[index, CLASSES.index(predicted)] = 1.0
        persistence_metrics = classification_metrics(valid_y, persistence_prob)

        logistic = LogisticResearchModel(horizon, self.seed).fit(train_x, train_y)
        logistic_valid = classification_metrics(valid_y, logistic.predict_proba(valid_x))
        tree, grid = fit_small_lightgbm_grid(
            horizon, train_x, train_y, valid_x, valid_y, seed=self.seed, threads=self.threads
        )
        tree_valid = classification_metrics(valid_y, tree.predict_proba(valid_x))

        # Selection is validation-only. A tree must beat logistic by a visible margin;
        # otherwise the smaller linear model wins as the stability safeguard.
        logistic_score = composite_score(logistic_valid)
        tree_score = composite_score(tree_valid)
        validation_fold_stability: list[dict[str, Any]] = []
        for validation_date in partitions.validation_dates:
            indices = [
                index for index, (row, _) in enumerate(valid_records)
                if row.as_of.date() == validation_date
            ]
            if not indices:
                continue
            fold_y = [valid_y[index] for index in indices]
            logistic_fold = classification_metrics(
                fold_y, logistic.predict_proba(valid_x[indices])
            )
            tree_fold = classification_metrics(
                fold_y, tree.predict_proba(valid_x[indices])
            )
            validation_fold_stability.append({
                "date": validation_date.isoformat(),
                "logistic_composite": composite_score(logistic_fold),
                "lightgbm_composite": composite_score(tree_fold),
                "lightgbm_not_worse": composite_score(tree_fold) >= composite_score(logistic_fold),
            })
        tree_stable_improvement = (
            tree_score >= logistic_score + 0.01
            and bool(validation_fold_stability)
            and all(item["lightgbm_not_worse"] for item in validation_fold_stability)
        )
        selected = tree if tree_stable_improvement else logistic
        selection_reason = (
            "LIGHTGBM_STABLE_VALIDATION_IMPROVEMENT"
            if tree_stable_improvement
            else "LOGISTIC_PREFERRED_NO_STABLE_TREE_IMPROVEMENT"
        )
        lineage_metadata = {
            "dataset_revision_id": revision_id,
            "dataset_checksum": self.repository.get_revision(revision_id).checksum,
            "feature_set_version": FEATURE_SET_VERSION,
            "partitions": partitions.to_dict(),
            "training_row_count": len(train_y),
            "validation_row_count": len(valid_y),
            "class_counts_train": dict(sorted(Counter(train_y).items())),
            "training_feature_min": train_x.min(axis=0).tolist(),
            "training_feature_max": train_x.max(axis=0).tolist(),
            "code_version": "0.4.0",
        }
        logistic.lineage_metadata = lineage_metadata
        tree.lineage_metadata = lineage_metadata
        selected_manifest = selected.save(self.artifact_store)
        logistic_manifest = selected_manifest if selected is logistic else logistic.save(self.artifact_store)
        tree_manifest = selected_manifest if selected is tree else tree.save(self.artifact_store)

        # The untouched test partition is evaluated once, after model selection.
        test_metrics = (
            classification_metrics(test_y, selected.predict_proba(test_x))
            if evaluate_test
            else None
        )
        manifest = {
            "schema_version": "horizon-training-manifest-v1",
            "code_version": "0.4.0",
            "created_at_utc": (
                max(item[0].as_of for item in test_records).isoformat()
                if test_records
                else max(item[0].as_of for item in calibration_records).isoformat()
            ),
            "package_versions": {
                "numpy": np.__version__,
                "scikit_learn": sklearn.__version__,
                "lightgbm": lightgbm.__version__,
            },
            "dataset_revision_id": revision_id,
            "dataset_checksum": self.repository.get_revision(revision_id).checksum,
            "instrument_id": instrument_id,
            "horizon_minutes": horizon,
            "feature_set_version": FEATURE_SET_VERSION,
            "feature_schema": list(FEATURE_NAMES),
            "feature_schema_checksum": digest(FEATURE_NAMES),
            "partitions": partitions.to_dict(),
            "seed": self.seed,
            "max_threads": self.threads,
            "missing_value_policy": "EXCLUDE_UNAVAILABLE_NO_IMPUTATION",
            "class_balance_policy": "TRAIN_ONLY_BALANCED_WEIGHTS_LOGISTIC",
            "class_counts": {
                "train": dict(sorted(Counter(train_y).items())),
                "validation": dict(sorted(Counter(valid_y).items())),
                "calibration": dict(sorted(Counter(calibration_y).items())),
                "test": dict(sorted(Counter(test_y).items())),
            },
            "selection_partition": "VALIDATION_ONLY",
            "calibration_partition_used_for_training": False,
            "test_partition_accessed_during_training": evaluate_test,
            "test_partition_used_for_training_or_selection": False,
            "majority_validation_metrics": majority_metrics,
            "persistence_validation_metrics": persistence_metrics,
            "logistic_validation_metrics": logistic_valid,
            "lightgbm_validation_metrics": tree_valid,
            "lightgbm_grid": grid,
            "tree_stable_improvement": tree_stable_improvement,
            "validation_fold_stability": validation_fold_stability,
            "selection_reason": selection_reason,
            "selected_algorithm": selected.algorithm,
            "selected_model_checksum": selected_manifest["model_checksum"],
            "candidate_artifacts": {
                "logistic": logistic_manifest,
                "lightgbm": tree_manifest,
            },
            "untouched_test_metrics": test_metrics,
            "counts": {
                "train": len(train_y),
                "validation": len(valid_y),
                "calibration": len(calibration_y),
                "test": len(test_y),
            },
            "calibration_records": [
                {"as_of": row.as_of.isoformat(), "actual": actual}
                for row, actual in calibration_records
            ],
            "test_records": [
                {"as_of": row.as_of.isoformat(), "actual": actual}
                for row, actual in test_records
            ],
        }
        manifest["training_manifest_checksum"] = digest(manifest)
        return manifest


def create_model_bundle(revision_id: str, horizon_manifests: Sequence[dict[str, Any]], *, status: str = "CANDIDATE") -> dict[str, Any]:
    if status not in {"CANDIDATE", "REJECTED"}:
        raise ValueError("research model bundles cannot be promoted beyond CANDIDATE")
    by_horizon = {int(item["horizon_minutes"]): item for item in horizon_manifests}
    if set(by_horizon) != {5, 10, 15, 20}:
        raise ValueError("a bundle must contain exactly one model for each required horizon")
    if any(item["dataset_revision_id"] != revision_id for item in by_horizon.values()):
        raise ValueError("bundle model dataset lineage mismatch")
    bundle = {
        "schema_version": "model-bundle-v1",
        "status": status,
        "dataset_revision_id": revision_id,
        "models": {
            str(horizon): {
                "algorithm": by_horizon[horizon]["selected_algorithm"],
                "model_checksum": by_horizon[horizon]["selected_model_checksum"],
            }
            for horizon in sorted(by_horizon)
        },
    }
    bundle["bundle_checksum"] = digest(bundle)
    return bundle
