from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from math import log
from statistics import pstdev
from typing import Any, Sequence

from .live_models import FinalizedMinute, OptionMinuteSnapshot
from .models import Candle, TradingSession


FEATURE_SET_VERSION_V2 = "banknifty-bigmove-v2"
RETURN_WINDOWS = (1, 2, 3, 5, 10, 15, 20, 30)
VOLATILITY_WINDOWS = (5, 10, 20)
VIX_DATA_STATUS = "VIX_DATA_PENDING"


def big_move_feature_definition() -> dict[str, Any]:
    return {
        "version": FEATURE_SET_VERSION_V2,
        "purpose": "detect meaningful 10/20/30-minute expansion, then direction",
        "availability_rule": "every input timestamp is less than or equal to as_of",
        "price_features": [
            *[f"return_{value}m" for value in RETURN_WINDOWS],
            *[f"realized_volatility_{value}m" for value in VOLATILITY_WINDOWS],
            "atr_normalized_14m", "range_expansion_ratio", "current_range_pct",
            "ema_slope_5m", "ema_fast_slow_spread", "macd_spread",
            "breakout_high_distance_20m", "breakout_low_distance_20m",
            "candle_body_pct", "upper_wick_pct", "lower_wick_pct",
            "consecutive_candle_direction", "consecutive_candle_count", "volume_spike_ratio",
            "session_vwap_distance", "minutes_since_market_open", "minutes_until_market_close",
            "opening_range_position",
        ],
        "futures_features": ["futures_index_premium", "futures_index_premium_pct",
            "futures_premium_change_1m", "futures_premium_change_5m", "futures_oi",
            "futures_oi_change_1m", "futures_oi_change_5m", "futures_volume_delta", "futures_buildup"],
        "option_features": ["pcr_oi", "oi_change_imbalance", "volume_pcr",
            "atm_ce_oi", "atm_pe_oi", "atm_ce_oi_change", "atm_pe_oi_change",
            "atm_straddle_premium", "atm_straddle_premium_change", "mean_bid_ask_spread_pct",
            "aggregate_depth_imbalance", "call_oi_wall", "put_oi_wall",
            "distance_to_call_oi_wall", "distance_to_put_oi_wall",
            "oi_concentration_entropy", "selected_universe_completeness"],
        "unavailable_slots": {"india_vix": VIX_DATA_STATUS, "implied_volatility": "IV_DATA_PENDING"},
        "null_policy": "NO_FILL_EXCLUDE_REQUIRED_UNAVAILABLE_ROWS",
    }


def register_big_move_feature_set(repository: object, created_at: datetime) -> None:
    from .repository import digest
    from .research_models import FeatureSetDefinition
    definition = big_move_feature_definition()
    repository.save_feature_set(FeatureSetDefinition(
        FEATURE_SET_VERSION_V2, created_at, definition, digest(definition)))


@dataclass(frozen=True)
class BigMoveFeatureSnapshot:
    as_of: datetime
    source_max_timestamp: datetime
    values: dict[str, Any]
    market_regime: str
    session_bias: dict[str, Any]
    quality_status: str


def _time(bar: Candle | FinalizedMinute) -> datetime:
    return bar.timestamp if isinstance(bar, Candle) else bar.minute_at


def _volume(bar: Candle | FinalizedMinute) -> int | None:
    return bar.volume if isinstance(bar, Candle) else bar.volume_delta


def _oi(bar: Candle | FinalizedMinute) -> int | None:
    return bar.oi if isinstance(bar, Candle) else bar.oi_close


def _ema(values: Sequence[Decimal], period: int) -> Decimal | None:
    if len(values) < period:
        return None
    alpha = Decimal("2") / Decimal(period + 1)
    result = sum(values[:period], Decimal("0")) / Decimal(period)
    for value in values[period:]:
        result = alpha * value + (Decimal("1") - alpha) * result
    return result


