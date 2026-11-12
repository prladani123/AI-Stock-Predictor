from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import math
from typing import Any, Iterable, Sequence

import numpy as np

from .big_move_labels import BIG_MOVE_HORIZONS, BigMoveThresholdPolicy, default_big_move_policy


KRONOS_MODEL_NAME = "KRONOS_MINI_ZERO_SHOT_V1"
TIME_MACHINE_MODEL_NAME = "BANKNIFTY_TIME_MACHINE_V1"
EXTERNAL_RESEARCH_SCHEMA = "EXTERNAL_RESEARCH_V1"
HISTORICAL_COUNTERFACTUAL = "HISTORICAL_COUNTERFACTUAL_RESEARCH"
PROSPECTIVE = "PROSPECTIVE"
RESEARCH_STATUSES = ("DISABLED", "SERVICE_UNAVAILABLE", "MODEL_NOT_DOWNLOADED",
                     "INSUFFICIENT_HISTORY", "INSUFFICIENT_PRIOR_SESSIONS",
                     "SESSION_BOUNDARY", "READY", "COMPLETED", "FAILED")


@dataclass(frozen=True)
class ResearchHorizon:
    horizon_minutes: int
    threshold: float
    threshold_policy_version: str
    p_meaningful_move: float
    p_up_given_meaningful_move: float
    p_down_given_meaningful_move: float
    direction: str
    expected_return: float
    expected_magnitude: float
    median: float
    p05: float
    p25: float
    p50: float
    p75: float
    p95: float
    max_up_excursion: float
    max_down_excursion: float
    uncertainty: float
    path_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "minutes": self.horizon_minutes,
            "threshold": self.threshold,
            "thresholdPolicyVersion": self.threshold_policy_version,
            "pMeaningfulMove": self.p_meaningful_move,
            "pUpGivenMeaningfulMove": self.p_up_given_meaningful_move,
            "pDownGivenMeaningfulMove": self.p_down_given_meaningful_move,
            "direction": self.direction,
            "expectedReturn": self.expected_return,
            "expectedMagnitude": self.expected_magnitude,
            "median": self.median,
            "p05": self.p05,
            "p25": self.p25,
            "p50": self.p50,
            "p75": self.p75,
            "p95": self.p95,
            "maxUpExcursion": self.max_up_excursion,
            "maxDownExcursion": self.max_down_excursion,
            "uncertainty": self.uncertainty,
            "dispersion": self.uncertainty,
            "pathCount": self.path_count,
        }


