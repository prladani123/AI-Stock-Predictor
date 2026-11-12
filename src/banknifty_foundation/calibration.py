from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import log_loss

from .ml_models import CLASSES
from .repository import digest


def _normalise(values: np.ndarray) -> np.ndarray:
    clipped = np.clip(np.asarray(values, dtype=float), 1e-9, 1.0)
    return clipped / clipped.sum(axis=1, keepdims=True)


def calibration_metrics(
    y_true: Sequence[str], probabilities: np.ndarray, buckets: int = 10
) -> dict[str, Any]:
    actual = np.asarray(y_true)
    probs = _normalise(probabilities)
    one_hot = np.asarray(
        [[1.0 if value == name else 0.0 for name in CLASSES] for value in actual]
    )
    per_class: dict[str, Any] = {}
    all_gaps: list[tuple[float, int]] = []
    class_reliability: dict[str, list[dict[str, Any]]] = {}
    for index, name in enumerate(CLASSES):
        records: list[dict[str, Any]] = []
        for bucket in range(buckets):
            lower, upper = bucket / buckets, (bucket + 1) / buckets
            mask = (probs[:, index] >= lower) & (
                probs[:, index] <= upper if bucket == buckets - 1 else probs[:, index] < upper
            )
            count = int(mask.sum())
            if count:
                confidence = float(probs[mask, index].mean())
                observed = float(one_hot[mask, index].mean())
                gap = abs(confidence - observed)
                all_gaps.append((gap, count))
                records.append(
                    {
                        "bucket_index": bucket,
                        "lower": lower,
                        "upper": upper,
                        "count": count,
                        "mean_probability": round(confidence, 8),
                        "observed_frequency": round(observed, 8),
                        "absolute_gap": round(gap, 8),
                    }
                )
        per_class[name] = {
            "brier": round(float(np.mean((probs[:, index] - one_hot[:, index]) ** 2)), 8)
        }
        class_reliability[name] = records
    total = sum(count for _, count in all_gaps) or 1
    return {
        "multiclass_brier": round(
            float(np.mean(np.sum((probs - one_hot) ** 2, axis=1))), 8
        ),
        "log_loss": round(float(log_loss(actual, probs, labels=CLASSES)), 8),
        "ece": round(sum(gap * count for gap, count in all_gaps) / total, 8),
        "mce": round(max((gap for gap, _ in all_gaps), default=0.0), 8),
        "per_class": per_class,
        "class_reliability_buckets": class_reliability,
    }


def top_confidence_reliability_buckets(
    y_true: Sequence[str], probabilities: np.ndarray, buckets: int = 10
) -> list[dict[str, Any]]:
    """Reliability evidence used by policy gating, based on each row's top class."""
    actual = np.asarray(y_true)
    probs = _normalise(probabilities)
    top_indices = np.argmax(probs, axis=1)
    top_classes = np.asarray(CLASSES)[top_indices]
    confidence = probs[np.arange(len(probs)), top_indices]
    result: list[dict[str, Any]] = []
    for bucket in range(buckets):
        lower, upper = bucket / buckets, (bucket + 1) / buckets
        mask = (confidence >= lower) & (
            confidence <= upper if bucket == buckets - 1 else confidence < upper
        )
        count = int(mask.sum())
        correct = top_classes[mask] == actual[mask] if count else np.asarray([], dtype=bool)
        directional = top_classes[mask] != "NO_TRADE" if count else np.asarray([], dtype=bool)
        directional_count = int(directional.sum())
        result.append(
            {
                "bucket_index": bucket,
                "lower": lower,
                "upper": upper,
                "count": count,
                "mean_confidence": round(float(confidence[mask].mean()), 8) if count else None,
                "observed_accuracy": round(float(correct.mean()), 8) if count else None,
                "directional_count": directional_count,
                "observed_directional_precision": (
                    round(float(correct[directional].mean()), 8) if directional_count else None
                ),
            }
        )
    return result


