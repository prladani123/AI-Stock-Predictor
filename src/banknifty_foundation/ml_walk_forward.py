from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Sequence

import numpy as np

from .calibration import AbstentionPolicy, apply_abstention_policy, fit_calibrator
from .features import FEATURE_NAMES, FEATURE_SET_VERSION
from .ml_models import CLASSES, ModelArtifactStore, classification_metrics, rows_to_matrix
from .ml_records import MLPrediction, MLPredictionOutcome
from .model_training import (
    ChronologicalPartitions,
    HorizonTrainingService,
    create_model_bundle,
    join_available_rows,
)
from .repository import Repository, digest

HORIZONS = (5, 10, 15, 20)


@dataclass(frozen=True)
class DailyWalkForwardConfig:
    test_dates: tuple[date, ...]
    minimum_training_days: int = 3
    validation_days: int = 1
    calibration_fit_days: int = 1
    calibration_selection_days: int = 1
    purge_minutes: int = 20
    seed: int = 1729
    threads: int = 2

    def __post_init__(self) -> None:
        if not self.test_dates or tuple(sorted(set(self.test_dates))) != self.test_dates:
            raise ValueError("test dates must be unique and chronological")
        if self.minimum_training_days < 1:
            raise ValueError("minimum training days must be positive")
        if min(
            self.validation_days,
            self.calibration_fit_days,
            self.calibration_selection_days,
        ) < 1:
            raise ValueError("validation and both calibration intervals are required")
        if self.purge_minutes < max(HORIZONS):
            raise ValueError("purge must cover the maximum target horizon")
        if not 1 <= self.threads <= 2:
            raise ValueError("ML threads must be between 1 and 2")


def build_daily_partitions(
    open_dates: Sequence[date], test_date: date, config: DailyWalkForwardConfig
) -> tuple[ChronologicalPartitions, tuple[date, ...], tuple[date, ...]]:
    prior = tuple(item for item in sorted(open_dates) if item < test_date)
    reserved = (
        config.validation_days
        + config.calibration_fit_days
        + config.calibration_selection_days
    )
    if len(prior) < config.minimum_training_days + reserved:
        raise ValueError(f"insufficient pre-test sessions for {test_date.isoformat()}")
    train = prior[:-reserved]
    validation_end = len(prior) - (
        config.calibration_fit_days + config.calibration_selection_days
    )
    validation = prior[
        validation_end - config.validation_days : validation_end
    ]
    fit_end = len(prior) - config.calibration_selection_days
    calibration_fit = prior[
        fit_end - config.calibration_fit_days : fit_end
    ]
    calibration_selection = prior[-config.calibration_selection_days :]
    partitions = ChronologicalPartitions(
        train,
        validation,
        calibration_fit + calibration_selection,
        (test_date,),
        config.purge_minutes,
    )
    return partitions, calibration_fit, calibration_selection


def _records_on_dates(records: Sequence[Any], dates: Sequence[date]) -> list[Any]:
    allowed = set(dates)
    return [item for item in records if item[0].as_of.date() in allowed]


def _feature_in_training_range(
    matrix: np.ndarray, manifest: dict[str, Any]
) -> bool:
    minimum = np.asarray(manifest["training_feature_min"], dtype=float)
    maximum = np.asarray(manifest["training_feature_max"], dtype=float)
    return bool(np.all(matrix >= minimum) and np.all(matrix <= maximum))


