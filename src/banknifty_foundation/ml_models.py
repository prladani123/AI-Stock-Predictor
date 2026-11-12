from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Protocol, Sequence

import lightgbm as lgb
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    confusion_matrix,
    f1_score,
    log_loss,
    precision_recall_fscore_support,
)
from sklearn.preprocessing import StandardScaler

from .features import FEATURE_NAMES, FEATURE_SET_VERSION
from .repository import canonical_json, digest

CLASSES = ("DOWN", "NO_TRADE", "UP")
PROJECT_MARKER = "BANKNIFTY_RESEARCH_MODEL_V1"
LEGACY_MODEL_FAMILY = "LEGACY_DIRECTION_BASELINE_V1"
ALGORITHMS = ("MAJORITY", "PERSISTENCE", "LOGISTIC_REGRESSION", "LIGHTGBM")


class ResearchModel(Protocol):
    algorithm: str
    horizon_minutes: int
    model_version: str
    feature_schema: tuple[str, ...]

    def fit(self, x: np.ndarray, y: Sequence[str], **kwargs: Any) -> "ResearchModel": ...
    def predict(self, x: np.ndarray) -> np.ndarray: ...
    def predict_proba(self, x: np.ndarray) -> np.ndarray: ...
    def metadata(self) -> dict[str, Any]: ...
    def save(self, store: "ModelArtifactStore") -> dict[str, Any]: ...


def _matrix(x: np.ndarray, expected_columns: int) -> np.ndarray:
    result = np.asarray(x, dtype=float)
    if result.ndim != 2 or result.shape[1] != expected_columns:
        raise ValueError(f"feature schema mismatch: expected {expected_columns} ordered columns")
    if not np.isfinite(result).all():
        raise ValueError("features must be finite; unavailable rows must be excluded")
    return result


def _softmax(scores: np.ndarray) -> np.ndarray:
    shifted = scores - np.max(scores, axis=1, keepdims=True)
    exp = np.exp(shifted)
    return exp / exp.sum(axis=1, keepdims=True)


@dataclass
class LogisticResearchModel:
    horizon_minutes: int
    seed: int = 1729
    max_iter: int = 300
    feature_schema: tuple[str, ...] = FEATURE_NAMES
    algorithm: str = "LOGISTIC_REGRESSION"
    model_version: str = "logistic-v1"
    lineage_metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.scaler: StandardScaler | None = None
        self.estimator: LogisticRegression | None = None
        self._parameters: dict[str, Any] | None = None

    def fit(self, x: np.ndarray, y: Sequence[str], **_: Any) -> "LogisticResearchModel":
        matrix = _matrix(x, len(self.feature_schema))
        labels = np.asarray(y)
        if set(labels) != set(CLASSES):
            raise ValueError("training partition must contain all three target classes")
        self.scaler = StandardScaler().fit(matrix)
        transformed = self.scaler.transform(matrix)
        self.estimator = LogisticRegression(
            solver="lbfgs",
            class_weight="balanced",
            max_iter=self.max_iter,
            random_state=self.seed,
        ).fit(transformed, labels)
        order = [list(self.estimator.classes_).index(item) for item in CLASSES]
        self._parameters = {
            "mean": self.scaler.mean_.tolist(),
            "scale": self.scaler.scale_.tolist(),
            "coef": self.estimator.coef_[order].tolist(),
            "intercept": self.estimator.intercept_[order].tolist(),
            "classes": list(CLASSES),
        }
        return self

    def predict_proba(self, x: np.ndarray) -> np.ndarray:
        if self._parameters is None:
            raise RuntimeError("model is not fitted")
        matrix = _matrix(x, len(self.feature_schema))
        p = self._parameters
        scaled = (matrix - np.asarray(p["mean"])) / np.asarray(p["scale"])
        return _softmax(scaled @ np.asarray(p["coef"]).T + np.asarray(p["intercept"]))

    def predict(self, x: np.ndarray) -> np.ndarray:
        return np.asarray(CLASSES)[np.argmax(self.predict_proba(x), axis=1)]

    def metadata(self) -> dict[str, Any]:
        return {
            "model_family": LEGACY_MODEL_FAMILY,
            "algorithm": self.algorithm,
            "horizon_minutes": self.horizon_minutes,
            "model_version": self.model_version,
            "seed": self.seed,
            "max_iter": self.max_iter,
            "class_weight": "balanced_train_only",
            "preprocessing": "StandardScaler_fit_on_training_only",
            "feature_schema": list(self.feature_schema),
            **self.lineage_metadata,
        }

    def save(self, store: "ModelArtifactStore") -> dict[str, Any]:
        if self._parameters is None:
            raise RuntimeError("model is not fitted")
        return store.save(self.metadata(), {"format": "safe-json-logistic-v1", **self._parameters})

    @classmethod
    def from_payload(cls, manifest: dict[str, Any], payload: dict[str, Any]) -> "LogisticResearchModel":
        model = cls(
            horizon_minutes=int(manifest["horizon_minutes"]),
            seed=int(manifest["seed"]),
            max_iter=int(manifest["max_iter"]),
            feature_schema=tuple(manifest["feature_schema"]),
            model_version=str(manifest["model_version"]),
        )
        model._parameters = payload
        return model