@dataclass(frozen=True)
class CalibrationResult:
    version: str
    method: str
    model_checksum: str
    horizon_minutes: int
    parameters: dict[str, Any]
    fit_range: dict[str, str]
    selection_range: dict[str, str]
    fit_count: int
    selection_count: int
    raw_selection_metrics: dict[str, Any]
    calibrated_selection_metrics: dict[str, Any]
    holdout_improved: bool
    reliability_buckets: tuple[dict[str, Any], ...]
    rejection_reasons: tuple[str, ...]
    checksum: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "method": self.method,
            "model_checksum": self.model_checksum,
            "horizon_minutes": self.horizon_minutes,
            "parameters": self.parameters,
            "fit_range": self.fit_range,
            "selection_range": self.selection_range,
            "fit_count": self.fit_count,
            "selection_count": self.selection_count,
            "raw_selection_metrics": self.raw_selection_metrics,
            "calibrated_selection_metrics": self.calibrated_selection_metrics,
            "holdout_improved": self.holdout_improved,
            "reliability_buckets": list(self.reliability_buckets),
            "rejection_reasons": list(self.rejection_reasons),
            "checksum": self.checksum,
        }


class ProbabilityCalibrator:
    def __init__(self, method: str, parameters: dict[str, Any]) -> None:
        self.method = method
        self.parameters = parameters

    def transform(self, raw_probabilities: np.ndarray) -> np.ndarray:
        raw = _normalise(raw_probabilities)
        outputs = np.zeros_like(raw)
        for index, name in enumerate(CLASSES):
            params = self.parameters[name]
            if self.method == "SIGMOID":
                score = np.log(
                    np.clip(raw[:, index], 1e-9, 1 - 1e-9)
                    / np.clip(1 - raw[:, index], 1e-9, 1)
                )
                outputs[:, index] = 1 / (
                    1
                    + np.exp(
                        -(float(params["coef"]) * score + float(params["intercept"]))
                    )
                )
            elif self.method == "ISOTONIC":
                outputs[:, index] = np.interp(
                    raw[:, index],
                    np.asarray(params["x"], dtype=float),
                    np.asarray(params["y"], dtype=float),
                )
            else:
                raise ValueError("unsupported calibration method")
        return _normalise(outputs)


def _fit_sigmoid(
    raw: np.ndarray, actual: Sequence[str], seed: int
) -> ProbabilityCalibrator:
    parameters: dict[str, Any] = {}
    for index, name in enumerate(CLASSES):
        target = np.asarray([int(item == name) for item in actual])
        if len(set(target)) < 2:
            raise ValueError(f"SIGMOID_CLASS_DIVERSITY_{name}")
        score = np.log(
            np.clip(raw[:, index], 1e-9, 1 - 1e-9)
            / np.clip(1 - raw[:, index], 1e-9, 1)
        ).reshape(-1, 1)
        fitted = LogisticRegression(random_state=seed, max_iter=200).fit(score, target)
        parameters[name] = {
            "coef": float(fitted.coef_[0, 0]),
            "intercept": float(fitted.intercept_[0]),
        }
    return ProbabilityCalibrator("SIGMOID", parameters)


def _fit_isotonic(raw: np.ndarray, actual: Sequence[str]) -> ProbabilityCalibrator:
    parameters: dict[str, Any] = {}
    for index, name in enumerate(CLASSES):
        target = np.asarray([int(item == name) for item in actual])
        fitted = IsotonicRegression(out_of_bounds="clip").fit(raw[:, index], target)
        x_values = [float(item) for item in fitted.X_thresholds_]
        y_values = [float(item) for item in fitted.y_thresholds_]
        if len(x_values) < 4 or len(y_values) < 4:
            raise ValueError(f"ISOTONIC_UNSTABLE_STEPS_{name}")
        parameters[name] = {"x": x_values, "y": y_values}
    return ProbabilityCalibrator("ISOTONIC", parameters)


def _range(values: Sequence[Any]) -> dict[str, str]:
    timestamps = [item.isoformat() if hasattr(item, "isoformat") else str(item) for item in values]
    return {"from": min(timestamps), "to": max(timestamps)}


