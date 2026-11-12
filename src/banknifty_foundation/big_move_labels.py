from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from math import sqrt
from statistics import pstdev
from typing import Sequence

from .calendar import IST
from .models import Candle, TradingSession


BIG_MOVE_HORIZONS = (10, 20, 30)
BIG_MOVE_CLASSES = ("BIG_UP", "BIG_DOWN", "NO_BIG_MOVE", "AMBIGUOUS", "UNKNOWN")


@dataclass(frozen=True)
class BigMoveThresholdPolicy:
    version: str
    minimum_returns: dict[int, Decimal]
    volatility_multipliers: dict[int, Decimal]
    volatility_lookback_minutes: int = 20

    def __post_init__(self) -> None:
        if tuple(sorted(self.minimum_returns)) != BIG_MOVE_HORIZONS:
            raise ValueError("minimum returns must cover 10, 20 and 30 minutes")
        if tuple(sorted(self.volatility_multipliers)) != BIG_MOVE_HORIZONS:
            raise ValueError("volatility multipliers must cover 10, 20 and 30 minutes")
        if any(value <= 0 for value in self.minimum_returns.values()):
            raise ValueError("minimum returns must be positive")
        if any(value < 0 for value in self.volatility_multipliers.values()):
            raise ValueError("volatility multipliers cannot be negative")
        if self.volatility_lookback_minutes < 2:
            raise ValueError("volatility lookback must be at least two minutes")

    def threshold(self, horizon: int, known_closes: Sequence[Decimal]) -> Decimal:
        """Return a threshold using only closes known at the label as-of time."""
        if horizon not in BIG_MOVE_HORIZONS:
            raise ValueError("unsupported big-move horizon")
        required = self.volatility_lookback_minutes + 1
        trailing = list(known_closes[-required:])
        volatility = Decimal("0")
        if len(trailing) == required and all(value > 0 for value in trailing):
            returns = [float(trailing[i] / trailing[i - 1] - Decimal("1")) for i in range(1, len(trailing))]
            volatility = Decimal(str(pstdev(returns)))
        adjusted = volatility * self.volatility_multipliers[horizon] * Decimal(str(sqrt(horizon)))
        return max(self.minimum_returns[horizon], adjusted)


@dataclass(frozen=True)
class BigMoveLabel:
    dataset_revision_id: str
    instrument_id: int
    as_of: datetime
    horizon_minutes: int
    expiry_at: datetime
    reference_price: Decimal
    threshold_policy_version: str
    threshold_value: Decimal
    max_future_high: Decimal | None
    min_future_low: Decimal | None
    maximum_up_excursion: Decimal | None
    maximum_down_excursion: Decimal | None
    maximum_favorable_excursion: Decimal | None
    maximum_adverse_excursion: Decimal | None
    close_return_at_horizon: Decimal | None
    realized_range: Decimal | None
    first_barrier: str | None
    first_barrier_at: datetime | None
    label: str
    quality_status: str

    def to_dict(self) -> dict:
        result = asdict(self)
        for key, value in tuple(result.items()):
            if isinstance(value, datetime):
                result[key] = value.isoformat()
            elif isinstance(value, Decimal):
                result[key] = str(value)
        return result


