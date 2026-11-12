"""Local-only CPU Kronos research sidecar.

This file is product integration added on top of the vendored Kronos source.
It is not part of upstream Kronos. It loads the committed Kronos-mini and
Kronos-Tokenizer-2k files, validates that no future candle is supplied, and
returns multiple independently seeded sample paths for research aggregation.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import numpy as np
import pandas as pd
import torch

from model import Kronos, KronosPredictor, KronosTokenizer


MODEL_NAME = "KRONOS_MINI_ZERO_SHOT_V1"
MODEL_DIR = os.getenv("KRONOS_MODEL_DIR", "/app/models/Kronos-mini")
TOKENIZER_DIR = os.getenv("KRONOS_TOKENIZER_DIR", "/app/models/Kronos-Tokenizer-2k")
THREADS = max(1, min(4, int(os.getenv("KRONOS_THREADS", "2"))))
MAX_CONTEXT = 2048
MAX_CANDLES = 2048
MAX_PATHS = 32
MIN_PATHS = 1
LOCK = threading.Lock()
STATE: dict[str, Any] = {
    "status": "SERVICE_UNAVAILABLE",
    "detail": None,
    "device": "cpu",
    "model": MODEL_NAME,
    "modelDir": MODEL_DIR,
    "tokenizerDir": TOKENIZER_DIR,
    "threads": THREADS,
}
PREDICTOR: KronosPredictor | None = None


def _load() -> None:
    global PREDICTOR
    torch.set_num_threads(THREADS)
    try:
        if not os.path.isdir(MODEL_DIR) or not os.path.isdir(TOKENIZER_DIR):
            STATE.update(status="MODEL_NOT_DOWNLOADED", detail="local model cache missing")
            return
        tokenizer = KronosTokenizer.from_pretrained(TOKENIZER_DIR)
        model = Kronos.from_pretrained(MODEL_DIR)
        PREDICTOR = KronosPredictor(model, tokenizer, device="cpu", max_context=MAX_CONTEXT)
        STATE.update(status="READY", detail=None)
    except Exception as error:  # pragma: no cover - exercised only in container
        STATE.update(status="SERVICE_UNAVAILABLE", detail=f"{type(error).__name__}: {error}")


def _parse_time(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamps must include an offset")
    return parsed


def _candles(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], datetime]:
    rows = payload.get("candles")
    if not isinstance(rows, list) or not rows:
        raise ValueError("candles must be a non-empty list")
    if len(rows) > MAX_CANDLES:
        raise ValueError(f"candles must not exceed {MAX_CANDLES} rows")
    reference = _parse_time(payload.get("reference_minute"))
    result: list[dict[str, Any]] = []
    previous: datetime | None = None
    for row in rows:
        timestamp = _parse_time(row.get("timestamp"))
        if timestamp > reference:
            raise ValueError("future candle supplied")
        if previous is not None and timestamp <= previous:
            raise ValueError("candles must be strictly ascending")
        values = [float(row.get(name)) for name in ("open", "high", "low", "close")]
        if any(not np.isfinite(value) or value <= 0 for value in values):
            raise ValueError("OHLC values must be finite and positive")
        if values[1] < max(values[0], values[3]) or values[2] > min(values[0], values[3]):
            raise ValueError("invalid OHLC bar")
        result.append({"timestamp": timestamp, "open": values[0], "high": values[1],
                       "low": values[2], "close": values[3]})
        previous = timestamp
    if result[-1]["timestamp"] != reference:
        raise ValueError("reference_minute must match the last completed candle")
    return result, reference


def _predict(payload: dict[str, Any]) -> dict[str, Any]:
    if PREDICTOR is None:
        raise RuntimeError(STATE["status"])
    candles, reference = _candles(payload)
    pred_len = int(payload.get("pred_len", 30))
    sample_paths = int(payload.get("sample_paths", 8))
    seed = int(payload.get("seed", 1729))
    temperature = float(payload.get("temperature", 1.0))
    top_p = float(payload.get("top_p", 0.9))
    top_k = int(payload.get("top_k", 0))
    if pred_len != 30:
        raise ValueError("the BANKNIFTY research contract requires pred_len=30")
    if not MIN_PATHS <= sample_paths <= MAX_PATHS:
        raise ValueError(f"sample_paths must be between {MIN_PATHS} and {MAX_PATHS}")
    if not 0 < temperature <= 5 or not 0 < top_p <= 1 or not 0 <= top_k <= 1000:
        raise ValueError("invalid sampling controls")
    frame = pd.DataFrame([{key: row[key] for key in ("open", "high", "low", "close")}
                          for row in candles])
    x_timestamp = pd.Series([row["timestamp"] for row in candles])
    y_timestamp = pd.Series([reference + timedelta(minutes=offset) for offset in range(1, pred_len + 1)])
    started = time.perf_counter()
    paths: list[list[dict[str, Any]]] = []
    for index in range(sample_paths):
        torch.manual_seed(seed + index)
        predicted = PREDICTOR.predict(frame, x_timestamp, y_timestamp, pred_len=pred_len,
            T=temperature, top_k=top_k, top_p=top_p, sample_count=1, verbose=False)
        if len(predicted) != pred_len:
            raise ValueError("Kronos returned an invalid forecast path")
        paths.append([
            {"timestamp": y_timestamp.iloc[offset].isoformat(),
             "open": float(predicted.iloc[offset]["open"]),
             "high": float(predicted.iloc[offset]["high"]),
             "low": float(predicted.iloc[offset]["low"]),
             "close": float(predicted.iloc[offset]["close"])}
            for offset in range(pred_len)
        ])
    return {
        "status": "COMPLETED",
        "modelName": MODEL_NAME,
        "modelVersion": "Kronos-mini",
        "tokenizerVersion": "Kronos-Tokenizer-2k",
        "device": "cpu",
        "paths": paths,
        "pathCount": len(paths),
        "seed": seed,
        "seedPolicy": "SEED_PLUS_PATH_INDEX",
        "sampling": {"temperature": temperature, "topK": top_k, "topP": top_p},
        "mode": "OHLC_ONLY",
        "volumeStatus": "INDEX_VOLUME_NOT_USED",
        "latencyMs": round((time.perf_counter() - started) * 1000, 3),
        "sourceMaxTimestamp": candles[-1]["timestamp"].isoformat(),
        "referenceMinute": reference.isoformat(),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "KronosResearch/1.0"

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send(200 if STATE["status"] == "READY" else 503, dict(STATE))
            return
        self._send(404, {"error": "NOT_FOUND"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/predict":
            self._send(404, {"error": "NOT_FOUND"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 8_000_000:
                raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(length))
            with LOCK:
                result = _predict(payload)
            self._send(200, result)
        except ValueError as error:
            self._send(422, {"error": "INVALID_INPUT", "detail": str(error)})
        except Exception as error:
            self._send(503, {"error": "KRONOS_SERVICE_UNAVAILABLE",
                             "detail": f"{type(error).__name__}: {error}"})

    def log_message(self, format: str, *args: Any) -> None:
        return


def main() -> None:
    _load()
    server = ThreadingHTTPServer(("0.0.0.0", int(os.getenv("KRONOS_PORT", "8010"))), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