def fit_calibrator(
    fit_probabilities: np.ndarray,
    fit_actual: Sequence[str],
    selection_probabilities: np.ndarray,
    selection_actual: Sequence[str],
    *,
    fit_timestamps: Sequence[Any],
    selection_timestamps: Sequence[Any],
    model_checksum: str,
    horizon_minutes: int,
    seed: int = 1729,
    minimum_fit_samples: int = 20,
    minimum_selection_samples: int = 10,
    minimum_class_samples: int = 1,
) -> tuple[ProbabilityCalibrator | None, CalibrationResult]:
    """Fit candidates on calibration-fit and select only on later holdout rows."""
    fit_raw = _normalise(fit_probabilities)
    selection_raw = _normalise(selection_probabilities)
    fit_actual = list(fit_actual)
    selection_actual = list(selection_actual)
    if max(fit_timestamps) >= min(selection_timestamps):
        raise ValueError("calibration fit must be strictly earlier than calibration selection")
    rejection: list[str] = []
    if len(fit_actual) < minimum_fit_samples:
        rejection.append("INSUFFICIENT_CALIBRATION_FIT_SAMPLES")
    if len(selection_actual) < minimum_selection_samples:
        rejection.append("INSUFFICIENT_CALIBRATION_SELECTION_SAMPLES")
    for name in CLASSES:
        if fit_actual.count(name) < minimum_class_samples:
            rejection.append(f"INSUFFICIENT_FIT_CLASS_SAMPLES_{name}")
        if selection_actual.count(name) < minimum_class_samples:
            rejection.append(f"INSUFFICIENT_SELECTION_CLASS_SAMPLES_{name}")
    if any(len(np.unique(fit_raw[:, index])) < 4 for index in range(3)):
        rejection.append("INSUFFICIENT_PROBABILITY_DIVERSITY")
    raw_metrics = calibration_metrics(selection_actual, selection_raw)

    candidates: list[
        tuple[str, ProbabilityCalibrator, dict[str, Any], list[dict[str, Any]]]
    ] = []
    if not rejection:
        for method, fitter in (
            ("SIGMOID", lambda: _fit_sigmoid(fit_raw, fit_actual, seed)),
            ("ISOTONIC", lambda: _fit_isotonic(fit_raw, fit_actual)),
        ):
            try:
                calibrator = fitter()
                transformed = calibrator.transform(selection_raw)
                metrics = calibration_metrics(selection_actual, transformed)
                if (
                    metrics["log_loss"] > raw_metrics["log_loss"] + 0.01
                    or metrics["multiclass_brier"]
                    > raw_metrics["multiclass_brier"] + 0.01
                ):
                    rejection.append(f"{method}_MATERIALLY_WORSE_ON_HOLDOUT")
                    continue
                candidates.append(
                    (
                        method,
                        calibrator,
                        metrics,
                        top_confidence_reliability_buckets(
                            selection_actual, transformed
                        ),
                    )
                )
            except ValueError as exc:
                rejection.append(str(exc))

    if candidates:
        candidates.sort(
            key=lambda item: (
                item[2]["log_loss"],
                item[2]["multiclass_brier"],
                item[2]["ece"],
                item[0],
            )
        )
        method, chosen, selected_metrics, reliability = candidates[0]
        parameters = chosen.parameters
    else:
        method, chosen, selected_metrics = "UNAVAILABLE", None, raw_metrics
        reliability = top_confidence_reliability_buckets(
            selection_actual, selection_raw
        )
        parameters = {}

    payload = {
        "version": f"calibration-v2-{method.lower()}",
        "method": method,
        "model_checksum": model_checksum,
        "horizon_minutes": horizon_minutes,
        "parameters": parameters,
        "fit_range": _range(fit_timestamps),
        "selection_range": _range(selection_timestamps),
        "fit_count": len(fit_actual),
        "selection_count": len(selection_actual),
        "raw_selection_metrics": raw_metrics,
        "calibrated_selection_metrics": selected_metrics,
        "holdout_improved": bool(
            method != "UNAVAILABLE"
            and selected_metrics["log_loss"] < raw_metrics["log_loss"]
            and selected_metrics["multiclass_brier"]
            < raw_metrics["multiclass_brier"]
        ),
        "reliability_buckets": tuple(reliability),
        "rejection_reasons": tuple(sorted(set(rejection))),
    }
    return chosen, CalibrationResult(**payload, checksum=digest(payload))