class DailyMLWalkForward:
    """Day-by-day retraining and point-in-time minute replay for all horizons."""

    def __init__(
        self,
        repository: Repository,
        artifact_store: ModelArtifactStore,
        *,
        policy: AbstentionPolicy | None = None,
    ) -> None:
        self.repository = repository
        self.artifact_store = artifact_store
        self.policy = policy or AbstentionPolicy()

    def run(
        self,
        revision_id: str,
        instrument_id: int,
        config: DailyWalkForwardConfig,
    ) -> dict[str, Any]:
        revision = self.repository.get_revision(revision_id)
        if revision.status != "ACCEPTED":
            raise ValueError("walk-forward ML requires an ACCEPTED dataset revision")
        sessions = [
            item
            for item in self.repository.sessions_between(
                revision.session_start, revision.session_end
            )
            if item.status in {"OPEN", "SPECIAL"}
        ]
        open_dates = tuple(item.session_date for item in sessions)
        if any(item not in open_dates for item in config.test_dates):
            raise ValueError("every test date must be an explicitly stored open session")
        all_features = self.repository.feature_rows(
            revision_id, instrument_id, FEATURE_SET_VERSION
        )
        all_labels = self.repository.target_labels(revision_id, instrument_id)
        policy_saved = self.repository.save_abstention_policy(
            self.policy.version, self.policy.checksum, self.policy.__dict__
        )
        folds: list[dict[str, Any]] = []
        all_training_manifests: list[dict[str, Any]] = []
        all_calibrations: list[dict[str, Any]] = []
        prediction_records: list[MLPrediction] = []
        outcome_records: list[MLPredictionOutcome] = []

        for test_date in config.test_dates:
            partitions, fit_dates, selection_dates = build_daily_partitions(
                open_dates, test_date, config
            )
            fold_id = f"ml-fold-{test_date.isoformat()}"
            fold_training: list[dict[str, Any]] = []
            fold_calibration: list[dict[str, Any]] = []
            model_context: dict[int, dict[str, Any]] = {}
            trainer = HorizonTrainingService(
                self.repository,
                self.artifact_store,
                seed=config.seed,
                threads=config.threads,
            )

            for horizon in HORIZONS:
                training = trainer.train(
                    revision_id,
                    instrument_id,
                    horizon,
                    partitions,
                    evaluate_test=False,
                )
                fold_training.append(training)
                all_training_manifests.append(training)
                selected_key = (
                    "lightgbm"
                    if training["selected_algorithm"] == "LIGHTGBM"
                    else "logistic"
                )
                selected_manifest = training["candidate_artifacts"][selected_key]
                self.repository.save_model_definition(selected_manifest)
                self.repository.save_training_manifest(training)
                comparison = {
                    "schema_version": "model-comparison-v1",
                    "training_manifest_checksum": training[
                        "training_manifest_checksum"
                    ],
                    "results": {
                        "majority_validation_metrics": training[
                            "majority_validation_metrics"
                        ],
                        "persistence_validation_metrics": training[
                            "persistence_validation_metrics"
                        ],
                        "logistic_validation_metrics": training[
                            "logistic_validation_metrics"
                        ],
                        "lightgbm_validation_metrics": training[
                            "lightgbm_validation_metrics"
                        ],
                        "validation_fold_stability": training[
                            "validation_fold_stability"
                        ],
                        "selected_algorithm": training["selected_algorithm"],
                        "selection_reason": training["selection_reason"],
                    },
                }
                comparison["comparison_checksum"] = digest(comparison)
                self.repository.save_model_comparison(comparison)
                model = self.artifact_store.load(
                    selected_manifest["manifest_path"],
                    expected_horizon=horizon,
                    expected_algorithm=training["selected_algorithm"],
                )

                eligible = join_available_rows(
                    self.repository,
                    revision_id,
                    instrument_id,
                    horizon,
                    before_date=test_date,
                )
                fit_records = _records_on_dates(eligible, fit_dates)
                selection_records = _records_on_dates(eligible, selection_dates)
                if not fit_records or not selection_records:
                    raise ValueError("calibration fit/selection records are required")
                fit_x = rows_to_matrix([item[0] for item in fit_records])
                selection_x = rows_to_matrix([item[0] for item in selection_records])
                calibrator, calibration = fit_calibrator(
                    model.predict_proba(fit_x),
                    [item[1] for item in fit_records],
                    model.predict_proba(selection_x),
                    [item[1] for item in selection_records],
                    fit_timestamps=[item[0].as_of for item in fit_records],
                    selection_timestamps=[item[0].as_of for item in selection_records],
                    model_checksum=selected_manifest["model_checksum"],
                    horizon_minutes=horizon,
                    seed=config.seed,
                    minimum_fit_samples=10,
                    minimum_selection_samples=10,
                    minimum_class_samples=1,
                )
                self.repository.save_calibration_run(calibration.to_dict())
                calibration_dict = calibration.to_dict()
                fold_calibration.append(calibration_dict)
                all_calibrations.append(calibration_dict)
                model_context[horizon] = {
                    "model": model,
                    "model_manifest": selected_manifest,
                    "training": training,
                    "calibrator": calibrator,
                    "calibration": calibration,
                }

            bundle = create_model_bundle(revision_id, fold_training)
            self.repository.save_model_bundle(bundle)
            test_rows = [
                row
                for row in all_features
                if row.as_of.date() == test_date
                and row.quality_status == "AVAILABLE"
                and row.source_max_timestamp <= row.as_of
                and all(row.values.get(name) is not None for name in FEATURE_NAMES)
            ]
            test_rows.sort(key=lambda item: item.as_of)
            fold_predictions: list[MLPrediction] = []

            # No test label is consulted in this inference loop.
            for row in test_rows:
                matrix = rows_to_matrix([row])
                for horizon in HORIZONS:
                    context = model_context[horizon]
                    raw = context["model"].predict_proba(matrix)[0]
                    calibrator = context["calibrator"]
                    calibrated = (
                        calibrator.transform(raw.reshape(1, -1))[0]
                        if calibrator is not None
                        else None
                    )
                    decision = apply_abstention_policy(
                        raw_probabilities=raw.tolist(),
                        calibrated_probabilities=(
                            calibrated.tolist() if calibrated is not None else None
                        ),
                        reliability_buckets=context[
                            "calibration"
                        ].reliability_buckets,
                        policy=self.policy,
                        calibrator_ready=calibrator is not None,
                        partial_lookback=False,
                        data_quality_failure=False,
                        schema_matches=True,
                        artifact_checksum_valid=True,
                        values_in_range=_feature_in_training_range(
                            matrix, context["model_manifest"]
                        ),
                        session_known=True,
                        unresolved_gap=False,
                    )
                    prediction_id = "mlpred-" + digest(
                        {
                            "fold": fold_id,
                            "model": context["model_manifest"]["model_checksum"],
                            "as_of": row.as_of,
                            "horizon": horizon,
                            "policy": self.policy.checksum,
                        }
                    )[:28]
                    prediction = MLPrediction(
                        prediction_id,
                        revision_id,
                        FEATURE_SET_VERSION,
                        context["model_manifest"]["model_checksum"],
                        context["calibration"].checksum,
                        self.policy.checksum,
                        context["training"]["training_manifest_checksum"],
                        fold_id,
                        instrument_id,
                        horizon,
                        row.as_of,
                        row.as_of + timedelta(minutes=horizon),
                        decision["raw_probabilities"],
                        decision["calibrated_probabilities"],
                        decision["raw_class"],
                        decision["calibrated_class"],
                        decision["final_class"],
                        decision["system_abstained"],
                        decision["abstention_reasons"],
                        decision["reliability_bucket"],
                        row.as_of,
                    )
                    fold_predictions.append(prediction)

            self.repository.save_ml_predictions(fold_predictions)
            prediction_records.extend(fold_predictions)

            # Outcomes are a separate post-expiry pass. They cannot affect inference.
            label_lookup = {
                (item.as_of, item.horizon_minutes): item
                for item in all_labels
                if item.as_of.date() == test_date
            }
            for prediction in fold_predictions:
                label = label_lookup.get(
                    (prediction.as_of, prediction.horizon_minutes)
                )
                actual = label.label if label is not None else "UNKNOWN"
                quality = label.quality_status if label is not None else "MISSING_LABEL"
                outcome = MLPredictionOutcome(
                    prediction.id, actual, quality, prediction.expires_at
                )
                self.repository.append_ml_prediction_outcome(outcome)
                outcome_records.append(outcome)

            fold_metrics: dict[str, Any] = {}
            for horizon in HORIZONS:
                horizon_predictions = [
                    item
                    for item in fold_predictions
                    if item.horizon_minutes == horizon
                ]
                resolved = [
                    (
                        item,
                        label_lookup.get((item.as_of, item.horizon_minutes)),
                    )
                    for item in horizon_predictions
                ]
                resolved = [
                    (prediction, label)
                    for prediction, label in resolved
                    if label is not None and label.label in CLASSES
                ]
                if resolved:
                    probabilities = np.asarray(
                        [
                            [
                                (
                                    prediction.calibrated_probabilities
                                    or prediction.raw_probabilities
                                )[name]
                                for name in CLASSES
                            ]
                            for prediction, _ in resolved
                        ]
                    )
                    fold_metrics[str(horizon)] = classification_metrics(
                        [label.label for _, label in resolved], probabilities
                    )
            folds.append(
                {
                    "fold_id": fold_id,
                    "test_date": test_date.isoformat(),
                    "partitions": partitions.to_dict(),
                    "calibration_fit_dates": [
                        item.isoformat() for item in fit_dates
                    ],
                    "calibration_selection_dates": [
                        item.isoformat() for item in selection_dates
                    ],
                    "training_manifest_checksums": {
                        str(item["horizon_minutes"]): item[
                            "training_manifest_checksum"
                        ]
                        for item in fold_training
                    },
                    "calibration_checksums": {
                        str(item["horizon_minutes"]): item["checksum"]
                        for item in fold_calibration
                    },
                    "bundle": bundle,
                    "eligible_minutes": len(test_rows),
                    "prediction_count": len(fold_predictions),
                    "predictions_per_eligible_minute": (
                        len(fold_predictions) // len(test_rows) if test_rows else 0
                    ),
                    "metrics": fold_metrics,
                }
            )

        report = {
            "schema_version": "daily-ml-walk-forward-v2",
            "dataset_revision_id": revision_id,
            "dataset_checksum": revision.checksum,
            "instrument_id": instrument_id,
            "feature_set_version": FEATURE_SET_VERSION,
            "feature_order": list(FEATURE_NAMES),
            "config": {
                "test_dates": [item.isoformat() for item in config.test_dates],
                "minimum_training_days": config.minimum_training_days,
                "validation_days": config.validation_days,
                "calibration_fit_days": config.calibration_fit_days,
                "calibration_selection_days": config.calibration_selection_days,
                "purge_minutes": config.purge_minutes,
                "seed": config.seed,
                "threads": config.threads,
            },
            "folds": folds,
            "training": all_training_manifests,
            "calibration": all_calibrations,
            "policy": {**self.policy.__dict__, "checksum": self.policy.checksum},
            "prediction_count": len(prediction_records),
            "outcome_count": len(outcome_records),
            "minute_by_minute": True,
            "four_horizons_per_eligible_minute": all(
                item["predictions_per_eligible_minute"] == 4 for item in folds
            ),
            "outer_test_labels_hidden_until_outcome_pass": True,
            "policy_inserted": policy_saved,
            "real_broker_calls": 0,
        }
        report["report_checksum"] = digest(report)
        return report
