from datetime import datetime, timedelta, timezone
from decimal import Decimal

import numpy as np
from fastapi.testclient import TestClient

from banknifty_foundation.big_move_features import build_big_move_features, classify_futures_buildup
from banknifty_foundation.big_move_labels import calculate_big_move_label, default_big_move_policy
from banknifty_foundation.big_move_models import train_two_stage_model
from banknifty_foundation.internal_api import app, runtime
from banknifty_foundation.live_models import DepthLevel, OptionMinuteSnapshot
from banknifty_foundation.models import Candle, TradingSession


START = datetime(2025, 1, 2, 3, 45, tzinfo=timezone.utc)
SESSION = TradingSession(START.date(), START, START + timedelta(hours=6, minutes=15), "OPEN")


def candle(offset: int, close: str, *, high: str | None = None, low: str | None = None, volume: int = 100) -> Candle:
    value = Decimal(close)
    return Candle(START + timedelta(minutes=offset), value, Decimal(high or close), Decimal(low or close), value, volume, 1000 + offset)


def option(offset: int, side: str, strike: str, oi: int, oi_change: int, atm_offset: int = 0) -> OptionMinuteSnapshot:
    bid = (DepthLevel(Decimal("99"), 20),)
    ask = (DepthLevel(Decimal("101"), 10),)
    return OptionMinuteSnapshot(f"{offset}-{side}-{strike}", offset + (1 if side == "CE" else 100),
        START + timedelta(minutes=offset), "universe", START.date(), Decimal(strike), side,
        atm_offset, Decimal("100"), 1000, 100, oi, oi_change, Decimal("99"), 20,
        Decimal("101"), 10, Decimal("2"), Decimal("0.02"), bid, ask, 20, 10,
        Decimal("0.1"), (), "PASS")


def test_big_move_label_uses_full_path_and_first_touch():
    history = [candle(index, str(100 + index / 100)) for index in range(21)]
    as_of = history[-1]
    future = [candle(21 + index, "100.2", high="100.2", low="100.1") for index in range(10)]
    future[2] = candle(23, "100.4", high="100.40", low="100.1")
    label = calculate_big_move_label(dataset_revision_id="revision", instrument_id=1,
        as_of_candle=as_of, horizon=10, session=SESSION, candles_known_at_as_of=history,
        future_candles=future, policy=default_big_move_policy())
    assert label is not None and label.label == "BIG_UP"
    assert label.first_barrier == "UP" and label.first_barrier_at == START + timedelta(minutes=23)
    assert label.max_future_high == Decimal("100.40")


def test_same_minute_double_barrier_is_ambiguous():
    history = [candle(index, "100") for index in range(21)]
    future = [candle(21 + index, "100", high="100.1", low="99.9") for index in range(10)]
    future[0] = candle(21, "100", high="100.3", low="99.7")
    label = calculate_big_move_label(dataset_revision_id="revision", instrument_id=1,
        as_of_candle=history[-1], horizon=10, session=SESSION, candles_known_at_as_of=history,
        future_candles=future, policy=default_big_move_policy())
    assert label is not None and label.label == "AMBIGUOUS"
    assert label.first_barrier == "BOTH_SAME_MINUTE"


def test_threshold_rejects_future_history_and_missing_path_is_unknown():
    history = [candle(index, "100") for index in range(21)]
    future_candle = candle(21, "120")
    try:
        calculate_big_move_label(dataset_revision_id="revision", instrument_id=1,
            as_of_candle=history[-1], horizon=10, session=SESSION,
            candles_known_at_as_of=[*history, future_candle], future_candles=[],
            policy=default_big_move_policy())
        assert False, "future threshold input must fail"
    except ValueError as error:
        assert "future data" in str(error)
    missing = calculate_big_move_label(dataset_revision_id="revision", instrument_id=1,
        as_of_candle=history[-1], horizon=10, session=SESSION, candles_known_at_as_of=history,
        future_candles=[], policy=default_big_move_policy())
    assert missing is not None and missing.label == "UNKNOWN"


def test_v2_features_are_point_in_time_and_include_price_volatility():
    history = [candle(index, str(100 + index / 10), high=str(100.1 + index / 10), low=str(99.9 + index / 10)) for index in range(35)]
    as_of = history[-1].timestamp
    first = build_big_move_features(as_of=as_of, index_candles=history, session=SESSION)
    future_changed = build_big_move_features(as_of=as_of, index_candles=[*history, candle(35, "1000")], session=SESSION)
    assert first.values == future_changed.values
    assert first.source_max_timestamp == as_of
    assert first.values["return_30m"] is not None and first.values["realized_volatility_20m"] is not None
    assert first.values["vix_data_status"] == "VIX_DATA_PENDING"


def test_futures_and_option_features_use_only_snapshots_at_as_of():
    index = [candle(i, str(100 + i / 10)) for i in range(7)]
    futures = [candle(i, str(101 + i / 10), volume=200 + i) for i in range(7)]
    options = [option(6, "CE", "100", 1200, 100), option(6, "PE", "100", 1800, 300),
               option(6, "CE", "101", 2000, 200, 1), option(6, "PE", "101", 900, 50, 1),
               option(6, "CE", "99", 700, 25, -1), option(6, "PE", "99", 2200, 400, -1),
               option(7, "CE", "100", 999999, 999999)]
    result = build_big_move_features(as_of=index[-1].timestamp, index_candles=index,
        futures_candles=futures, option_snapshots=options, session=SESSION)
    assert result.values["futures_index_premium"] == Decimal("1.0")
    assert result.values["pcr_oi"] == Decimal("4900") / Decimal("3900")
    assert result.values["selected_universe_completeness"] == Decimal("1")
    assert result.values["iv_data_status"] == "IV_DATA_PENDING"
    assert classify_futures_buildup(Decimal("0.01"), Decimal("10")) == "LONG_BUILDUP"


def test_two_stage_logistic_model_emits_separate_move_and_direction_probabilities():
    x = np.asarray([[i, i % 3] for i in range(12)], dtype=float)
    labels = ["NO_BIG_MOVE", "BIG_DOWN", "BIG_UP"] * 4
    model = train_two_stage_model(horizon=20, train_x=x, labels=labels,
        realized_excursions=[0.001, 0.004, 0.005] * 4,
        feature_schema=("momentum", "regime"))
    result = model.predict([7.0, 1.0], 50000)
    assert 0 <= result["bigMoveProbability"] <= 1
    assert abs(result["upGivenBigMoveProbability"] + result["downGivenBigMoveProbability"] - 1) < 1e-9
    assert result["modelVersion"].startswith("big-move-v2")


def test_v2_internal_api_has_three_honest_horizons(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_TOKEN", "v2-test-token")
    runtime.running = False
    runtime.repository = None
    client = TestClient(app)
    response = client.get("/internal/intelligence/current", headers={"X-Internal-Token": "v2-test-token"})
    assert response.status_code == 200
    body = response.json()
    assert [item["minutes"] for item in body["horizons"]] == [10, 20, 30]
    assert all(item["bigMoveProbability"] is None for item in body["horizons"])
    assert body["vixStatus"] == "VIX_DATA_PENDING"
