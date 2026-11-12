from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, Sequence

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

from .big_move_labels import BIG_MOVE_HORIZONS


MOVE_CLASSES = ("NO_BIG_MOVE", "BIG_MOVE")
DIRECTION_CLASSES = ("DOWN", "UP")
BIG_MOVE_MODEL_FAMILY = "BIG_MOVE_V2"
LEGACY_MODEL_FAMILY = "LEGACY_DIRECTION_BASELINE_V1"


class ForecastModel(Protocol):
    model_family: str
    model_version: str

    def predict(self, features: Sequence[float], reference_price: float) -> dict[str, Any]: ...


@dataclass
class BigMoveTwoStageModel:
    horizon_minutes: int
    algorithm: str
    feature_schema: tuple[str, ...]
    model_version: str
    move_scaler: StandardScaler | None
    move_estimator: Any
    direction_scaler: StandardScaler | None
    direction_estimator: Any
    expected_move_percent: float
    model_family: str = BIG_MOVE_MODEL_FAMILY
    move_calibrator: Any = None
    direction_calibrator: Any = None
    move_decision_threshold: float | None = None

    def predict(self, features: Sequence[float], reference_price: float) -> dict[str, Any]:
        matrix = np.asarray([features], dtype=float)
        if matrix.shape[1] != len(self.feature_schema) or not np.isfinite(matrix).all():
            raise ValueError("V2 feature schema mismatch or unavailable value")
        move_x = self.move_scaler.transform(matrix) if self.move_scaler else matrix
        direction_x = self.direction_scaler.transform(matrix) if self.direction_scaler else matrix
        move_order = list(self.move_estimator.classes_)
        direction_order = list(self.direction_estimator.classes_)
        move_probability = float(self.move_estimator.predict_proba(move_x)[0][move_order.index("BIG_MOVE")])
        up_probability = float(self.direction_estimator.predict_proba(direction_x)[0][direction_order.index("UP")])
        raw_move, raw_up = move_probability, up_probability
        if self.move_calibrator is not None:
            move_probability = float(self.move_calibrator.predict([raw_move])[0])
        if self.direction_calibrator is not None:
            up_probability = float(self.direction_calibrator.predict([raw_up])[0])
        if any(not np.isfinite(p) or not 0 <= p <= 1 for p in (raw_move, raw_up, move_probability, up_probability)):
            raise ValueError('Unavailable V2 probability')
        if self.move_decision_threshold is not None and not 0 <= self.move_decision_threshold <= 1:
            raise ValueError('Invalid learned move threshold')
        is_move = self.move_decision_threshold is not None and move_probability >= self.move_decision_threshold
        direction = "UP" if up_probability >= 0.5 else "DOWN"
        return {
            "minutes": self.horizon_minutes,
            "rawBigMoveProbability": raw_move,
            "rawUpGivenBigMoveProbability": raw_up,
            "moveDecisionThreshold": self.move_decision_threshold,
            "bigMoveProbability": move_probability,
            "quietProbability": 1.0 - move_probability,
            "state": "WAIT" if self.move_decision_threshold is None else "BIG_MOVE" if is_move else "NO_BIG_MOVE",
            "direction": direction if is_move else None,
            "directionProbability": max(up_probability, 1.0 - up_probability) if is_move else None,
            "upGivenBigMoveProbability": up_probability,
            "downGivenBigMoveProbability": 1.0 - up_probability,
            "expectedMovePercent": self.expected_move_percent,
            "expectedMovePoints": reference_price * self.expected_move_percent,
            "algorithm": self.algorithm,
            "modelVersion": self.model_version,
        }


