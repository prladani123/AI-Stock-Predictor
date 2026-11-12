from __future__ import annotations

from collections import Counter
from typing import Any

from .baselines import CLASSES
from .research_models import PredictionOutcome, ResearchPrediction


def classification_metrics(
    predictions: list[ResearchPrediction],
    outcomes: list[PredictionOutcome],
) -> dict[str, Any]:
    actual_by_id = {item.prediction_id: item.actual_class for item in outcomes}
    resolved = [
        (item.predicted_class, actual_by_id[item.id])
        for item in predictions
        if item.id in actual_by_id
        and item.predicted_class in CLASSES
        and actual_by_id[item.id] in CLASSES
    ]
    matrix = {
        actual: {predicted: 0 for predicted in CLASSES}
        for actual in CLASSES
    }
    for predicted, actual in resolved:
        matrix[actual][predicted] += 1
    per_class: dict[str, dict[str, float]] = {}
    for class_name in CLASSES:
        true_positive = matrix[class_name][class_name]
        predicted_total = sum(matrix[actual][class_name] for actual in CLASSES)
        actual_total = sum(matrix[class_name].values())
        precision = true_positive / predicted_total if predicted_total else 0.0
        recall = true_positive / actual_total if actual_total else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        per_class[class_name] = {
            "precision": round(precision, 6),
            "recall": round(recall, 6),
            "f1": round(f1, 6),
        }
    directional = [(predicted, actual) for predicted, actual in resolved if predicted in {"UP", "DOWN"}]
    correct = sum(predicted == actual for predicted, actual in resolved)
    return {
        "total_samples": len(predictions),
        "resolved_samples": len(resolved),
        "unresolved_samples": len(predictions) - len(resolved),
        "class_distribution": dict(sorted(Counter(actual for _, actual in resolved).items())),
        "prediction_distribution": dict(
            sorted(Counter(item.predicted_class for item in predictions).items())
        ),
        "confusion_matrix": matrix,
        "accuracy": round(correct / len(resolved), 6) if resolved else 0.0,
        "per_class": per_class,
        "directional_precision": round(
            sum(predicted == actual for predicted, actual in directional) / len(directional), 6
        )
        if directional
        else 0.0,
        "directional_coverage": round(len(directional) / len(resolved), 6) if resolved else 0.0,
    }


def grouped_metrics(
    predictions: list[ResearchPrediction],
    outcomes: list[PredictionOutcome],
    key,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    outcome_map = {item.prediction_id: item for item in outcomes}
    groups: dict[str, list[ResearchPrediction]] = {}
    for prediction in predictions:
        groups.setdefault(str(key(prediction)), []).append(prediction)
    for name, group in sorted(groups.items()):
        group_ids = {item.id for item in group}
        group_outcomes = [item for identifier, item in outcome_map.items() if identifier in group_ids]
        result[name] = classification_metrics(group, group_outcomes)
    return result
