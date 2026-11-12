from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import math
import os
from typing import Any, Callable, Sequence

import httpx

from .big_move_labels import BigMoveThresholdPolicy, default_big_move_policy
from .external_research import (ExternalForecastResult, HISTORICAL_COUNTERFACTUAL,
                                KRONOS_MODEL_NAME, aggregate_paths, checksum_payload,
                                median_path)
from .models import Candle


KRONOS_UPSTREAM_SHA = "67b630e67f6a18c9e9be918d9b4337c960db1e9a"
KRONOS_MODEL_SHA256 = "A7D5F37E2E9FBD9891F7D7D4F72574512DD1F704FEE14223E0A8CD0FBF54197C"
KRONOS_TOKENIZER_SHA256 = "B97EC46B3B72160509E289183EAF7BDF5F0DAC5BB9B49522F6D46638A99A8717"


def _enabled(value: str | None) -> bool:
    return (value or "false").strip().lower() == "true"


class KronosForecastAdapter:
    """Optional research client for the local vendored Kronos Docker service.

    Normal startup never imports Torch and never downloads a model. The model
    runs in a separate CPU sidecar built from `third_party/Kronos/`.
    """

    model_name = KRONOS_MODEL_NAME
    model_version = "Kronos-mini"
    upstream_sha = KRONOS_UPSTREAM_SHA

    def __init__(
        self, predictor_factory: Callable[[], Any] | None = None, *,
        model_name: str = KRONOS_MODEL_NAME, service_url: str | None = None,
        enabled: bool | None = None, sample_paths: int | None = None,
        seed: int | None = None, timeout_seconds: float | None = None,
        policy: BigMoveThresholdPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.predictor_factory = predictor_factory
        self.model_name = KRONOS_MODEL_NAME
        self.display_model = model_name
        self.service_url = (service_url or os.getenv(
            "KRONOS_SERVICE_URL", "http://kronos-research:8010")).rstrip("/")
        self.enabled = _enabled(os.getenv("KRONOS_RESEARCH_ENABLED")) if enabled is None else enabled
        self.sample_paths = max(1, min(32, int(sample_paths or os.getenv("KRONOS_SAMPLE_PATHS", "8"))))
        self.seed = int(seed if seed is not None else os.getenv("KRONOS_SEED", "1729"))
        self.timeout_seconds = float(timeout_seconds or os.getenv("KRONOS_TIMEOUT_SECONDS", "90"))
        self.policy = policy or default_big_move_policy()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def availability(self) -> dict[str, Any]:
        """Cheap configuration status; never blocks a production request on Kronos."""
        if self.predictor_factory is not None:
            return {"available": True, "status": "READY", "reason": None,
                    "model": self.model_name, "modelVersion": self.model_version,
                    "source": "IN_PROCESS_TEST_FACTORY"}
        if not self.enabled:
            return {"available": False, "status": "DISABLED", "reason": "KRONOS_RESEARCH_DISABLED",
                    "model": self.model_name, "modelVersion": self.model_version,
                    "serviceUrl": self.service_url}
        return {"available": True, "status": "READY", "reason": None,
                "model": self.model_name, "modelVersion": self.model_version,
                "serviceUrl": self.service_url, "mode": "OHLC_ONLY",
                "volumeStatus": "INDEX_VOLUME_NOT_USED"}

    def health(self) -> dict[str, Any]:
        if self.predictor_factory is not None:
            return self.availability()
        if not self.enabled:
            return self.availability()
        try:
            response = httpx.get(self.service_url + "/health", timeout=3.0)
            payload = response.json()
            status = payload.get("status", "SERVICE_UNAVAILABLE")
            return {"available": status == "READY", "status": status,
                    "reason": payload.get("detail") if status != "READY" else None,
                    "model": self.model_name, "modelVersion": self.model_version,
                    "serviceUrl": self.service_url, "detail": payload}
        except Exception as error:
            return {"available": False, "status": "SERVICE_UNAVAILABLE",
                    "reason": f"{type(error).__name__}: {error}", "model": self.model_name,
                    "modelVersion": self.model_version, "serviceUrl": self.service_url}

    def _configuration(self, paths: int, pred_len: int = 30) -> dict[str, Any]:
        return {"samplePaths": int(paths), "predLen": int(pred_len), "seed": self.seed,
                "lookback": 400, "thresholdPolicyVersion": self.policy.version,
                "mode": "OHLC_ONLY", "samplingMethod": "SEPARATE_SEEDED_SAMPLE_COUNT_1"}

    def _failure(self, status: str, reference: datetime, detail: str,
                 point_in_time_status: str = HISTORICAL_COUNTERFACTUAL,
                 input_checksum: str | None = None) -> ExternalForecastResult:
        now = self.clock()
        return ExternalForecastResult(self.model_name, self.model_version, reference, reference,
            now, now, reference, point_in_time_status, status, {}, (),
            {"researchStatus": status, "statusDetail": detail, "pathCount": 0,
             "futureBars": 30, "mode": "OHLC_ONLY",
             "volumeStatus": "INDEX_VOLUME_NOT_USED", "usedByPriceCoreV2": False,
             "serviceUrl": self.service_url, "upstreamRevision": self.upstream_sha,
             "modelRevision": KRONOS_MODEL_SHA256,
             "tokenizerRevision": KRONOS_TOKENIZER_SHA256,
             "inputChecksum": input_checksum},
            input_checksum=input_checksum, configuration=self._configuration(0),
            upstream_revision=self.upstream_sha, model_revision=KRONOS_MODEL_SHA256,
            tokenizer_revision=KRONOS_TOKENIZER_SHA256, seed=self.seed,
            threshold_policy_version=self.policy.version)

    def forecast(self, candles: Sequence[Candle], *, as_of: datetime, lookback: int = 400,
                 pred_len: int = 30, sample_count: int | None = None,
                 point_in_time_status: str = HISTORICAL_COUNTERFACTUAL,
                 session_open: datetime | None = None,
                 session_close: datetime | None = None) -> ExternalForecastResult:
        if pred_len != 30:
            raise ValueError("the V2 Kronos experiment requires a 30-minute forecast")
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        reference = as_of.astimezone(timezone.utc).replace(second=0, microsecond=0)
        from .calendar import IST
        local = reference.astimezone(IST)
        session_open = session_open or local.replace(hour=9, minute=15)
        session_close = session_close or local.replace(hour=15, minute=30)
        if session_close is not None:
            close = session_close.astimezone(timezone.utc)
            if reference + timedelta(minutes=pred_len) >= close:
                return self._failure("SESSION_BOUNDARY", reference,
                                     "30-minute forecast crosses the NSE session close",
                                     point_in_time_status)
        history = sorted((item for item in candles if item.timestamp <= reference),
                         key=lambda item: item.timestamp)
        if session_open is not None:
            opened = session_open.astimezone(timezone.utc)
            history = [item for item in history if item.timestamp >= opened]
        if not history or history[-1].timestamp != reference:
            raise ValueError("completed OHLC history through as_of is required")
        if len({item.timestamp for item in history}) != len(history):
            raise ValueError("duplicate candle timestamps are not allowed")
        history = history[-max(2, min(lookback, 2048)):]
        if any(right.timestamp - left.timestamp != timedelta(minutes=1)
               for left, right in zip(history, history[1:])):
            return self._failure("INPUT_GAP", reference, "minute gap in input window",
                                 point_in_time_status)
        required_history = self.policy.volatility_lookback_minutes + 1
        if len(history) < required_history:
            return self._failure("INPUT_GAP", reference,
                                 f"contiguous history has {len(history)} candles; "
                                 f"at least {required_history} required",
                                 point_in_time_status)
        history = history[-max(2, min(lookback, 2048)):]
        input_checksum = checksum_payload({
            "model": self.model_name, "referenceMinute": reference.isoformat(),
            "candles": [{"timestamp": item.timestamp.isoformat(), "open": str(item.open),
                         "high": str(item.high), "low": str(item.low), "close": str(item.close)}
                        for item in history],
        })
        paths = sample_count or self.sample_paths
        if self.predictor_factory is not None:
            result = self._local_forecast(history, reference, paths, point_in_time_status, input_checksum)
            return replace(result, configuration={**result.configuration, "lookback": lookback})
        if not self.enabled:
            return self._failure("DISABLED", reference, "KRONOS_RESEARCH_DISABLED",
                                 point_in_time_status, input_checksum)
        payload = {
            "candles": [{"timestamp": item.timestamp.isoformat(), "open": float(item.open),
                         "high": float(item.high), "low": float(item.low),
                         "close": float(item.close)} for item in history],
            "reference_minute": reference.isoformat(),
            "pred_len": pred_len,
            "sample_paths": paths,
            "seed": self.seed,
        }
        try:
            response = httpx.post(self.service_url + "/predict", json=payload,
                                  timeout=self.timeout_seconds)
            if response.status_code == 503:
                return self._failure("SERVICE_UNAVAILABLE", reference,
                                     response.text[:500], point_in_time_status, input_checksum)
            if response.status_code >= 400:
                return self._failure("FAILED", reference, response.text[:500],
                                     point_in_time_status, input_checksum)
            result = response.json()
        except Exception as error:
            return self._failure("SERVICE_UNAVAILABLE", reference,
                                 f"{type(error).__name__}: {error}", point_in_time_status,
                                 input_checksum)
        if (result.get("modelRevision") != KRONOS_MODEL_SHA256
                or result.get("tokenizerRevision") != KRONOS_TOKENIZER_SHA256
                or result.get("upstreamRevision") != self.upstream_sha
                or result.get("seed") != self.seed
                or result.get("pathCount") != paths):
            return self._failure("FAILED", reference, "Kronos response identity mismatch",
                                 point_in_time_status, input_checksum)
        output = self._result(history, reference, result, point_in_time_status, input_checksum)
        return replace(output, configuration={**output.configuration, "lookback": lookback})

    def _local_forecast(self, history: Sequence[Candle], reference: datetime, paths: int,
                        point_in_time_status: str,
                        input_checksum: str | None = None) -> ExternalForecastResult:
        import pandas as pd

        selected = list(history)
        frame = pd.DataFrame([{"open": float(item.open), "high": float(item.high),
                               "low": float(item.low), "close": float(item.close)}
                              for item in selected])
        x_timestamp = pd.Series([item.timestamp for item in selected])
        y_timestamp = pd.Series([reference + timedelta(minutes=offset) for offset in range(1, 31)])
        predicted = self.predictor_factory().predict(frame, x_timestamp, y_timestamp,
            pred_len=30, sample_count=max(1, paths), verbose=False)
        if len(predicted) != 30:
            return self._failure("FAILED", reference, "predictor returned an invalid path",
                                 point_in_time_status, input_checksum)
        path = [{"timestamp": y_timestamp.iloc[index].isoformat(),
                 "open": float(predicted.iloc[index]["open"]),
                 "high": float(predicted.iloc[index]["high"]),
                 "low": float(predicted.iloc[index]["low"]),
                 "close": float(predicted.iloc[index]["close"])} for index in range(30)]
        return self._result(history, reference, {
            "status": "COMPLETED", "paths": [path], "pathCount": 1,
            "seed": self.seed, "seedPolicy": "LOCAL_TEST_FACTORY",
            "latencyMs": 0.0, "modelName": self.model_name,
            "modelVersion": self.model_version, "mode": "OHLC_ONLY",
            "volumeStatus": "INDEX_VOLUME_NOT_USED",
        }, point_in_time_status, input_checksum)

    def _result(self, history: Sequence[Candle], reference: datetime, payload: dict[str, Any],
                point_in_time_status: str,
                input_checksum: str | None = None) -> ExternalForecastResult:
        paths = payload.get("paths")
        if not isinstance(paths, list) or not paths:
            return self._failure("FAILED", reference, "Kronos returned no sample paths",
                                 point_in_time_status, input_checksum)
        validated: list[list[dict[str, Any]]] = []
        for path in paths:
            if not isinstance(path, list) or len(path) != 30:
                return self._failure("FAILED", reference, "Kronos returned an invalid path length",
                                     point_in_time_status, input_checksum)
            converted: list[dict[str, Any]] = []
            for index, row in enumerate(path):
                try:
                    timestamp = datetime.fromisoformat(str(row["timestamp"]).replace("Z", "+00:00"))
                    values = [float(row[name]) for name in ("open", "high", "low", "close")]
                except (KeyError, TypeError, ValueError):
                    return self._failure("FAILED", reference, "Kronos returned malformed OHLC rows",
                                         point_in_time_status, input_checksum)
                if (timestamp.tzinfo is None or timestamp.utcoffset() is None
                        or timestamp != reference + timedelta(minutes=index + 1)):
                    return self._failure("FAILED", reference,
                                         "Kronos path is outside the future window",
                                         point_in_time_status, input_checksum)
                opened, high, low, close = values
                if any(value <= 0 or not math.isfinite(value) for value in values):
                    return self._failure("FAILED", reference, "Kronos returned invalid OHLC",
                                         point_in_time_status, input_checksum)
                if high < max(opened, close) or low > min(opened, close) or high < low:
                    return self._failure("FAILED", reference, "Kronos returned inconsistent OHLC",
                                         point_in_time_status, input_checksum)
                converted.append({"timestamp": timestamp.isoformat(), "open": opened,
                                  "high": high, "low": low, "close": close})
            validated.append(converted)
        reference_price = float(history[-1].close)
        horizons = aggregate_paths(validated, reference_price=reference_price,
            known_closes=[float(item.close) for item in history], policy=self.policy)
        now = self.clock()
        latency = float(payload.get("latencyMs") or 0.0)
        seed = int(payload.get("seed", self.seed))
        sampling = payload.get("sampling", {})
        policy_versions = sorted({item.threshold_policy_version for item in horizons.values()})
        summary = {
            "researchStatus": "COMPLETED",
            "pathCount": len(validated),
            "seed": seed,
            "seedPolicy": payload.get("seedPolicy", "SEED_PLUS_PATH_INDEX"),
            "sampling": sampling,
            "mode": "OHLC_ONLY",
            "volumeStatus": "INDEX_VOLUME_NOT_USED",
            "kronosLatencyMs": latency,
            "modelSource": "third_party/Kronos",
            "upstreamSha": self.upstream_sha,
            "upstreamRevision": self.upstream_sha,
            "modelRevision": KRONOS_MODEL_SHA256,
            "tokenizerRevision": KRONOS_TOKENIZER_SHA256,
            "serviceUrl": self.service_url,
            "inputChecksum": input_checksum,
            "usedByPriceCoreV2": False,
        }
        return ExternalForecastResult(self.model_name, self.model_version, reference, reference,
            now, now, reference, point_in_time_status, "COMPLETED", horizons,
            median_path(validated), summary, input_checksum=input_checksum,
            reference_state_checksum=checksum_payload({
                "reference": {"timestamp": reference.isoformat(),
                              "open": str(history[-1].open), "high": str(history[-1].high),
                              "low": str(history[-1].low), "close": str(history[-1].close)}}),
            configuration={**self._configuration(len(validated)),
                           "sampling": sampling, "seedPolicy": payload.get("seedPolicy")},
            upstream_revision=self.upstream_sha, model_revision=KRONOS_MODEL_SHA256,
            tokenizer_revision=KRONOS_TOKENIZER_SHA256, seed=seed,
            threshold_policy_version=(policy_versions[0] if len(policy_versions) == 1 else None))