@dataclass
class LightGBMResearchModel:
    horizon_minutes: int
    params: dict[str, Any]
    seed: int = 1729
    max_threads: int = 2
    feature_schema: tuple[str, ...] = FEATURE_NAMES
    algorithm: str = "LIGHTGBM"
    model_version: str = "lightgbm-v1"
    lineage_metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.max_threads < 1 or self.max_threads > 2:
            raise ValueError("LightGBM threads must be between 1 and 2")
        self.estimator: lgb.LGBMClassifier | None = None
        self.booster_text: str | None = None
        self.best_iteration: int | None = None
        self.feature_importance: dict[str, int] = {}

    def fit(
        self,
        x: np.ndarray,
        y: Sequence[str],
        *,
        validation_x: np.ndarray,
        validation_y: Sequence[str],
    ) -> "LightGBMResearchModel":
        train = _matrix(x, len(self.feature_schema))
        valid = _matrix(validation_x, len(self.feature_schema))
        labels = np.asarray(y)
        if set(labels) != set(CLASSES):
            raise ValueError("training partition must contain all three target classes")
        class_to_int = {name: index for index, name in enumerate(CLASSES)}
        config = {
            "objective": "multiclass",
            "num_class": 3,
            "n_estimators": 160,
            "learning_rate": 0.04,
            "verbosity": -1,
            "n_jobs": self.max_threads,
            "random_state": self.seed,
            "deterministic": True,
            "force_col_wise": True,
            **self.params,
        }
        self.estimator = lgb.LGBMClassifier(**config)
        self.estimator.fit(
            train,
            np.asarray([class_to_int[item] for item in labels]),
            eval_set=[(valid, np.asarray([class_to_int[item] for item in validation_y]))],
            callbacks=[lgb.early_stopping(12, verbose=False)],
        )
        self.best_iteration = int(self.estimator.best_iteration_ or config["n_estimators"])
        self.booster_text = self.estimator.booster_.model_to_string(num_iteration=self.best_iteration)
        self.feature_importance = {
            name: int(value)
            for name, value in zip(self.feature_schema, self.estimator.booster_.feature_importance())
        }
        return self

    def predict_proba(self, x: np.ndarray) -> np.ndarray:
        if not self.booster_text:
            raise RuntimeError("model is not fitted")
        booster = lgb.Booster(model_str=self.booster_text)
        return np.asarray(booster.predict(_matrix(x, len(self.feature_schema))))

    def predict(self, x: np.ndarray) -> np.ndarray:
        return np.asarray(CLASSES)[np.argmax(self.predict_proba(x), axis=1)]

    def metadata(self) -> dict[str, Any]:
        return {
            "model_family": LEGACY_MODEL_FAMILY,
            "algorithm": self.algorithm,
            "horizon_minutes": self.horizon_minutes,
            "model_version": self.model_version,
            "seed": self.seed,
            "max_threads": self.max_threads,
            "params": self.params,
            "best_iteration": self.best_iteration,
            "feature_schema": list(self.feature_schema),
            "feature_importance": self.feature_importance,
            "preprocessing": "none_unavailable_rows_excluded",
            **self.lineage_metadata,
        }

    def save(self, store: "ModelArtifactStore") -> dict[str, Any]:
        if not self.booster_text:
            raise RuntimeError("model is not fitted")
        return store.save(
            self.metadata(), {"format": "lightgbm-native-text-v1", "booster_text": self.booster_text}
        )

    @classmethod
    def from_payload(cls, manifest: dict[str, Any], payload: dict[str, Any]) -> "LightGBMResearchModel":
        model = cls(
            int(manifest["horizon_minutes"]),
            dict(manifest["params"]),
            int(manifest["seed"]),
            int(manifest["max_threads"]),
            tuple(manifest["feature_schema"]),
            model_version=str(manifest["model_version"]),
        )
        model.best_iteration = int(manifest["best_iteration"])
        model.feature_importance = {
            str(key): int(value) for key, value in manifest.get("feature_importance", {}).items()
        }
        model.booster_text = str(payload["booster_text"])
        return model