def train_two_stage_model(
    *, horizon: int, train_x: np.ndarray, labels: Sequence[str], realized_excursions: Sequence[float],
    feature_schema: Sequence[str], algorithm: str = "LOGISTIC_REGRESSION", seed: int = 1729,
    validation_x: np.ndarray | None = None, validation_labels: Sequence[str] | None = None,
    model_version: str | None = None,
) -> BigMoveTwoStageModel:
    """Fit move detection on all valid rows and direction only on first-touch move rows."""
    if horizon not in BIG_MOVE_HORIZONS:
        raise ValueError("unsupported V2 horizon")
    matrix = np.asarray(train_x, dtype=float)
    if matrix.ndim != 2 or matrix.shape[1] != len(feature_schema) or not np.isfinite(matrix).all():
        raise ValueError("finite ordered V2 training features are required")
    if len(labels) != len(matrix) or len(realized_excursions) != len(matrix):
        raise ValueError("training rows, labels and excursions must align")
    usable = np.asarray([item in {"BIG_UP", "BIG_DOWN", "NO_BIG_MOVE"} for item in labels])
    move_y = np.asarray(["BIG_MOVE" if item in {"BIG_UP", "BIG_DOWN"} else "NO_BIG_MOVE" for item in labels])[usable]
    direction_mask = np.asarray([item in {"BIG_UP", "BIG_DOWN"} for item in labels])
    direction_y = np.asarray(["UP" if item == "BIG_UP" else "DOWN" for item in labels])[direction_mask]
    if set(move_y) != set(MOVE_CLASSES) or set(direction_y) != set(DIRECTION_CLASSES):
        raise ValueError("both move and direction stages require both classes")
    move_x, direction_x = matrix[usable], matrix[direction_mask]
    if algorithm == "LOGISTIC_REGRESSION":
        move_scaler = StandardScaler().fit(move_x)
        direction_scaler = StandardScaler().fit(direction_x)
        move_estimator = LogisticRegression(class_weight="balanced", max_iter=300, random_state=seed).fit(move_scaler.transform(move_x), move_y)
        direction_estimator = LogisticRegression(class_weight="balanced", max_iter=300, random_state=seed).fit(direction_scaler.transform(direction_x), direction_y)
    elif algorithm == "LIGHTGBM":
        import lightgbm as lgb
        move_scaler = direction_scaler = None
        params = {"n_estimators": 160, "learning_rate": 0.04, "num_leaves": 15, "max_depth": 4,
                  "class_weight": "balanced", "n_jobs": 2, "random_state": seed, "verbosity": -1,
                  "deterministic": True, "force_col_wise": True}
        move_estimator = lgb.LGBMClassifier(**params)
        direction_estimator = lgb.LGBMClassifier(**params)
        callbacks = [lgb.early_stopping(12, verbose=False)]
        if validation_x is not None and validation_labels is not None:
            valid = np.asarray(validation_x, dtype=float)
            valid_move = np.asarray(["BIG_MOVE" if item in {"BIG_UP", "BIG_DOWN"} else "NO_BIG_MOVE" for item in validation_labels])
            move_estimator.fit(move_x, move_y, eval_set=[(valid, valid_move)], callbacks=callbacks)
        else:
            move_estimator.fit(move_x, move_y)
        direction_estimator.fit(direction_x, direction_y)
    else:
        raise ValueError("V2 supports LOGISTIC_REGRESSION and LIGHTGBM")
    big_excursions = [float(value) for value, label in zip(realized_excursions, labels) if label in {"BIG_UP", "BIG_DOWN"}]
    expected = float(np.median(big_excursions)) if big_excursions else 0.0
    if model_version is None:
        from hashlib import sha256
        import json
        identity=json.dumps({'schema':list(feature_schema),'labels':list(labels),
            'excursions':[float(v) for v in realized_excursions],'seed':seed,'algorithm':algorithm,
            'horizon':horizon,'implementation':'two-stage-v2-1'},sort_keys=True).encode()+matrix.tobytes()
        if validation_x is not None:
            identity+=np.asarray(validation_x,dtype=float).tobytes()+json.dumps(list(validation_labels) if validation_labels is not None else None).encode()
        model_version=f"big-move-v2-{algorithm.lower()}-h{horizon}-{sha256(identity).hexdigest()[:20]}"
    return BigMoveTwoStageModel(horizon, algorithm, tuple(feature_schema),
        model_version, move_scaler, move_estimator,
        direction_scaler, direction_estimator, expected)
