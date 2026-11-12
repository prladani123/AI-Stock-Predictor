from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from .calendar import IST
from .models import Candle
from .repository import Repository, digest
from .research_models import FeatureRow, FeatureSetDefinition

FEATURE_SET_VERSION = "banknifty-index-core-v1"
RETURN_LAGS = (1, 2, 3, 5, 10, 15, 20)
ROLLING_WINDOW = 5
FEATURE_SCHEMA: tuple[dict[str, Any], ...] = (
    *tuple(
        {
            "name": f"return_{lag}m",
            "source_columns": ["close"],
            "lookback_minutes": lag,
            "availability_rule": "exact completed candle at as_of and as_of-lag",
            "null_policy": "null when exact lookback is missing",
        }
        for lag in RETURN_LAGS
    ),
    *tuple(
        {
            "name": name,
            "source_columns": ["open", "high", "low", "close"],
            "lookback_minutes": 0,
            "availability_rule": "completed candle at as_of",
            "null_policy": "invalid source candle rejects dataset",
        }
        for name in (
            "candle_body_pct",
            "candle_range_pct",
            "upper_wick_pct",
            "lower_wick_pct",
        )
    ),
    {
        "name": "rolling_mean_return_5m",
        "source_columns": ["close"],
        "lookback_minutes": 5,
        "availability_rule": "six exact consecutive completed closes ending at as_of",
        "null_policy": "null when any minute is missing",
    },
    {
        "name": "rolling_std_return_5m",
        "source_columns": ["close"],
        "lookback_minutes": 5,
        "availability_rule": "six exact consecutive completed closes ending at as_of",
        "null_policy": "null when any minute is missing",
    },
    {
        "name": "rolling_high_low_position_5m",
        "source_columns": ["high", "low", "close"],
        "lookback_minutes": 5,
        "availability_rule": "five exact consecutive completed candles ending at as_of",
        "null_policy": "null when any minute is missing",
    },
    *tuple(
        {
            "name": name,
            "source_columns": ["close"],
            "lookback_minutes": 20,
            "availability_rule": "consecutive completed candles ending at as_of, capped at 20",
            "null_policy": "count stops at first missing minute",
        }
        for name in ("consecutive_positive_candles", "consecutive_negative_candles")
    ),
    *tuple(
        {
            "name": name,
            "source_columns": ["candle_at", "trading_session"],
            "lookback_minutes": 0,
            "availability_rule": "explicit stored session containing as_of",
            "null_policy": "row unavailable when session is unknown",
        }
        for name in ("time_of_day_bucket", "minutes_since_market_open", "minutes_until_market_close")
    ),
)
FEATURE_NAMES = tuple(item["name"] for item in FEATURE_SCHEMA)


def feature_definition() -> dict[str, Any]:
    return {
        "instrument_scope": "BANKNIFTY_INDEX_ONLY",
        "completed_candles_only": True,
        "return_lags_minutes": list(RETURN_LAGS),
        "rolling_return_window_minutes": ROLLING_WINDOW,
        "rolling_high_low_window_minutes": ROLLING_WINDOW,
        "features": list(FEATURE_SCHEMA),
        "feature_order": list(FEATURE_NAMES),
        "availability_time": "after the one-minute candle at as_of is completed",
        "null_policy": "UNAVAILABLE_NO_FILL",
        "centred_windows": False,
    }


def _return(current: Decimal, previous: Decimal) -> Decimal:
    return current / previous - Decimal("1")