def _return_at(index_by_time: dict[datetime, Candle | FinalizedMinute], as_of: datetime,
               current_close: Decimal, window: int) -> Decimal | None:
    prior = index_by_time.get(as_of - timedelta(minutes=window))
    return current_close / prior.close - Decimal("1") if prior is not None and prior.close > 0 else None


def _realized_vol_at(index_by_time: dict[datetime, Candle | FinalizedMinute], as_of: datetime,
                     window: int) -> Decimal | None:
    bars = [index_by_time.get(as_of - timedelta(minutes=offset)) for offset in range(window, -1, -1)]
    if any(item is None for item in bars):
        return None
    complete = [item for item in bars if item is not None]
    values = [float(complete[i].close / complete[i - 1].close - Decimal("1")) for i in range(1, len(complete))]
    return Decimal(str(pstdev(values)))


def classify_futures_buildup(price_change: Decimal | None, oi_change: Decimal | None) -> str:
    if price_change is None or oi_change is None or price_change == 0 or oi_change == 0:
        return "NEUTRAL"
    if price_change > 0 and oi_change > 0:
        return "LONG_BUILDUP"
    if price_change < 0 and oi_change > 0:
        return "SHORT_BUILDUP"
    if price_change > 0 and oi_change < 0:
        return "SHORT_COVERING"
    return "LONG_UNWINDING"


