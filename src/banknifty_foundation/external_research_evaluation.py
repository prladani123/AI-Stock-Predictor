"""Chronological, matched-cohort evaluation for optional external research."""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, brier_score_loss, f1_score, log_loss

from .big_move_labels import BIG_MOVE_HORIZONS
from .calendar import IST
from .external_research import KRONOS_MODEL_NAME, TIME_MACHINE_MODEL_NAME

VALID_LABELS = {"BIG_UP", "BIG_DOWN", "NO_BIG_MOVE"}
SCORE_MODELS = ("PRICE_CORE_V2", "KRONOS", "TIME_MACHINE",
                "V2_KRONOS", "V2_TIME_MACHINE", "V2_KRONOS_TIME_MACHINE")


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


def _fit_meta(rows: list[tuple[date, np.ndarray, int]], calibration: list[tuple[date, np.ndarray, int]],
              holdout: list[tuple[date, np.ndarray, int]]) -> dict[str, Any]:
    if len({row[0] for row in rows}) < 3 or len(rows) < 20:
        return {"status": "INSUFFICIENT_TRAINING_ROWS"}
    x = np.asarray([row[1] for row in rows], dtype=float)
    y = np.asarray([row[2] for row in rows], dtype=int)
    if len(set(y.tolist())) < 2:
        return {"status": "INSUFFICIENT_TRAINING_CLASSES"}
    model = LogisticRegression(C=1.0, max_iter=500, random_state=1729).fit(x, y)
    calibration_p = model.predict_proba(np.asarray([row[1] for row in calibration], dtype=float))[:, 1]
    calibration_y = np.asarray([row[2] for row in calibration], dtype=int)
    best = (0.5, -1.0)
    for threshold in np.arange(0.10, 0.91, 0.05):
        score = f1_score(calibration_y, (calibration_p >= threshold).astype(int), zero_division=0)
        if score > best[1]:
            best = (float(threshold), float(score))
    holdout_x = np.asarray([row[1] for row in holdout], dtype=float)
    holdout_y = np.asarray([row[2] for row in holdout], dtype=int)
    probability = model.predict_proba(holdout_x)[:, 1]
    return {"status": "COMPLETED", "thresholdSelection": {"threshold": best[0], "f1": best[1]},
            "holdout": _metrics(holdout_y, probability, best[0]),
            "coefficients": [float(value) for value in model.coef_[0]]}


class ExternalResearchEvaluationService:
    def __init__(self, repository: Any) -> None:
        self.repository = repository

    def _v2(self, revision_id: str, instrument_id: int) -> dict[tuple[Any, int], float]:
        with self.repository._connection() as connection:
            rows = connection.execute("""
                SELECT as_of, horizon_minutes, big_move_probability
                FROM big_move_predictions
                WHERE dataset_revision_id=%s AND instrument_id=%s
            """, (revision_id, instrument_id)).fetchall()
        return {(row[0], int(row[1])): float(row[2]) for row in rows}

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
                for key, horizon in (summary.get("horizons") or {}).items():
                    target[(row["as_of"], int(key))] = horizon
        v2 = self._v2(revision_id, instrument_id)
        matched_keys = sorted(set(labels) & set(kronos) & set(time_machine) & set(v2))
        if not matched_keys:
            return {"available": False, "reason": "NO_MATCHED_REFERENCE_MINUTES",
                    "cohortCounts": {"labels": len(labels), "kronos": len(kronos),
                                     "timeMachine": len(time_machine), "priceCoreV2": len(v2),
                                     "allThree": 0}, "researchOnly": True}
        by_date: dict[date, list[tuple[Any, int]]] = defaultdict(list)
        for key in matched_keys:
            by_date[key[0].astimezone(IST).date()].append(key)
        dates = sorted(by_date)
        if len(dates) < 6:
            return {"available": False, "reason": "INSUFFICIENT_MATCHED_DATES",
                    "matchedReferenceMinutes": len(matched_keys), "matchedDates": len(dates),
                    "researchOnly": True}
        split_a = max(1, int(len(dates) * 0.60))
        split_b = max(split_a + 1, int(len(dates) * 0.80))
        train_dates, calibration_dates, holdout_dates = dates[:split_a], dates[split_a:split_b], dates[split_b:]
        cohorts = {
            "train": [key for day in train_dates for key in by_date[day]],
            "calibration": [key for day in calibration_dates for key in by_date[day]],
            "holdout": [key for day in holdout_dates for key in by_date[day]],
        }
        report_horizons: dict[str, Any] = {}
        for horizon in BIG_MOVE_HORIZONS:
            def sample(key):
                return (key[0], labels[key], kronos[key], time_machine[key], v2[key])
            partitions = {name: [sample(key) for key in keys if key[1] == horizon]
                          for name, keys in cohorts.items()}
            if not partitions["train"] or not partitions["calibration"] or not partitions["holdout"]:
                report_horizons[str(horizon)] = {"status": "INSUFFICIENT_HORIZON_ROWS",
                    "counts": {name: len(items) for name, items in partitions.items()}}
                continue
            def target(item):
                return 0 if item[1].label == "NO_BIG_MOVE" else 1
            holdout = partitions["holdout"]
            target_y = np.asarray([target(item) for item in holdout], dtype=int)
            direct = {
                "PRICE_CORE_V2": _metrics(target_y, np.asarray([item[4] for item in holdout])),
                "KRONOS": _metrics(target_y, np.asarray([float(item[2]["pMeaningfulMove"]) for item in holdout])),
                "TIME_MACHINE": _metrics(target_y, np.asarray([float(item[3]["pMeaningfulMove"]) for item in holdout])),
            }
            meta = {}
            for name, indexes in (("V2_KRONOS", (4, 2)), ("V2_TIME_MACHINE", (4, 3)),
                                  ("V2_KRONOS_TIME_MACHINE", (4, 2, 3))):
                def vector(item, selected=indexes):
                    return np.asarray([float(item[index]["pMeaningfulMove"]) if index in (2, 3) else float(item[index])
                                       for index in selected], dtype=float)
                meta[name] = _fit_meta(
                    [(item[0], vector(item), target(item)) for item in partitions["train"]],
                    [(item[0], vector(item), target(item)) for item in partitions["calibration"]],
                    [(item[0], vector(item), target(item)) for item in holdout])
            report_horizons[str(horizon)] = {
                "status": "COMPLETED", "matchedSamples": {name: len(items) for name, items in partitions.items()},
                "directHoldout": direct, "chronologicalMetaModel": meta}
        return {
            "available": True,
            "schemaVersion": "EXTERNAL_RESEARCH_EVALUATION_V1",
            "revisionId": revision_id, "instrumentId": instrument_id,
            "matchedReferenceMinutes": len(matched_keys), "matchedDates": len(dates),
            "split": {"trainDates": [day.isoformat() for day in train_dates],
                      "calibrationDates": [day.isoformat() for day in calibration_dates],
                      "holdoutDates": [day.isoformat() for day in holdout_dates]},
            "horizons": report_horizons,
            "policy": "IDENTICAL_MATCHED_REFERENCE_MINUTES_CHRONOLOGICAL_META_MODEL",
            "productionArtifactsModified": False,
            "researchOnly": True, "usedByPriceCoreV2": False,
        }
