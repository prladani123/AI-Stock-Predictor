"""Chronological, matched-support evaluation for optional external research."""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from random import Random
from typing import Any

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, brier_score_loss, f1_score, log_loss

from .big_move_labels import BIG_MOVE_HORIZONS
from .calendar import IST
from .external_research import (KRONOS_MODEL_NAME, TIME_MACHINE_MODEL_NAME,
                                HISTORICAL_COUNTERFACTUAL, checksum_payload)

VALID_LABELS = {"BIG_UP", "BIG_DOWN", "NO_BIG_MOVE"}
SCORE_MODELS = ("PRICE_CORE_V2", "KRONOS", "TIME_MACHINE",
                "V2_KRONOS", "V2_TIME_MACHINE", "V2_KRONOS_TIME_MACHINE")
CLASS_INDEX = {"NO_BIG_MOVE": 0, "BIG_UP": 1, "BIG_DOWN": 2, "AMBIGUOUS": 3}


def _metrics(target: np.ndarray, probability: np.ndarray, threshold: float = 0.5) -> dict[str, Any]:
    probability = np.clip(np.asarray(probability, dtype=float), 0.0, 1.0)
    predicted = (probability >= threshold).astype(int)
    return {
        "samples": int(len(target)),
        "positives": int(target.sum()),
        "brier": float(brier_score_loss(target, probability)),
        "logLoss": float(log_loss(target, np.column_stack((1 - probability, probability)), labels=[0, 1])),
        "accuracy": float(accuracy_score(target, predicted)),
        "f1": float(f1_score(target, predicted, zero_division=0)),
        "threshold": float(threshold),
    }


def _joint(labels: list[str], meaningful: np.ndarray, ambiguous: np.ndarray,
           up_given: np.ndarray, down_given: np.ndarray) -> dict[str, Any]:
    meaningful = np.clip(meaningful.astype(float), 0.0, 1.0)
    ambiguous = np.clip(ambiguous.astype(float), 0.0, 1.0)
    up_given = np.clip(up_given.astype(float), 0.0, None)
    down_given = np.clip(down_given.astype(float), 0.0, None)
    normalizer = up_given + down_given
    up = np.where(normalizer > 0, up_given / np.where(normalizer > 0, normalizer, 1), 0.5)
    down = 1.0 - up
    probabilities = np.column_stack((
        np.clip(1.0 - meaningful, 0.0, 1.0),
        meaningful * (1.0 - ambiguous) * up,
        meaningful * (1.0 - ambiguous) * down,
        meaningful * ambiguous,
    ))
    probabilities = np.clip(probabilities, 1e-12, 1.0)
    probabilities /= probabilities.sum(axis=1, keepdims=True)
    actual = np.asarray([CLASS_INDEX[label] for label in labels], dtype=int)
    one_hot = np.eye(4)[actual]
    return {
        "samples": int(len(labels)),
        "accuracy": float(accuracy_score(actual, probabilities.argmax(axis=1))),
        "macroF1": float(f1_score(actual, probabilities.argmax(axis=1),
                                   labels=[0, 1, 2, 3], average="macro", zero_division=0)),
        "logLoss": float(-np.log(probabilities[np.arange(len(actual)), actual]).mean()),
        "multiClassBrier": float(((probabilities - one_hot) ** 2).sum(axis=1).mean()),
        "actualClassProbability": float(probabilities[np.arange(len(actual)), actual].mean()),
    }


def _magnitude(realized: np.ndarray, expected: np.ndarray,
               signed: np.ndarray, expected_return: np.ndarray) -> dict[str, Any]:
    if not len(realized):
        return {"samples": 0}
    error = expected - realized
    return {
        "samples": int(len(realized)),
        "mae": float(np.abs(error).mean()),
        "rmse": float(np.sqrt((error ** 2).mean())),
        "correlation": (float(np.corrcoef(expected, realized)[0, 1])
                        if len(realized) > 1 and np.std(expected) > 0 and np.std(realized) > 0
                        else None),
        "signedMae": (float(np.abs(expected_return - signed).mean())
                      if all(value is not None for value in expected_return) else None),
        "unit": "return_fraction",
        "target": "MAXIMUM_EXCURSION_ON_MEANINGFUL_MOVE_ROWS",
    }


