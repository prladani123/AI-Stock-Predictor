from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from importlib.util import find_spec
from typing import Any, Callable, Sequence

from .models import Candle


@dataclass(frozen=True)
class ExperimentalForecast:
    model_name: str
    as_of: datetime
    forecast_path: tuple[dict[str, Any], ...]
    forecast_returns: dict[int, float]
    maximum_up_excursion: float
    maximum_down_excursion: float
    predicted_range_width: float
    status: str = "EXPERIMENTAL_MODEL_OUTPUT"


class KronosForecastAdapter:
    """Optional adapter: normal startup never imports Torch or downloads a model."""

    model_family = "KRONOS_EXPERIMENTAL"
    model_version = "kronos-mini-or-small-unconfigured"

    def __init__(self, predictor_factory: Callable[[], Any] | None = None, *, model_name: str = "Kronos-mini") -> None:
        self.predictor_factory = predictor_factory
        self.model_name = model_name

    def availability(self) -> dict[str, Any]:
        if self.predictor_factory is not None:
            return {"available": True, "reason": None, "model": self.model_name}
        try:
            installed = find_spec("torch") is not None and find_spec("model.kronos") is not None
        except ModuleNotFoundError:
            installed = False
        if not installed:
            return {"available": False, "reason": "KRONOS_OPTIONAL_DEPENDENCY_OR_MODEL_UNAVAILABLE", "model": self.model_name}
        return {"available": False, "reason": "KRONOS_PREDICTOR_FACTORY_NOT_CONFIGURED", "model": self.model_name}

    def forecast(self, candles: Sequence[Candle], *, as_of: datetime, lookback: int = 400,
                 pred_len: int = 30, sample_count: int = 5) -> ExperimentalForecast:
        if pred_len != 30:
            raise ValueError("the V2 Kronos experiment requires a 30-minute forecast")
        history = sorted((item for item in candles if item.timestamp <= as_of), key=lambda item: item.timestamp)
        if not history or history[-1].timestamp != as_of:
            raise ValueError("completed OHLC history through as_of is required")
        if self.predictor_factory is None:
            raise RuntimeError(self.availability()["reason"])
        import pandas as pd
        selected = history[-lookback:]
        frame = pd.DataFrame([{"open": float(item.open), "high": float(item.high), "low": float(item.low),
                               "close": float(item.close), "volume": item.volume} for item in selected])
        x_timestamp = pd.Series([item.timestamp for item in selected])
        y_timestamp = pd.Series([as_of + timedelta(minutes=offset) for offset in range(1, 31)])
        predicted = self.predictor_factory().predict(frame, x_timestamp, y_timestamp,
            pred_len=30, sample_count=sample_count, verbose=False)
        required = {"open", "high", "low", "close"}
        if not required.issubset(predicted.columns) or len(predicted) != 30:
            raise ValueError("Kronos returned an invalid forecast path")
        reference = float(history[-1].close)
        path = tuple({"timestamp": y_timestamp.iloc[index].isoformat(),
                      **{name: float(predicted.iloc[index][name]) for name in required}}
                     for index in range(30))
        returns = {horizon: float(predicted.iloc[horizon - 1]["close"]) / reference - 1 for horizon in (10, 20, 30)}
        max_up = max(float(row["high"]) for row in path) / reference - 1
        max_down = 1 - min(float(row["low"]) for row in path) / reference
        return ExperimentalForecast(self.model_name, as_of, path, returns, max_up, max_down, max_up + max_down)