@dataclass(frozen=True)
class ExternalForecastResult:
    model_name: str
    model_version: str
    reference_minute: datetime
    market_as_of: datetime
    computed_at: datetime
    available_at: datetime
    source_max_timestamp: datetime
    point_in_time_status: str
    status: str
    horizons: dict[int, ResearchHorizon]
    forecast_path: tuple[dict[str, Any], ...] = ()
    summary: dict[str, Any] = field(default_factory=dict)

    @property
    def path_count(self) -> int:
        return int(self.summary.get("pathCount") or 0)

    def decision_available(self, decision_at: datetime) -> bool:
        return self.point_in_time_status == PROSPECTIVE and self.available_at <= decision_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model_name,
            "modelVersion": self.model_version,
            "referenceMinute": self.reference_minute.isoformat(),
            "marketAsOf": self.market_as_of.isoformat(),
            "computedAt": self.computed_at.isoformat(),
            "availableAt": self.available_at.isoformat(),
            "sourceMaxTimestamp": self.source_max_timestamp.isoformat(),
            "pointInTimeStatus": self.point_in_time_status,
            "status": self.status,
            "horizons": {str(key): value.to_dict() for key, value in sorted(self.horizons.items())},
            "forecastPath": list(self.forecast_path),
            "summary": self.summary,
        }

    def to_record(self, *, dataset_revision_id: str, instrument_id: int) -> dict[str, Any]:
        identity = {
            "revision": dataset_revision_id, "instrument": instrument_id,
            "referenceMinute": self.reference_minute.isoformat(),
            "model": self.model_name, "version": self.model_version,
            "pathCount": self.path_count,
        }
        output_id = "ext-" + hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()[:32]
        summary = dict(self.summary)
        summary.update({
            "schemaVersion": EXTERNAL_RESEARCH_SCHEMA,
            "modelName": self.model_name,
            "modelVersion": self.model_version,
            "referenceMinute": self.reference_minute.isoformat(),
            "marketAsOf": self.market_as_of.isoformat(),
            "computedAt": self.computed_at.isoformat(),
            "availableAt": self.available_at.isoformat(),
            "sourceMaxTimestamp": self.source_max_timestamp.isoformat(),
            "pointInTimeStatus": self.point_in_time_status,
            "researchStatus": self.status,
            "horizons": {str(key): value.to_dict() for key, value in sorted(self.horizons.items())},
            "researchOnly": True,
            "usedByPriceCoreV2": False,
        })
        return {
            "output_id": output_id,
            "dataset_revision_id": dataset_revision_id,
            "instrument_id": instrument_id,
            "as_of": self.reference_minute,
            "model_name": self.model_name,
            "model_version": self.model_version,
            "pred_len": 30,
            "forecast_path": list(self.forecast_path),
            "summary": summary,
            "status": "EXPERIMENTAL_MODEL_OUTPUT",
        }


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def checksum_payload(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def percentile(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    return float(np.quantile(np.asarray(values, dtype=float), q, method="linear"))


def _first_barrier(bars: Sequence[dict[str, Any]], reference: float, threshold: float) -> str:
    for bar in bars:
        up = float(bar["high"]) / reference - 1.0 >= threshold
        down = 1.0 - float(bar["low"]) / reference >= threshold
        if up and down:
            return "AMBIGUOUS"
        if up:
            return "UP"
        if down:
            return "DOWN"
    return "NONE"


def aggregate_paths(
    paths: Iterable[Sequence[dict[str, Any]]], *, reference_price: float,
    known_closes: Sequence[float], policy: BigMoveThresholdPolicy | None = None,
) -> dict[int, ResearchHorizon]:
    """Aggregate sampled OHLC paths using the frozen Big Move V2 thresholds."""
    selected_policy = policy or default_big_move_policy()
    materialized = [[{key: float(bar[key]) for key in ("open", "high", "low", "close")}
                     for bar in path] for path in paths]
    if not materialized or any(len(path) < 30 for path in materialized):
        raise ValueError("every research path must contain 30 one-minute OHLC bars")
    reference = float(reference_price)
    if not math.isfinite(reference) or reference <= 0:
        raise ValueError("reference price must be positive")
    thresholds = {horizon: float(selected_policy.threshold(
        horizon, [Decimal(str(item)) for item in known_closes])) for horizon in BIG_MOVE_HORIZONS}
    result: dict[int, ResearchHorizon] = {}
    for horizon in BIG_MOVE_HORIZONS:
        returns: list[float] = []
        up_excursions: list[float] = []
        down_excursions: list[float] = []
        directions: list[str] = []
        threshold = thresholds[horizon]
        for path in materialized:
            bars = path[:horizon]
            close_return = float(bars[-1]["close"]) / reference - 1.0
            up = max([float(bar["high"]) / reference - 1.0 for bar in bars] + [0.0])
            down = max([1.0 - float(bar["low"]) / reference for bar in bars] + [0.0])
            returns.append(close_return)
            up_excursions.append(up)
            down_excursions.append(down)
            barrier = _first_barrier(bars, reference, threshold)
            if barrier in {"UP", "DOWN", "AMBIGUOUS"}:
                directions.append(barrier)
        meaningful = len(directions)
        up_count = directions.count("UP")
        down_count = directions.count("DOWN")
        p_meaningful = meaningful / len(materialized)
        p_up = up_count / meaningful if meaningful else 0.0
        p_down = down_count / meaningful if meaningful else 0.0
        direction = "UP" if p_up > p_down else "DOWN" if p_down > p_up else "NEUTRAL"
        result[horizon] = ResearchHorizon(
            horizon_minutes=horizon,
            threshold=threshold,
            threshold_policy_version=selected_policy.version,
            p_meaningful_move=p_meaningful,
            p_up_given_meaningful_move=p_up,
            p_down_given_meaningful_move=p_down,
            direction=direction,
            expected_return=float(np.mean(returns)),
            expected_magnitude=float(np.mean(np.abs(returns))),
            median=percentile(returns, 0.50),
            p05=percentile(returns, 0.05),
            p25=percentile(returns, 0.25),
            p50=percentile(returns, 0.50),
            p75=percentile(returns, 0.75),
            p95=percentile(returns, 0.95),
            max_up_excursion=percentile(up_excursions, 0.95),
            max_down_excursion=percentile(down_excursions, 0.95),
            uncertainty=float(np.std(np.asarray(returns, dtype=float))),
            path_count=len(materialized),
        )
    return result


def median_path(paths: Sequence[Sequence[dict[str, Any]]]) -> tuple[dict[str, Any], ...]:
    if not paths:
        return ()
    length = min(len(path) for path in paths)
    result: list[dict[str, Any]] = []
    for index in range(length):
        result.append({
            "timestamp": paths[0][index].get("timestamp"),
            "open": float(np.median([float(path[index]["open"]) for path in paths])),
            "high": float(np.median([float(path[index]["high"]) for path in paths])),
            "low": float(np.median([float(path[index]["low"]) for path in paths])),
            "close": float(np.median([float(path[index]["close"]) for path in paths])),
        })
    return tuple(result)


def path_fan_quantiles(paths: Sequence[Sequence[dict[str, Any]]]) -> list[dict[str, Any]]:
    if not paths:
        return []
    length = min(len(path) for path in paths)
    result: list[dict[str, Any]] = []
    for index in range(length):
        closes = [float(path[index]["close"]) for path in paths]
        result.append({"offset": index + 1, "p05": percentile(closes, 0.05),
                       "p25": percentile(closes, 0.25), "p50": percentile(closes, 0.50),
                       "p75": percentile(closes, 0.75), "p95": percentile(closes, 0.95)})
    return result