def _paired_bootstrap(items: list[dict[str, Any]], target: np.ndarray,
                      primary: np.ndarray, comparator: np.ndarray) -> dict[str, Any]:
    days: dict[date, list[int]] = defaultdict(list)
    for index, item in enumerate(items):
        days[item["key"][0].astimezone(IST).date()].append(index)
    ordered = sorted(days)
    if len(ordered) < 5:
        return {"status": "INSUFFICIENT_EVIDENCE", "days": len(ordered)}
    random = Random(1729)
    differences = []
    for _ in range(1000):
        index = np.concatenate([np.asarray(days[ordered[random.randrange(len(ordered))]])
                                for _ in ordered])
        left = float(np.mean((primary[index] - target[index]) ** 2))
        right = float(np.mean((comparator[index] - target[index]) ** 2))
        differences.append(right - left)
    lower, upper = np.quantile(differences, [0.025, 0.975])
    return {"status": "PASS", "unit": "TRADING_DAY_BLOCK", "samples": 1000,
            "seed": 1729, "days": len(ordered),
            "brierImprovement": {"lower": float(lower), "upper": float(upper),
                                 "mean": float(np.mean(differences))}}


def _meta_model(rows: list[tuple[date, np.ndarray, int]], calibration: list[tuple[date, np.ndarray, int]]):
    if len({row[0].astimezone(IST).date() for row in rows}) < 3 or len(rows) < 20 or not calibration:
        return None, {"status": "INSUFFICIENT_TRAINING_ROWS"}
    x = np.asarray([row[1] for row in rows], dtype=float)
    y = np.asarray([row[2] for row in rows], dtype=int)
    if len(set(y.tolist())) < 2:
        return None, {"status": "INSUFFICIENT_TRAINING_CLASSES"}
    model = LogisticRegression(C=1.0, max_iter=500, random_state=1729).fit(x, y)
    calibration_p = model.predict_proba(np.asarray([row[1] for row in calibration], dtype=float))[:, 1]
    calibration_y = np.asarray([row[2] for row in calibration], dtype=int)
    best = (0.5, -1.0)
    for threshold in np.arange(0.10, 0.91, 0.05):
        score = f1_score(calibration_y, (calibration_p >= threshold).astype(int), zero_division=0)
        if score > best[1]:
            best = (float(threshold), float(score))
    return (model, best[0]), {"status": "COMPLETED",
                              "thresholdSelection": {"threshold": best[0], "f1": best[1]},
                              "coefficients": [float(value) for value in model.coef_[0]]}