def _option_features(snapshots: Sequence[OptionMinuteSnapshot], as_of: datetime, spot: Decimal) -> dict[str, Any]:
    eligible = [item for item in snapshots if item.minute_at <= as_of]
    if not eligible:
        return {"option_data_available": False, "iv_data_status": "IV_DATA_PENDING"}
    minute = max(item.minute_at for item in eligible)
    if minute != as_of:
        return {"option_data_available": False, "iv_data_status": "IV_DATA_PENDING"}
    current = [item for item in eligible if item.minute_at == minute]
    universe = max({item.universe_id for item in current},
                   key=lambda value: sum(item.universe_id == value for item in current))
    current = [item for item in current if item.universe_id == universe]
    calls = [item for item in current if item.option_type == "CE"]
    puts = [item for item in current if item.option_type == "PE"]
    total_ce_oi = sum((item.oi or 0) for item in calls)
    total_pe_oi = sum((item.oi or 0) for item in puts)
    total_ce_delta = sum((item.oi_change or 0) for item in calls)
    total_pe_delta = sum((item.oi_change or 0) for item in puts)
    ce_volume = sum((item.volume_delta or 0) for item in calls)
    pe_volume = sum((item.volume_delta or 0) for item in puts)
    atm_ce = next((item for item in calls if item.atm_offset == 0), None)
    atm_pe = next((item for item in puts if item.atm_offset == 0), None)
    previous_minutes = sorted({_time_value.minute_at for _time_value in eligible if _time_value.minute_at < minute})
    selected_expiry = current[0].expiry
    previous_atm = [item for item in eligible if previous_minutes and item.minute_at == minute-timedelta(minutes=1)
                    and item.atm_offset == 0 and item.expiry == selected_expiry and item.universe_id==universe
                    and atm_ce is not None and item.strike==atm_ce.strike]
    prior_straddle = sum((item.last_price for item in previous_atm), Decimal("0")) if len(previous_atm) == 2 else None
    straddle = atm_ce.last_price + atm_pe.last_price if atm_ce and atm_pe else None
    all_oi = [Decimal(item.oi or 0) for item in current]
    total_oi = sum(all_oi, Decimal("0"))
    entropy = None
    if total_oi > 0:
        probabilities = [float(value / total_oi) for value in all_oi if value > 0]
        entropy = Decimal(str(-sum(value * log(value) for value in probabilities) / log(len(all_oi)))) if len(all_oi) > 1 else Decimal("0")
    calls_above = [item for item in calls if item.strike >= spot]
    puts_below = [item for item in puts if item.strike <= spot]
    max_call_oi = max((item.oi or 0 for item in calls_above), default=0)
    max_put_oi = max((item.oi or 0 for item in puts_below), default=0)
    resistance = min((item for item in calls_above if (item.oi or 0) >= max_call_oi * 0.3), key=lambda item: item.strike, default=None)
    support = max((item for item in puts_below if (item.oi or 0) >= max_put_oi * 0.3), key=lambda item: item.strike, default=None)
    bid_qty = sum(sum(level.quantity for level in item.bids) for item in current)
    ask_qty = sum(sum(level.quantity for level in item.asks) for item in current)
    depth_total = bid_qty + ask_qty
    spreads = [item.spread_percent for item in current if item.spread_percent is not None]
    expected_pairs = {item.atm_offset for item in current}
    complete_pairs = sum(any(item.option_type == "CE" and item.atm_offset == offset for item in current)
                         and any(item.option_type == "PE" and item.atm_offset == offset for item in current)
                         for offset in expected_pairs)
    result = {
        "option_data_available": True, "option_source_minute": minute,
        "total_ce_oi": total_ce_oi, "total_pe_oi": total_pe_oi,
        "pcr_oi": Decimal(total_pe_oi) / Decimal(total_ce_oi) if total_ce_oi else None,
        "total_ce_oi_change": total_ce_delta, "total_pe_oi_change": total_pe_delta,
        "oi_change_imbalance": Decimal(total_pe_delta - total_ce_delta) / Decimal(abs(total_pe_delta) + abs(total_ce_delta)) if total_pe_delta or total_ce_delta else Decimal("0"),
        "ce_volume": ce_volume, "pe_volume": pe_volume,
        "volume_pcr": Decimal(pe_volume) / Decimal(ce_volume) if ce_volume else None,
        "atm_ce_oi": atm_ce.oi if atm_ce else None, "atm_pe_oi": atm_pe.oi if atm_pe else None,
        "atm_ce_oi_change": atm_ce.oi_change if atm_ce else None, "atm_pe_oi_change": atm_pe.oi_change if atm_pe else None,
        "atm_straddle_premium": straddle,
        "atm_straddle_premium_change": straddle - prior_straddle if straddle is not None and prior_straddle is not None else None,
        "mean_bid_ask_spread_pct": sum(spreads, Decimal("0")) / Decimal(len(spreads)) if spreads else None,
        "aggregate_depth_imbalance": Decimal(bid_qty - ask_qty) / Decimal(depth_total) if depth_total else None,
        "call_oi_wall": resistance.strike if resistance else None,
        "put_oi_wall": support.strike if support else None,
        "distance_to_call_oi_wall": (resistance.strike - spot) / spot if resistance else None,
        "distance_to_put_oi_wall": (spot - support.strike) / spot if support else None,
        "oi_concentration_entropy": entropy,
        "selected_universe_completeness": Decimal(complete_pairs) / Decimal(len(expected_pairs)) if expected_pairs else Decimal("0"),
        "iv_data_status": "IV_DATA_PENDING",
    }
    # Existing formulas are retained, but absent observations must never become model zeros.
    if not calls or not puts or any(item.oi is None for item in current):
        for key in ('total_ce_oi','total_pe_oi','pcr_oi','oi_concentration_entropy',
                    'call_oi_wall','put_oi_wall','distance_to_call_oi_wall','distance_to_put_oi_wall'):
            result[key]=None
    if not calls or not puts or any(item.oi_change is None for item in current):
        for key in ('total_ce_oi_change','total_pe_oi_change','oi_change_imbalance'): result[key]=None
    if not calls or not puts or any(item.volume_delta is None for item in current):
        for key in ('ce_volume','pe_volume','volume_pcr'): result[key]=None
    if any(not item.bids or not item.asks for item in current): result['aggregate_depth_imbalance']=None
    if any(item.spread_percent is None for item in current): result['mean_bid_ask_spread_pct']=None
    return result


