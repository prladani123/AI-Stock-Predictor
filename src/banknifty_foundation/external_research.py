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
EXTERNAL_RESEARCH_SCHEMA = "EXTERNAL_RESEARCH_V2"
HISTORICAL_COUNTERFACTUAL = "HISTORICAL_COUNTERFACTUAL_RESEARCH"
PROSPECTIVE_DECISION_AVAILABLE = "PROSPECTIVE_DECISION_AVAILABLE"
PROSPECTIVE_POST_DECISION = "PROSPECTIVE_POST_DECISION"
PROSPECTIVE_STATUSES = (PROSPECTIVE_DECISION_AVAILABLE, PROSPECTIVE_POST_DECISION)
PROSPECTIVE = PROSPECTIVE_POST_DECISION  # Backward-compatible honest default.
RESEARCH_STATUSES = ("DISABLED", "SERVICE_UNAVAILABLE", "MODEL_NOT_DOWNLOADED",
                     "REPLAY_POOL_NOT_CONFIGURED", "INSUFFICIENT_HISTORY",
                     "INSUFFICIENT_PRIOR_SESSIONS", "SESSION_BOUNDARY", "INPUT_GAP",
                     "READY", "COMPLETED", "FAILED")


@dataclass(frozen=True)
class ResearchHorizon:
    horizon_minutes: int
    threshold: float
    threshold_policy_version: str
    p_meaningful_move: float
    p_up_given_meaningful_move: float
    p_down_given_meaningful_move: float
    p_ambiguous_barrier: float
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
            "pAmbiguousBarrier": self.p_ambiguous_barrier,
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
    prediction_ids: tuple[str, ...] = ()
    replay_pool_revision_id: str | None = None
    input_checksum: str | None = None
    reference_state_checksum: str | None = None
    configuration: dict[str, Any] = field(default_factory=dict)
    upstream_revision: str | None = None
    model_revision: str | None = None
    tokenizer_revision: str | None = None
    seed: int | None = None
    threshold_policy_version: str | None = None
    sampling_pool_checksum: str | None = None
    decision_at: datetime | None = None

    @property
    def path_count(self) -> int:
        return int(self.summary.get("pathCount") or 0)

    @property
    def sampling(self) -> dict[str, Any]:
        value = self.summary.get("sampling")
        return dict(value) if isinstance(value, dict) else {}

    def decision_available(self, decision_at: datetime) -> bool:
        return (self.point_in_time_status == PROSPECTIVE_DECISION_AVAILABLE
                and self.available_at <= decision_at)

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
            "decisionAt": self.decision_at.isoformat() if self.decision_at else None,
            "status": self.status,
            "predictionIds": list(self.prediction_ids),
            "replayPoolRevisionId": self.replay_pool_revision_id,
            "inputChecksum": self.input_checksum,
            "referenceStateChecksum": self.reference_state_checksum,
            "upstreamRevision": self.upstream_revision,
            "modelRevision": self.model_revision,
            "tokenizerRevision": self.tokenizer_revision,
            "seed": self.seed,
            "thresholdPolicyVersion": self.threshold_policy_version,
            "samplingPoolChecksum": self.sampling_pool_checksum,
            "horizons": {str(key): value.to_dict() for key, value in sorted(self.horizons.items())},
            "forecastPath": list(self.forecast_path),
            "summary": self.summary,
        }

    def _identity(self, *, dataset_revision_id: str | None, instrument_id: int) -> dict[str, Any]:
        policy_versions = sorted({item.threshold_policy_version for item in self.horizons.values()})
        return {
            "schema": EXTERNAL_RESEARCH_SCHEMA,
            "datasetRevision": dataset_revision_id,
            "replayPoolRevision": self.replay_pool_revision_id,
            "instrument": instrument_id,
            "referenceMinute": self.reference_minute.isoformat(),
            "predictionIds": sorted(self.prediction_ids),
            "decisionAt": self.decision_at.isoformat() if self.decision_at else None,
            "marketAsOf": self.market_as_of.isoformat(),
            "sourceMaxTimestamp": self.source_max_timestamp.isoformat(),
            "model": self.model_name, "modelVersion": self.model_version,
            "upstreamRevision": self.upstream_revision,
            "modelRevision": self.model_revision,
            "tokenizerRevision": self.tokenizer_revision,
            "inputChecksum": self.input_checksum,
            "referenceStateChecksum": self.reference_state_checksum,
            "configurationChecksum": checksum_payload(self.configuration),
            "sampling": self.sampling,
            "seed": self.seed,
            "pathCount": self.path_count,
            "thresholdPolicyVersion": self.threshold_policy_version or (
                policy_versions[0] if len(policy_versions) == 1 else None),
            "samplingPoolChecksum": self.sampling_pool_checksum,
        }

    def to_record(self, *, dataset_revision_id: str | None, instrument_id: int) -> dict[str, Any]:
        stamps = [self.reference_minute, self.market_as_of, self.source_max_timestamp,
                  self.computed_at, self.available_at]
        if self.decision_at is not None:
            stamps.append(self.decision_at)
        if any(value.tzinfo is None or value.utcoffset() is None for value in stamps):
            raise ValueError("external evidence timestamps must be aware")
        if not self.source_max_timestamp <= self.market_as_of <= self.computed_at <= self.available_at:
            raise ValueError("invalid external market/source cutoff")
        if self.point_in_time_status in PROSPECTIVE_STATUSES:
            if (dataset_revision_id is not None or not self.prediction_ids or self.decision_at is None
                    or self.market_as_of > self.decision_at
                    or self.point_in_time_status != prospective_status(self.available_at, self.decision_at)):
                raise ValueError("invalid live research identity or point-in-time status")
        elif (self.point_in_time_status != HISTORICAL_COUNTERFACTUAL
              or self.prediction_ids or dataset_revision_id is None):
            raise ValueError("invalid historical research identity")
        identity = self._identity(dataset_revision_id=dataset_revision_id,
                                  instrument_id=instrument_id)
        identity_checksum = checksum_payload(identity)
        output_id = "ext-" + identity_checksum[:32]
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
            "decisionAt": self.decision_at.isoformat() if self.decision_at else None,
            "predictionIds": sorted(self.prediction_ids),
            "replayPoolRevisionId": self.replay_pool_revision_id,
            "identityChecksum": identity_checksum,
            "identity": identity,
            "inputChecksum": self.input_checksum,
            "referenceStateChecksum": self.reference_state_checksum,
            "configurationChecksum": identity["configurationChecksum"],
            "upstreamRevision": self.upstream_revision,
            "modelRevision": self.model_revision,
            "tokenizerRevision": self.tokenizer_revision,
            "seed": summary.get("seed", self.seed),
            "pathCount": self.path_count,
            "thresholdPolicyVersion": identity["thresholdPolicyVersion"],
            "samplingPoolChecksum": self.sampling_pool_checksum,
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
            "identity_checksum": identity_checksum,
            "prediction_ids": sorted(self.prediction_ids),
            "replay_pool_revision_id": self.replay_pool_revision_id,
            "input_checksum": self.input_checksum,
            "reference_state_checksum": self.reference_state_checksum,
            "configuration_checksum": identity["configurationChecksum"],
            "upstream_revision": self.upstream_revision,
            "model_revision": self.model_revision,
            "tokenizer_revision": self.tokenizer_revision,
            "seed": self.seed,
            "path_count": self.path_count,
            "sampling": self.sampling,
            "threshold_policy_version": identity["thresholdPolicyVersion"],
            "sampling_pool_checksum": self.sampling_pool_checksum,
            "decision_at": self.decision_at,
            "available_at": self.available_at,
            "computed_at": self.computed_at,
            "source_max_timestamp": self.source_max_timestamp,
            "point_in_time_status": self.point_in_time_status,
        }


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def checksum_payload(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def persistence_content(record: dict[str, Any]) -> str:
    """Compare deterministic output, retaining the FIRST observed availability.

    Rerun wall clocks and latency are not new evidence; never overwrite the first
    committed result or upgrade its point-in-time classification.
    """
    from copy import deepcopy
    value = deepcopy(record)
    for key in ("computed_at", "available_at", "created_at", "point_in_time_status"):
        value.pop(key, None)
    for key in ("computedAt", "availableAt", "pointInTimeStatus", "kronosLatencyMs"):
        value.get("summary", {}).pop(key, None)
    return canonical_json(value)


def prospective_status(available_at: datetime, decision_at: datetime) -> str:
    """Never claim a research result was available at a decision it missed."""
    return (PROSPECTIVE_DECISION_AVAILABLE if available_at <= decision_at
            else PROSPECTIVE_POST_DECISION)


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
        ambiguous_count = directions.count("AMBIGUOUS")
        p_meaningful = meaningful / len(materialized)
        p_up = up_count / meaningful if meaningful else 0.0
        p_down = down_count / meaningful if meaningful else 0.0
        p_ambiguous = ambiguous_count / meaningful if meaningful else 0.0
        direction = "UP" if p_up > p_down else "DOWN" if p_down > p_up else "NEUTRAL"
        result[horizon] = ResearchHorizon(
            horizon_minutes=horizon,
            threshold=threshold,
            threshold_policy_version=selected_policy.version,
            p_meaningful_move=p_meaningful,
            p_up_given_meaningful_move=p_up,
            p_down_given_meaningful_move=p_down,
            p_ambiguous_barrier=p_ambiguous,
            direction=direction,
            expected_return=float(np.mean(returns)),
            expected_magnitude=float(np.mean([max(up, down) for up, down in
                zip(up_excursions, down_excursions) if max(up, down) >= threshold])) if meaningful else 0.0,
            median=percentile(returns, 0.50),
            p05=percentile(returns, 0.05),
            p25=percentile(returns, 0.25),
            p50=percentile(returns, 0.50),
            p75=percentile(returns, 0.75),
            p95=percentile(returns, 0.95),
            max_up_excursion=max(up_excursions),
            max_down_excursion=max(down_excursions),
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