class ModelArtifactStore:
    """Fail-closed safe JSON/native-text artifacts; never deserializes pickle/joblib."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _safe(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if path.parent != self.root or path.suffix != ".json":
            raise ValueError("artifact path is outside the approved model directory")
        return path

    def save(self, metadata: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        artifact_checksum = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
        identity = digest({"metadata": metadata, "artifact_checksum": artifact_checksum})[:20]
        artifact_name = f"{metadata['algorithm'].lower()}-h{metadata['horizon_minutes']}-{identity}.json"
        artifact_path = self._safe(artifact_name)
        artifact_content = canonical_json(payload)
        if artifact_path.exists():
            if artifact_path.read_text(encoding="utf-8") != artifact_content:
                raise ValueError("immutable model artifact already exists with different content")
        else:
            artifact_path.write_text(artifact_content, encoding="utf-8")
        manifest = {
            "project_marker": PROJECT_MARKER,
            **metadata,
            "artifact_path": artifact_name,
            "artifact_checksum": artifact_checksum,
            "manifest_version": "model-manifest-v1",
        }
        manifest["model_checksum"] = digest(manifest)
        manifest_path = self._safe(f"manifest-{identity}.json")
        manifest_content = canonical_json(manifest)
        if manifest_path.exists():
            if manifest_path.read_text(encoding="utf-8") != manifest_content:
                raise ValueError("immutable model manifest already exists with different content")
        else:
            manifest_path.write_text(manifest_content, encoding="utf-8")
        return {**manifest, "manifest_path": manifest_path.name}

    def load(
        self,
        manifest_path: str,
        *,
        expected_horizon: int | None = None,
        expected_algorithm: str | None = None,
        expected_schema: tuple[str, ...] = FEATURE_NAMES,
    ) -> ResearchModel:
        path = self._safe(manifest_path)
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("model manifest is missing or invalid") from exc
        required = {
            "project_marker", "algorithm", "horizon_minutes", "model_version", "feature_schema",
            "artifact_path", "artifact_checksum", "model_checksum", "manifest_version",
        }
        if not required.issubset(manifest) or manifest["project_marker"] != PROJECT_MARKER:
            raise ValueError("untrusted or incomplete model manifest")
        expected_model_checksum = digest({k: v for k, v in manifest.items() if k != "model_checksum"})
        if manifest["model_checksum"] != expected_model_checksum:
            raise ValueError("model manifest checksum mismatch")
        if tuple(manifest["feature_schema"]) != expected_schema:
            raise ValueError("model feature schema mismatch")
        if int(manifest["horizon_minutes"]) not in {5, 10, 15, 20}:
            raise ValueError("model horizon mismatch")
        if expected_horizon is not None and int(manifest["horizon_minutes"]) != expected_horizon:
            raise ValueError("model horizon mismatch")
        if expected_algorithm is not None and manifest["algorithm"] != expected_algorithm:
            raise ValueError("model algorithm mismatch")
        artifact = self._safe(str(manifest["artifact_path"]))
        try:
            raw = artifact.read_text(encoding="utf-8")
            payload = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("model artifact is missing or invalid") from exc
        if hashlib.sha256(canonical_json(payload).encode()).hexdigest() != manifest["artifact_checksum"]:
            raise ValueError("model artifact checksum mismatch")
        if manifest["algorithm"] == "LOGISTIC_REGRESSION" and payload.get("format") == "safe-json-logistic-v1":
            return LogisticResearchModel.from_payload(manifest, payload)
        if manifest["algorithm"] == "LIGHTGBM" and payload.get("format") == "lightgbm-native-text-v1":
            return LightGBMResearchModel.from_payload(manifest, payload)
        raise ValueError("model algorithm or artifact format is not trusted")


def classification_metrics(y_true: Sequence[str], probabilities: np.ndarray) -> dict[str, Any]:
    predicted = np.asarray(CLASSES)[np.argmax(probabilities, axis=1)]
    truth = np.asarray(y_true)
    matrix = confusion_matrix(truth, predicted, labels=CLASSES)
    precision, recall, class_f1, support = precision_recall_fscore_support(
        truth, predicted, labels=CLASSES, zero_division=0
    )
    directional = predicted != "NO_TRADE"
    directional_count = int(directional.sum())
    return {
        "accuracy": round(float(accuracy_score(y_true, predicted)), 8),
        "macro_f1": round(float(f1_score(y_true, predicted, labels=CLASSES, average="macro", zero_division=0)), 8),
        "weighted_f1": round(float(f1_score(y_true, predicted, labels=CLASSES, average="weighted", zero_division=0)), 8),
        "log_loss": round(float(log_loss(y_true, probabilities, labels=CLASSES)), 8),
        "directional_coverage": round(float(directional.mean()), 8),
        "directional_precision": round(
            float((predicted[directional] == truth[directional]).mean())
            if directional_count else 0.0,
            8,
        ),
        "confusion_matrix": {
            actual: {predicted_name: int(matrix[row, column]) for column, predicted_name in enumerate(CLASSES)}
            for row, actual in enumerate(CLASSES)
        },
        "per_class": {
            name: {
                "precision": round(float(precision[index]), 8),
                "recall": round(float(recall[index]), 8),
                "f1": round(float(class_f1[index]), 8),
                "support": int(support[index]),
            }
            for index, name in enumerate(CLASSES)
        },
        "class_distribution": {
            name: int((truth == name).sum()) for name in CLASSES
        },
        "prediction_distribution": {
            name: int((predicted == name).sum()) for name in CLASSES
        },
    }


def composite_score(metrics: dict[str, Any]) -> float:
    # Research selection values selective directional performance, class balance,
    # coverage and probability quality. Accuracy alone can never select a model.
    return round(
        0.30 * metrics["macro_f1"]
        + 0.15 * metrics["weighted_f1"]
        + 0.20 * metrics["directional_precision"]
        + 0.15 * metrics["directional_coverage"]
        + 0.10 * metrics["accuracy"]
        - 0.10 * min(metrics["log_loss"], 3.0) / 3.0,
        8,
    )


def fit_small_lightgbm_grid(
    horizon: int,
    train_x: np.ndarray,
    train_y: Sequence[str],
    validation_x: np.ndarray,
    validation_y: Sequence[str],
    *,
    seed: int = 1729,
    threads: int = 2,
) -> tuple[LightGBMResearchModel, list[dict[str, Any]]]:
    grid = (
        {"num_leaves": 7, "max_depth": 3, "min_child_samples": 12},
        {"num_leaves": 15, "max_depth": 4, "min_child_samples": 12},
        {"num_leaves": 15, "max_depth": 5, "min_child_samples": 20},
    )
    records: list[dict[str, Any]] = []
    candidates: list[tuple[float, LightGBMResearchModel]] = []
    for params in grid:  # deliberately sequential for the i5/12GB target
        model = LightGBMResearchModel(horizon, params, seed, threads).fit(
            train_x, train_y, validation_x=validation_x, validation_y=validation_y
        )
        metrics = classification_metrics(validation_y, model.predict_proba(validation_x))
        score = composite_score(metrics)
        records.append({"params": params, "best_iteration": model.best_iteration, "metrics": metrics, "composite_score": score})
        candidates.append((score, model))
    candidates.sort(key=lambda item: (-item[0], canonical_json(item[1].params)))
    return candidates[0][1], records


def rows_to_matrix(rows: Sequence[Any]) -> np.ndarray:
    return _matrix(
        np.asarray([[float(row.values[name]) for name in FEATURE_NAMES] for row in rows], dtype=float),
        len(FEATURE_NAMES),
    )