def _regime(values: dict[str, Any]) -> str:
    if values["minutes_since_market_open"] < 30:
        return "OPENING_VOLATILITY"
    vol5, vol20 = values.get("realized_volatility_5m"), values.get("realized_volatility_20m")
    if vol5 is not None and vol20:
        ratio = vol5 / vol20
        if ratio >= Decimal("1.35"):
            return "VOLATILITY_EXPANSION"
        if ratio <= Decimal("0.70"):
            return "VOLATILITY_COMPRESSION"
    ret5 = values.get("return_5m")
    breakout = values.get("breakout_high_distance_20m")
    if ret5 is not None and breakout is not None and abs(breakout) <= Decimal("0.0005") and abs(ret5) >= Decimal("0.001"):
        return "BREAKOUT_ATTEMPT"
    spread = values.get("ema_fast_slow_spread")
    if spread is not None and spread >= Decimal("0.0005"):
        return "TREND_UP"
    if spread is not None and spread <= Decimal("-0.0005"):
        return "TREND_DOWN"
    return "RANGE" if vol20 is not None else "UNKNOWN"


def _session_bias(values: dict[str, Any]) -> dict[str, Any]:
    score = Decimal("0")
    drivers: list[str] = []
    vwap = values.get("session_vwap_distance")
    if vwap is not None and abs(vwap) >= Decimal("0.0002"):
        score += Decimal("1") if vwap > 0 else Decimal("-1")
        drivers.append("price above session VWAP" if vwap > 0 else "price below session VWAP")
    buildup = values.get("futures_buildup")
    buildup_scores = {"LONG_BUILDUP": 1, "SHORT_COVERING": 1, "SHORT_BUILDUP": -1, "LONG_UNWINDING": -1}
    if buildup in buildup_scores:
        score += Decimal(buildup_scores[buildup]); drivers.append(str(buildup).lower().replace("_", " ") + " in futures")
    oi_imbalance = values.get("oi_change_imbalance")
    if oi_imbalance is not None and abs(oi_imbalance) >= Decimal("0.15"):
        score += Decimal("0.75") if oi_imbalance > 0 else Decimal("-0.75")
        drivers.append("put OI buildup dominates" if oi_imbalance > 0 else "call OI buildup dominates")
    trend = values.get("return_5m")
    if trend is not None and abs(trend) >= Decimal("0.0005"):
        score += Decimal("0.75") if trend > 0 else Decimal("-0.75")
        drivers.append("positive five-minute momentum" if trend > 0 else "negative five-minute momentum")
    contradictory = score != 0 and any("above" in item or "positive" in item or "long buildup" in item or "short covering" in item or "put OI" in item for item in drivers) and any("below" in item or "negative" in item or "short buildup" in item or "long unwinding" in item or "call OI" in item for item in drivers)
    direction = "MIXED" if contradictory and abs(score) < Decimal("1") else "BULLISH" if score >= 1 else "BEARISH" if score <= -1 else "NEUTRAL"
    confidence = min(Decimal("0.90"), Decimal("0.35") + abs(score) * Decimal("0.15")) if drivers else Decimal("0")
    return {"direction": direction, "confidence": float(confidence), "drivers": drivers}