def _feature_values(
    candle: Candle,
    lookup: dict,
    session,
) -> tuple[dict[str, Any], str]:
    values: dict[str, Any] = {}
    missing = False
    for lag in RETURN_LAGS:
        previous = lookup.get(candle.timestamp - timedelta(minutes=lag))
        name = f"return_{lag}m"
        if previous is None:
            values[name] = None
            missing = True
        else:
            values[name] = _return(candle.close, previous.close)

    values["candle_body_pct"] = (candle.close - candle.open) / candle.open
    values["candle_range_pct"] = (candle.high - candle.low) / candle.open
    values["upper_wick_pct"] = (
        candle.high - max(candle.open, candle.close)
    ) / candle.open
    values["lower_wick_pct"] = (
        min(candle.open, candle.close) - candle.low
    ) / candle.open

    returns: list[Decimal] = []
    rolling_candles: list[Candle] = []
    for offset in range(ROLLING_WINDOW + 1):
        item = lookup.get(candle.timestamp - timedelta(minutes=offset))
        if item is None:
            missing = True
            break
        rolling_candles.append(item)
    if len(rolling_candles) == ROLLING_WINDOW + 1:
        chronological = list(reversed(rolling_candles))
        returns = [
            _return(chronological[index].close, chronological[index - 1].close)
            for index in range(1, len(chronological))
        ]
        mean = sum(returns, Decimal("0")) / Decimal(len(returns))
        variance = sum((item - mean) ** 2 for item in returns) / Decimal(len(returns))
        values["rolling_mean_return_5m"] = mean
        values["rolling_std_return_5m"] = variance.sqrt()
        window = chronological[1:]
        high = max(item.high for item in window)
        low = min(item.low for item in window)
        values["rolling_high_low_position_5m"] = (
            (candle.close - low) / (high - low) if high != low else Decimal("0.5")
        )
    else:
        values["rolling_mean_return_5m"] = None
        values["rolling_std_return_5m"] = None
        values["rolling_high_low_position_5m"] = None

    positive = 0
    negative = 0
    cursor = candle.timestamp
    for _ in range(20):
        current = lookup.get(cursor)
        previous = lookup.get(cursor - timedelta(minutes=1))
        if current is None or previous is None:
            break
        movement = current.close - previous.close
        if movement > 0 and negative == 0:
            positive += 1
        elif movement < 0 and positive == 0:
            negative += 1
        else:
            break
        cursor -= timedelta(minutes=1)
    values["consecutive_positive_candles"] = positive
    values["consecutive_negative_candles"] = negative
    minutes_open = int((candle.timestamp - session.open_at).total_seconds() // 60)
    values["minutes_since_market_open"] = minutes_open
    values["minutes_until_market_close"] = int(
        (session.close_at - candle.timestamp).total_seconds() // 60
    )
    local = candle.timestamp.astimezone(IST)
    values["time_of_day_bucket"] = local.hour * 2 + local.minute // 30
    return values, "PARTIAL_LOOKBACK" if missing else "AVAILABLE"


class FeatureGenerationService:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def generate(
        self,
        revision_id: str,
        instrument_id: int,
        version: str = FEATURE_SET_VERSION,
    ) -> tuple[list[FeatureRow], int]:
        revision = self.repository.get_revision(revision_id)
        if revision.status != "ACCEPTED":
            raise ValueError("features require an ACCEPTED dataset revision")
        instrument = self.repository.get_instrument(instrument_id)
        if not (
            instrument.exchange == "NSE"
            and instrument.instrument_type == "EQ"
            and "BANK" in instrument.name.upper()
        ):
            raise ValueError("Phase 4 features require the BANKNIFTY index")
        definition = feature_definition()
        feature_set = FeatureSetDefinition(version, revision.created_at, definition, digest(definition))
        self.repository.save_feature_set(feature_set)

        sessions = {
            item.session_date: item
            for item in self.repository.sessions_between(revision.session_start, revision.session_end)
            if item.status in {"OPEN", "SPECIAL"}
        }
        candles = self.repository.revision_candles(revision_id, instrument_id)
        by_session: dict = {}
        for candle in candles:
            by_session.setdefault(candle.timestamp.astimezone(IST).date(), []).append(candle)
        rows: list[FeatureRow] = []
        for session_date, session_candles in sorted(by_session.items()):
            session = sessions.get(session_date)
            if session is None:
                continue
            lookup = {item.timestamp: item for item in session_candles}
            for candle in sorted(session_candles, key=lambda item: item.timestamp):
                values, quality = _feature_values(candle, lookup, session)
                row = FeatureRow(
                    revision_id,
                    instrument_id,
                    candle.timestamp,
                    version,
                    candle.timestamp,
                    values,
                    quality,
                )
                if row.source_max_timestamp > row.as_of:
                    raise AssertionError("feature source watermark exceeded as_of")
                rows.append(row)
        inserted = self.repository.save_feature_rows(rows)
        return rows, inserted
