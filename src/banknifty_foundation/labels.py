from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from .calendar import IST
from .models import Candle, TargetLabel, TradingSession
from .repository import Repository

HORIZONS = (5, 10, 15, 20)


@dataclass(frozen=True)
class LabelInput:
    instrument_id: int
    as_of: datetime
    horizon_minutes: int
    reference_price: Decimal
    expiry_at: datetime
    threshold_policy_version: str
    threshold_value: Decimal


def build_label_input(
    instrument_id: int,
    candle: Candle,
    horizon: int,
    session: TradingSession,
    policy_version: str,
    threshold: Decimal,
) -> LabelInput | None:
    """Build the time-t record without inspecting any future candle value."""
    expiry = candle.timestamp + timedelta(minutes=horizon)
    if expiry >= session.close_at or candle.timestamp < session.open_at:
        return None
    return LabelInput(
        instrument_id,
        candle.timestamp,
        horizon,
        candle.close,
        expiry,
        policy_version,
        threshold,
    )


class LabelGenerationService:
    def __init__(
        self,
        repository: Repository,
        policy_version: str,
        thresholds: dict[int, Decimal],
    ) -> None:
        if tuple(sorted(thresholds)) != HORIZONS:
            raise ValueError("thresholds must be configured for 5, 10, 15 and 20 minutes")
        if any(value <= 0 for value in thresholds.values()):
            raise ValueError("label thresholds must be positive")
        self.repository = repository
        self.policy_version = policy_version
        self.thresholds = thresholds

    def generate(self, revision_id: str, instrument_id: int) -> tuple[list[TargetLabel], int]:
        revision = self.repository.get_revision(revision_id)
        if revision.status != "ACCEPTED":
            raise ValueError("final labels may be generated only from an ACCEPTED dataset revision")
        if instrument_id not in revision.instrument_ids:
            raise ValueError("instrument is not part of the accepted revision")
        instrument = self.repository.get_instrument(instrument_id)
        if not (
            instrument.exchange == "NSE"
            and instrument.instrument_type == "EQ"
            and "BANK" in instrument.name.upper()
        ):
            raise ValueError("target labels require the BANKNIFTY index instrument")

        sessions = {
            session.session_date: session
            for session in self.repository.sessions_between(revision.session_start, revision.session_end)
            if session.status in {"OPEN", "SPECIAL"}
        }
        candles = self.repository.revision_candles(revision_id, instrument_id)
        lookup = {item.timestamp: item for item in candles}
        labels: list[TargetLabel] = []
        for candle in candles:
            session = sessions.get(candle.timestamp.astimezone(IST).date())
            if session is None:
                continue
            for horizon in HORIZONS:
                input_record = build_label_input(
                    instrument_id,
                    candle,
                    horizon,
                    session,
                    self.policy_version,
                    self.thresholds[horizon],
                )
                if input_record is None:
                    continue
                future = lookup.get(input_record.expiry_at)
                if future is None or min(future.open, future.high, future.low, future.close) <= 0:
                    labels.append(
                        TargetLabel(
                            revision_id,
                            instrument_id,
                            input_record.as_of,
                            horizon,
                            input_record.reference_price,
                            input_record.expiry_at,
                            None,
                            self.policy_version,
                            input_record.threshold_value,
                            None,
                            "UNKNOWN",
                            "MISSING_OR_INVALID_EXPIRY",
                        )
                    )
                    continue
                realised = future.close / input_record.reference_price - Decimal("1")
                if realised >= input_record.threshold_value:
                    value = "UP"
                elif realised <= -input_record.threshold_value:
                    value = "DOWN"
                else:
                    value = "NO_TRADE"
                labels.append(
                    TargetLabel(
                        revision_id,
                        instrument_id,
                        input_record.as_of,
                        horizon,
                        input_record.reference_price,
                        input_record.expiry_at,
                        future.close,
                        self.policy_version,
                        input_record.threshold_value,
                        realised,
                        value,
                        "VALID",
                    )
                )
        self.repository.save_threshold_policy(self.policy_version, self.thresholds)
        inserted = self.repository.save_labels(labels)
        return labels, inserted