class ExternalResearchEvaluationService:
    def __init__(self, repository: Any) -> None:
        self.repository = repository

    def _v2(self, revision_id: str, instrument_id: int) -> dict[tuple[Any, int], dict[str, float | None]]:
        # Reuse Stage D's estimator/calibration-fit/calibration-selection/threshold
        # separation. No production predictions, artifact writes or promotion.
        from .big_move_evaluation import EvaluationConfiguration, _load, _fold_fit, _ready, _valid
        from .big_move_runtime import BigMoveTrainingService, numeric
        service = BigMoveTrainingService(self.repository)
        rows, _ = _load(service, revision_id, instrument_id, "LOGISTIC_REGRESSION")
        config = EvaluationConfiguration()
        days = sorted({label.as_of.astimezone(IST).date() for _, label in rows})
        result = {}
        self.v2_folds = []
        for day in days[config.minimumTrainingDays:]:
            for horizon in BIG_MOVE_HORIZONS:
                prior = [(f, l) for f, l in rows if l.horizon_minutes == horizon
                         and l.expiry_at.astimezone(IST).date() < day]
                try:
                    model, metadata = _fold_fit(service, prior, horizon, "LOGISTIC_REGRESSION", config)
                except ValueError as error:
                    self.v2_folds.append({"day": str(day), "horizon": horizon,
                                          "status": "INSUFFICIENT_EVIDENCE", "reason": str(error)})
                    continue
                self.v2_folds.append({"day": str(day), "horizon": horizon,
                                      "modelVersion": model.model_version, "metadata": metadata})
                for feature, label in rows:
                    if (label.horizon_minutes != horizon or label.as_of.astimezone(IST).date() != day
                            or not _valid((feature, label)) or not _ready(feature, model.feature_schema)):
                        continue
                    p = model.predict([numeric(feature.values[n]) for n in model.feature_schema],
                                      float(label.reference_price))
                    result[(label.as_of, horizon)] = {
                        "meaningful": p["bigMoveProbability"], "up": p["upGivenBigMoveProbability"],
                        "down": p["downGivenBigMoveProbability"], "magnitude": p["expectedMovePercent"],
                        "return": None, "threshold": p["moveDecisionThreshold"],
                    }
        return result

    def evaluate(self, revision_id: str, instrument_id: int) -> dict[str, Any]:
        labels = {(item.as_of, item.horizon_minutes): item for item in
                  self.repository.big_move_labels(revision_id, instrument_id)
                  if item.quality_status == "VALID" and item.label in VALID_LABELS}
        if not labels:
            return {"available": False, "reason": "NO_VALID_LABELS", "researchOnly": True}
        kronos: dict[tuple[Any, int], dict[str, Any]] = {}
        time_machine: dict[tuple[Any, int], dict[str, Any]] = {}
        for model_name, target in ((KRONOS_MODEL_NAME, kronos),
                                   (TIME_MACHINE_MODEL_NAME, time_machine)):
            for row in self.repository.experimental_outputs_for(
                revision_id, instrument_id, model_name=model_name, limit=5000):
                summary = row["summary"] or {}
                if summary.get("researchStatus") != "COMPLETED":
                    continue
                if summary.get("pointInTimeStatus") != HISTORICAL_COUNTERFACTUAL:
                    continue
                for key, horizon in (summary.get("horizons") or {}).items():
                    identity = (row["as_of"], int(key))
                    if identity in target:
                        raise ValueError("Select one external configuration/seed per reference before evaluation")
                    target[identity] = horizon
        v2 = self._v2(revision_id, instrument_id)
        comparison_keys = sorted(set(labels) & set(kronos) & set(time_machine) & set(v2))
        if not comparison_keys:
            return {"available": False, "reason": "NO_MATCHED_REFERENCE_MINUTES",
                    "cohortCounts": {"labels": len(labels), "kronos": len(kronos),
                                     "timeMachine": len(time_machine), "priceCoreV2": len(v2)},
                    "researchOnly": True}
        by_date: dict[date, list[tuple[Any, int]]] = defaultdict(list)
        for key in comparison_keys:
            by_date[key[0].astimezone(IST).date()].append(key)
        dates = sorted(by_date)
        if len(dates) < 6:
            return {"available": False, "reason": "INSUFFICIENT_MATCHED_DATES",
                    "matchedReferenceMinutes": len(comparison_keys), "matchedDates": len(dates),
                    "researchOnly": True}
        split_a = max(1, int(len(dates) * 0.60))
        split_b = max(split_a + 1, int(len(dates) * 0.80))
        train_dates, calibration_dates, holdout_dates = dates[:split_a], dates[split_a:split_b], dates[split_b:]
        cohorts = {
            "train": [key for day in train_dates for key in by_date[day]],
            "calibration": [key for day in calibration_dates for key in by_date[day]],
            "holdout": [key for day in holdout_dates for key in by_date[day]],
        }

        def external(sample, name):
            value = sample.get(name) or {}
            q = float(value.get("pMeaningfulMove") or 0.0)
            up = float(value.get("pUpGivenMeaningfulMove") or 0.0)
            down = float(value.get("pDownGivenMeaningfulMove") or 0.0)
            return {
                "meaningful": q,
                "up": up, "down": down,
                "ambiguous": float(value.get("pAmbiguousBarrier") or 0.0),
                "magnitude": float(value.get("expectedMagnitude") or 0.0),
                "return": float(value.get("expectedReturn") or 0.0),
            }

        def sample(key):
            return {"key": key, "label": labels[key],
                    "KRONOS": external({"KRONOS": kronos.get(key)}, "KRONOS"),
                    "TIME_MACHINE": external({"TIME_MACHINE": time_machine.get(key)}, "TIME_MACHINE"),
                    "PRICE_CORE_V2": v2.get(key)}

        def cohort_prediction(item, name):
            if name == "KRONOS":
                return item["KRONOS"]
            if name == "TIME_MACHINE":
                return item["TIME_MACHINE"]
            if name == "PRICE_CORE_V2":
                value = item["PRICE_CORE_V2"]
                if not value:
                    return None
                return {**value, "ambiguous": 0.0}
            members = {"V2_KRONOS": ("PRICE_CORE_V2", "KRONOS"),
                       "V2_TIME_MACHINE": ("PRICE_CORE_V2", "TIME_MACHINE"),
                       "V2_KRONOS_TIME_MACHINE": ("PRICE_CORE_V2", "KRONOS", "TIME_MACHINE")}[name]
            available = [cohort_prediction(item, member) for member in members if item.get(member)]
            if len(available) != len(members):
                return None
            return {
                "meaningful": float(np.mean([value["meaningful"] for value in available])),
                "up": float(np.mean([value["up"] for value in available])),
                "down": float(np.mean([value["down"] for value in available])),
                "ambiguous": float(np.mean([value["ambiguous"] for value in available])),
                "magnitude": float(np.mean([value["magnitude"] for value in available])),
                "return": None if any(value["return"] is None for value in available)
                          else float(np.mean([value["return"] for value in available])),
            }

        report_horizons: dict[str, Any] = {}
        for horizon in BIG_MOVE_HORIZONS:
            partitions = {name: [sample(key) for key in keys if key[1] == horizon]
                          for name, keys in cohorts.items()}
            if not partitions["train"] or not partitions["calibration"] or not partitions["holdout"]:
                report_horizons[str(horizon)] = {"status": "INSUFFICIENT_HORIZON_ROWS",
                    "counts": {name: len(items) for name, items in partitions.items()}}
                continue
            holdout = partitions["holdout"]
            target = np.asarray([item["label"].label != "NO_BIG_MOVE" for item in holdout], dtype=int)

            def prediction_metrics(predictions, selected_threshold=None):
                q = np.asarray([value["meaningful"] for value in predictions], dtype=float)
                stage_a = _metrics(target, q)
                thresholds = [value.get("threshold", selected_threshold) for value in predictions]
                if all(value is not None for value in thresholds):
                    classified = q >= np.asarray(thresholds)
                    stage_a.update(accuracy=float(accuracy_score(target, classified)),
                        f1=float(f1_score(target, classified, zero_division=0)), threshold=None,
                        thresholdPolicy="PRE_HOLDOUT_SELECTION", thresholds=thresholds)
                else:
                    stage_a.update(accuracy=None, f1=None, threshold=None,
                                   thresholdPolicy="INSUFFICIENT_THRESHOLD_EVIDENCE")
                labelled = [index for index, item in enumerate(holdout)
                            if item["label"].label in ("BIG_UP", "BIG_DOWN")]
                direction = np.asarray([item["label"].label == "BIG_UP" for item in holdout
                                        if item["label"].label in ("BIG_UP", "BIG_DOWN")], dtype=int)
                stage_b = (_metrics(direction, np.asarray(
                    [(value["up"] + 1e-12) / (value["up"] + value["down"] + 2e-12)
                     for value in predictions], dtype=float)[labelled])
                    if len(labelled) > 1 and len(set(direction.tolist())) > 1 else
                    {"status": "INSUFFICIENT_DIRECTION_CLASSES", "samples": len(labelled)})
                magnitude_mask = [index for index, item in enumerate(holdout)
                                  if item["label"].label in ("BIG_UP", "BIG_DOWN")
                                  and item["label"].maximum_up_excursion is not None
                                  and item["label"].maximum_down_excursion is not None
                                  and item["label"].close_return_at_horizon is not None]
                realized = np.asarray([float(max(holdout[i]["label"].maximum_up_excursion,
                    holdout[i]["label"].maximum_down_excursion)) for i in magnitude_mask])
                signed = np.asarray([float(holdout[i]["label"].close_return_at_horizon)
                                     for i in magnitude_mask])
                return {
                    "status": "COMPLETED", "stageA": stage_a,
                    "stageB": stage_b,
                    "joint": _joint([item["label"].label for item in holdout], q,
                                    np.asarray([value["ambiguous"] for value in predictions]),
                                    np.asarray([value["up"] for value in predictions]),
                                    np.asarray([value["down"] for value in predictions])),
                    "magnitude": _magnitude(realized,
                        np.asarray([value["magnitude"] for value in predictions])[magnitude_mask],
                        signed,
                        np.asarray([value["return"] for value in predictions])[magnitude_mask]),
                }

            cohort_report: dict[str, Any] = {}
            holdout_predictions: dict[str, list[dict[str, float]]] = {}
            for name in SCORE_MODELS:
                predictions = [cohort_prediction(item, name) for item in holdout]
                if any(value is None for value in predictions):
                    cohort_report[name] = {"status": "UNAVAILABLE"}
                    continue
                holdout_predictions[name] = predictions
                calibration_predictions = [cohort_prediction(item, name) for item in partitions["calibration"]]
                cal_y = np.asarray([target_label(item) for item in partitions["calibration"]])
                selected_threshold = None
                if name != "PRICE_CORE_V2" and len(set(cal_y)) == 2:
                    cal_p = np.asarray([item["meaningful"] for item in calibration_predictions])
                    selected_threshold = max((float(t) for t in np.unique(cal_p)),
                        key=lambda t: (f1_score(cal_y, cal_p >= t, zero_division=0), -t))
                cohort_report[name] = prediction_metrics(predictions, selected_threshold)
            baseline = holdout_predictions.get("PRICE_CORE_V2")
            if baseline is not None:
                for name, predictions in holdout_predictions.items():
                    if name == "PRICE_CORE_V2":
                        continue
                    cohort_report[name]["bootstrapVersusPriceCoreV2"] = _paired_bootstrap(
                        holdout, target,
                        np.asarray([value["meaningful"] for value in predictions]),
                        np.asarray([value["meaningful"] for value in baseline]))
            meta_report: dict[str, Any] = {}
            for name, members in (("V2_KRONOS", ("PRICE_CORE_V2", "KRONOS")),
                                  ("V2_TIME_MACHINE", ("PRICE_CORE_V2", "TIME_MACHINE")),
                                  ("V2_KRONOS_TIME_MACHINE",
                                   ("PRICE_CORE_V2", "KRONOS", "TIME_MACHINE"))):
                rows, calibration, holdout_rows, meta_predictions = [], [], [], []
                for partition, target_list in (("train", rows), ("calibration", calibration),
                                               ("holdout", holdout_rows)):
                    for item in partitions[partition]:
                        values = [cohort_prediction(item, member) for member in members]
                        if any(value is None for value in values):
                            continue
                        target_list.append((item["key"][0],
                            np.asarray([value["meaningful"] for value in values]), int(target_label(item))))
                        if partition == "holdout":
                            meta_predictions.append({key: (None if any(value[key] is None for value in values)
                                else float(np.mean([value[key] for value in values])))
                                for key in ("meaningful", "up", "down", "ambiguous",
                                            "magnitude", "return")})
                model, detail = _meta_model(rows, calibration)
                if model is None:
                    meta_report[name] = detail
                    continue
                fitted, threshold = model
                probability = fitted.predict_proba(np.asarray([row[1] for row in holdout_rows]))[:, 1]
                if len(meta_predictions) != len(holdout):
                    meta_report[name] = {**detail, "status": "INSUFFICIENT_COMMON_HOLDOUT_SUPPORT"}
                    continue
                for prediction, value in zip(meta_predictions, probability):
                    prediction["meaningful"] = float(value)
                meta_report[name] = {
                    **detail,
                    "holdout": _metrics(np.asarray([row[2] for row in holdout_rows]), probability, threshold),
                    "cohort": prediction_metrics(meta_predictions, threshold),
                    "bootstrapVersusPriceCoreV2": _paired_bootstrap(
                        holdout, target, probability,
                        np.asarray([value["meaningful"] for value in baseline]))
                    if baseline is not None else {"status": "UNAVAILABLE"},
                }
            report_horizons[str(horizon)] = {
                "status": "COMPLETED",
                "matchedSamples": {name: len(items) for name, items in partitions.items()},
                "support": "EXACT_COMMON_REFERENCE_MINUTES_AND_HORIZON",
                "cohorts": cohort_report,
                "chronologicalMetaModel": meta_report,
            }
        core = {
            "available": True,
            "schemaVersion": "EXTERNAL_RESEARCH_EVALUATION_V2",
            "revisionId": revision_id, "instrumentId": instrument_id,
            "matchedReferenceMinutes": len(comparison_keys), "matchedDates": len(dates),
            "split": {"trainDates": [day.isoformat() for day in train_dates],
                      "calibrationDates": [day.isoformat() for day in calibration_dates],
                      "holdoutDates": [day.isoformat() for day in holdout_dates]},
            "horizons": report_horizons,
            "policy": "EXACT_COMMON_SUPPORT_CHRONOLOGICAL_TRAIN_CALIBRATION_HOLDOUT",
            "excludedEvidence": "Historical counterfactual cohort only; post-decision evidence is never decision-available",
            "v2Scoring": "CHRONOLOGICAL_STAGE_D_RESEARCH_REFITS_NO_PRODUCTION_ARTIFACT_WRITES",
            "v2Folds": getattr(self, "v2_folds", []),
            "cohortStatus": {name: any(
                report_horizons.get(str(horizon), {}).get("cohorts", {}).get(name, {}).get("status") == "COMPLETED"
                for horizon in BIG_MOVE_HORIZONS) for name in SCORE_MODELS},
            "productionArtifactsModified": False,
            "researchOnly": True, "usedByPriceCoreV2": False,
        }
        core["evaluationChecksum"] = checksum_payload(core)
        return core


def target_label(item) -> int:
    return 0 if item["label"].label == "NO_BIG_MOVE" else 1