def calculate_big_move_label(
    *, dataset_revision_id: str, instrument_id: int, as_of_candle: Candle,
    horizon: int, session: TradingSession, candles_known_at_as_of: Sequence[Candle],
    future_candles: Sequence[Candle], policy: BigMoveThresholdPolicy,
) -> BigMoveLabel | None:
    """Calculate a path label; threshold inputs are frozen before future bars are read."""
    if horizon not in BIG_MOVE_HORIZONS:
        raise ValueError("unsupported big-move horizon")
    expiry = as_of_candle.timestamp + timedelta(minutes=horizon)
    if as_of_candle.timestamp < session.open_at or expiry >= session.close_at:
        return None
    if any(item.timestamp > as_of_candle.timestamp for item in candles_known_at_as_of):
        raise ValueError("threshold history contains future data")
    threshold = policy.threshold(horizon, [item.close for item in candles_known_at_as_of])
    lookup = {item.timestamp: item for item in future_candles}
    path = [lookup.get(as_of_candle.timestamp + timedelta(minutes=offset)) for offset in range(1, horizon + 1)]
    if any(item is None for item in path):
        return BigMoveLabel(dataset_revision_id, instrument_id, as_of_candle.timestamp, horizon,
            expiry, as_of_candle.close, policy.version, threshold, None, None, None, None,
            None, None, None, None, None, None, "UNKNOWN", "INCOMPLETE_FUTURE_PATH")
    completed = [item for item in path if item is not None]
    if any(min(item.open, item.high, item.low, item.close) <= 0 for item in completed):
        return BigMoveLabel(dataset_revision_id, instrument_id, as_of_candle.timestamp, horizon,
            expiry, as_of_candle.close, policy.version, threshold, None, None, None, None,
            None, None, None, None, None, None, "UNKNOWN", "INVALID_FUTURE_PATH")

    reference = as_of_candle.close
    max_high = max(item.high for item in completed)
    min_low = min(item.low for item in completed)
    up_excursion = max(Decimal("0"), max_high / reference - Decimal("1"))
    down_excursion = max(Decimal("0"), Decimal("1") - min_low / reference)
    first_barrier: str | None = None
    first_at: datetime | None = None
    label = "NO_BIG_MOVE"
    for candle in completed:
        up_hit = candle.high / reference - Decimal("1") >= threshold
        down_hit = Decimal("1") - candle.low / reference >= threshold
        if up_hit and down_hit:
            label, first_barrier, first_at = "AMBIGUOUS", "BOTH_SAME_MINUTE", candle.timestamp
            break
        if up_hit:
            label, first_barrier, first_at = "BIG_UP", "UP", candle.timestamp
            break
        if down_hit:
            label, first_barrier, first_at = "BIG_DOWN", "DOWN", candle.timestamp
            break
    favorable = up_excursion if label == "BIG_UP" else down_excursion if label == "BIG_DOWN" else max(up_excursion, down_excursion)
    adverse = down_excursion if label == "BIG_UP" else up_excursion if label == "BIG_DOWN" else min(up_excursion, down_excursion)
    return BigMoveLabel(
        dataset_revision_id, instrument_id, as_of_candle.timestamp, horizon, expiry, reference,
        policy.version, threshold, max_high, min_low, up_excursion, down_excursion,
        favorable, adverse, completed[-1].close / reference - Decimal("1"),
        (max_high - min_low) / reference, first_barrier, first_at, label, "VALID",
    )


def default_big_move_policy() -> BigMoveThresholdPolicy:
    return BigMoveThresholdPolicy(
        version="big-move-threshold-v1",
        minimum_returns={10: Decimal("0.0015"), 20: Decimal("0.0020"), 30: Decimal("0.0025")},
        volatility_multipliers={10: Decimal("1.25"), 20: Decimal("1.20"), 30: Decimal("1.15")},
    )


class BigMoveLabelGenerationService:
    def __init__(self, repository: object, policy: BigMoveThresholdPolicy | None = None) -> None:
        self.repository = repository
        self.policy = policy or default_big_move_policy()

    def generate(self, revision_id: str, instrument_id: int) -> tuple[list[BigMoveLabel], int]:
        revision = self.repository.get_revision(revision_id)
        if revision.status != "ACCEPTED":
            raise ValueError("big-move labels require an ACCEPTED dataset revision")
        if instrument_id not in revision.instrument_ids:
            raise ValueError("instrument is not part of the accepted revision")
        instrument = self.repository.get_instrument(instrument_id)
        if not (instrument.exchange == "NSE" and instrument.instrument_type == "EQ" and "BANK" in instrument.name.upper()):
            raise ValueError("big-move labels require the BANKNIFTY index")
        sessions = {item.session_date: item for item in self.repository.sessions_between(
            revision.session_start, revision.session_end) if item.status in {"OPEN", "SPECIAL"}}
        candles = sorted(self.repository.revision_candles(revision_id, instrument_id), key=lambda item: item.timestamp)
        labels: list[BigMoveLabel] = []
        for candle in candles:
            session = sessions.get(candle.timestamp.astimezone(IST).date())
            if session is None:
                continue
            session_candles = [item for item in candles if session.open_at <= item.timestamp < session.close_at]
            known = [item for item in session_candles if item.timestamp <= candle.timestamp]
            future = [item for item in session_candles if candle.timestamp < item.timestamp <= candle.timestamp + timedelta(minutes=30)]
            for horizon in BIG_MOVE_HORIZONS:
                label = calculate_big_move_label(dataset_revision_id=revision_id,
                    instrument_id=instrument_id, as_of_candle=candle, horizon=horizon,
                    session=session, candles_known_at_as_of=known, future_candles=future,
                    policy=self.policy)
                if label is not None:
                    labels.append(label)
        self.repository.save_big_move_policy(self.policy)
        inserted = self.repository.save_big_move_labels(labels)
        return labels, inserted