@dataclass(frozen=True)
class AbstentionPolicy:
    version: str = "abstention-v2"
    minimum_probability: float = 0.55
    minimum_margin: float = 0.10
    minimum_reliability_bucket_samples: int = 5
    minimum_directional_precision: float = 0.50

    @property
    def checksum(self) -> str:
        return digest(self.__dict__)


def matching_reliability_bucket(
    confidence: float, buckets: Sequence[dict[str, Any]]
) -> dict[str, Any] | None:
    for item in buckets:
        if confidence >= float(item["lower"]) and (
            confidence <= float(item["upper"])
            if int(item["bucket_index"]) == 9
            else confidence < float(item["upper"])
        ):
            return dict(item)
    return None


def apply_abstention_policy(
    *,
    raw_probabilities: Sequence[float],
    calibrated_probabilities: Sequence[float] | None,
    reliability_buckets: Sequence[dict[str, Any]],
    policy: AbstentionPolicy,
    model_ready: bool = True,
    calibrator_ready: bool = True,
    partial_lookback: bool = False,
    data_quality_failure: bool = False,
    schema_matches: bool = True,
    artifact_checksum_valid: bool = True,
    values_in_range: bool = True,
    session_known: bool = True,
    unresolved_gap: bool = False,
) -> dict[str, Any]:
    reasons: list[str] = []
    if not model_ready:
        reasons.append("MODEL_NOT_READY")
    if not calibrator_ready or calibrated_probabilities is None:
        reasons.append("CALIBRATOR_NOT_READY")
    if partial_lookback:
        reasons.append("PARTIAL_LOOKBACK")
    if data_quality_failure:
        reasons.append("DATA_QUALITY_FAILURE")
    if not schema_matches:
        reasons.append("FEATURE_SCHEMA_MISMATCH")
    if not artifact_checksum_valid:
        reasons.append("ARTEFACT_CHECKSUM_FAILURE")
    if not values_in_range:
        reasons.append("UNSEEN_FEATURE_RANGE")
    if not session_known:
        reasons.append("UNKNOWN_SESSION")
    if unresolved_gap:
        reasons.append("UNRESOLVED_GAP")
    probabilities = np.asarray(
        calibrated_probabilities
        if calibrated_probabilities is not None
        else raw_probabilities,
        dtype=float,
    )
    order = np.argsort(probabilities)[::-1]
    confidence = float(probabilities[order[0]])
    margin = float(probabilities[order[0]] - probabilities[order[1]])
    raw_class = CLASSES[int(np.argmax(raw_probabilities))]
    calibrated_class = CLASSES[int(order[0])]
    bucket = matching_reliability_bucket(confidence, reliability_buckets)
    if calibrated_class in {"UP", "DOWN"}:
        if confidence < policy.minimum_probability:
            reasons.append("LOW_PROBABILITY")
        if margin < policy.minimum_margin:
            reasons.append("SMALL_MARGIN")
        if bucket is None or int(bucket["count"]) < policy.minimum_reliability_bucket_samples:
            reasons.append("INSUFFICIENT_CALIBRATION_SAMPLES")
        elif (
            bucket["observed_directional_precision"] is None
            or float(bucket["observed_directional_precision"])
            < policy.minimum_directional_precision
        ):
            reasons.append("INSUFFICIENT_BUCKET_DIRECTIONAL_PRECISION")
    return {
        "policy_version": policy.version,
        "policy_checksum": policy.checksum,
        "raw_class": raw_class,
        "calibrated_class": calibrated_class,
        "final_class": "NO_TRADE" if reasons else calibrated_class,
        "system_abstained": bool(reasons),
        "abstention_reasons": tuple(sorted(set(reasons))),
        "confidence": round(confidence, 8),
        "margin": round(margin, 8),
        "reliability_bucket": bucket,
        "raw_probabilities": {
            name: float(raw_probabilities[index]) for index, name in enumerate(CLASSES)
        },
        "calibrated_probabilities": (
            {name: float(probabilities[index]) for index, name in enumerate(CLASSES)}
            if calibrated_probabilities is not None
            else None
        ),
    }