def build_big_move_features(
    *, as_of: datetime, index_candles: Sequence[Candle | FinalizedMinute], session: TradingSession,
    futures_candles: Sequence[Candle | FinalizedMinute] = (), option_snapshots: Sequence[OptionMinuteSnapshot] = (),
) -> BigMoveFeatureSnapshot:
    index = sorted((item for item in index_candles if session.open_at <= _time(item) <= as_of), key=_time)
    if not index or _time(index[-1]) != as_of:
        raise ValueError("a completed BANKNIFTY candle at as_of is required")
    closes = [item.close for item in index]
    current = index[-1]
    index_by_time = {_time(item): item for item in index}
    values: dict[str, Any] = {
        f"return_{window}m": _return_at(index_by_time, as_of, current.close, window)
        for window in RETURN_WINDOWS
    }
    for window in VOLATILITY_WINDOWS:
        values[f"realized_volatility_{window}m"] = _realized_vol_at(index_by_time, as_of, window)
    true_ranges = [max(item.high - item.low, abs(item.high - previous.close), abs(item.low - previous.close))
                   for previous, item in zip(index[-15:-1], index[-14:])]
    values["atr_normalized_14m"] = sum(true_ranges, Decimal("0")) / Decimal(len(true_ranges)) / current.close if true_ranges else None
    ranges = [(item.high - item.low) / item.open for item in index[-20:] if item.open > 0]
    mean_range = sum(ranges, Decimal("0")) / Decimal(len(ranges)) if ranges else None
    current_range = (current.high - current.low) / current.open
    values["range_expansion_ratio"] = current_range / mean_range if mean_range else None
    values["current_range_pct"] = current_range
    ema_fast, ema_slow = _ema(closes, 5), _ema(closes, 13)
    values["ema_slope_5m"] = (ema_fast / _ema(closes[:-1], 5) - 1) if ema_fast is not None and _ema(closes[:-1], 5) else None
    values["ema_fast_slow_spread"] = (ema_fast - ema_slow) / current.close if ema_fast is not None and ema_slow is not None else None
    ema12, ema26 = _ema(closes, 12), _ema(closes, 26)
    values["macd_spread"] = (ema12 - ema26) / current.close if ema12 is not None and ema26 is not None else None
    window20 = index[-20:]
    high20, low20 = max(item.high for item in window20), min(item.low for item in window20)
    values["breakout_high_distance_20m"] = (high20 - current.close) / current.close
    values["breakout_low_distance_20m"] = (current.close - low20) / current.close
    values["candle_body_pct"] = (current.close - current.open) / current.open
    values["upper_wick_pct"] = (current.high - max(current.open, current.close)) / current.open
    values["lower_wick_pct"] = (min(current.open, current.close) - current.low) / current.open
    previous_close = index[-2].close if len(index) > 1 else current.close
    sign = 1 if current.close > previous_close else -1 if current.close < previous_close else 0
    consecutive = 0
    for left, right in reversed(list(zip(index[:-1], index[1:]))):
        step = 1 if right.close > left.close else -1 if right.close < left.close else 0
        if step != sign or step == 0: break
        consecutive += 1
    values["consecutive_candle_direction"] = sign
    values["consecutive_candle_count"] = consecutive
    volumes = [_volume(item) for item in index[-20:] if (_volume(item) or 0) > 0]
    current_volume = _volume(current)
    values["volume_spike_ratio"] = Decimal(current_volume) / (Decimal(sum(volumes)) / Decimal(len(volumes))) if current_volume and volumes else None
    session_bars = [item for item in index if _time(item) >= session.open_at]
    volume_total = sum((_volume(item) or 0) for item in session_bars)
    weighted = sum((item.close * Decimal(_volume(item) or 0) for item in session_bars), Decimal("0"))
    values["session_vwap_distance"] = current.close / (weighted / Decimal(volume_total)) - 1 if volume_total else None
    values["minutes_since_market_open"] = int((as_of - session.open_at).total_seconds() // 60)
    values["minutes_until_market_close"] = int((session.close_at - as_of).total_seconds() // 60)
    opening = [item for item in session_bars if _time(item) < session.open_at + timedelta(minutes=15)]
    if opening:
        opening_high, opening_low = max(item.high for item in opening), min(item.low for item in opening)
        values["opening_range_position"] = (current.close - opening_low) / (opening_high - opening_low) if opening_high != opening_low else Decimal("0.5")
    else:
        values["opening_range_position"] = None

    futures = sorted((item for item in futures_candles if session.open_at <= _time(item) <= as_of), key=_time)
    futures_by_time = {_time(item): item for item in futures}
    latest_future = futures_by_time.get(as_of)
    values["futures_data_available"] = latest_future is not None
    if latest_future:
        values["futures_index_premium"] = latest_future.close - current.close
        values["futures_index_premium_pct"] = latest_future.close / current.close - 1
        for window in (1, 5):
            prior_at = as_of - timedelta(minutes=window)
            prior = futures_by_time.get(prior_at)
            prior_index = index_by_time.get(prior_at)
            values[f"futures_premium_change_{window}m"] = ((latest_future.close - current.close) - (prior.close - prior_index.close)) / current.close if prior and prior_index else None
            values[f"futures_oi_change_{window}m"] = (_oi(latest_future) - _oi(prior)) if prior and _oi(latest_future) is not None and _oi(prior) is not None else None
        values["futures_oi"] = _oi(latest_future)
        values["futures_volume_delta"] = _volume(latest_future)
        prior_future = futures_by_time.get(as_of - timedelta(minutes=1))
        future_return = latest_future.close / prior_future.close - 1 if prior_future is not None else None
        oi_change = Decimal(_oi(latest_future) - _oi(prior_future)) if prior_future is not None and _oi(latest_future) is not None and _oi(prior_future) is not None else None
        values["futures_buildup"] = classify_futures_buildup(future_return, oi_change)
    else:
        values.update({"futures_index_premium": None, "futures_index_premium_pct": None,
            "futures_premium_change_1m": None, "futures_premium_change_5m": None,
            "futures_oi": None, "futures_oi_change_1m": None, "futures_oi_change_5m": None,
            "futures_volume_delta": None, "futures_buildup": "NEUTRAL"})
    values.update(_option_features(option_snapshots, as_of, current.close))
    values["vix_data_status"] = VIX_DATA_STATUS
    market_regime = _regime(values)
    session_bias = _session_bias(values)
    missing_core = sum(values.get(f"return_{window}m") is None for window in (5, 10, 20))
    quality = "PASS" if missing_core == 0 else "PARTIAL_LOOKBACK"
    return BigMoveFeatureSnapshot(as_of, max(_time(item) for item in index), values, market_regime, session_bias, quality)


class BigMoveFeatureGenerationService:
    """Persist immutable index-first V2 rows; derivative fields remain explicitly unavailable."""

    def __init__(self, repository: object) -> None:
        self.repository = repository

    def generate(self, revision_id: str, instrument_id: int) -> tuple[list[object], int]:
        from .calendar import IST
        from .research_models import FeatureRow
        revision = self.repository.get_revision(revision_id)
        if revision.status != "ACCEPTED":
            raise ValueError("V2 features require an ACCEPTED dataset revision")
        instrument = self.repository.get_instrument(instrument_id)
        if not (instrument.exchange == "NSE" and instrument.instrument_type == "EQ" and "BANK" in instrument.name.upper()):
            raise ValueError("V2 features require the BANKNIFTY index")
        register_big_move_feature_set(self.repository, revision.created_at)
        sessions = {item.session_date: item for item in self.repository.sessions_between(
            revision.session_start, revision.session_end) if item.status in {"OPEN", "SPECIAL"}}
        candles = self.repository.revision_candles(revision_id, instrument_id)
        rows: list[FeatureRow] = []
        for candle in sorted(candles, key=lambda item: item.timestamp):
            session = sessions.get(candle.timestamp.astimezone(IST).date())
            if session is None:
                continue
            snapshot = build_big_move_features(as_of=candle.timestamp,
                index_candles=candles, session=session)
            rows.append(FeatureRow(revision_id, instrument_id, candle.timestamp,
                FEATURE_SET_VERSION_V2, snapshot.source_max_timestamp,
                {**snapshot.values, "market_regime": snapshot.market_regime,
                 "session_bias": snapshot.session_bias}, snapshot.quality_status))
        return rows, self.repository.save_feature_rows(rows)
