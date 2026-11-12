from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any


LIVE_STATES = frozenset({
    "DISCONNECTED", "CONNECTING", "CONNECTED", "DEGRADED",
    "RECONNECTING", "AUTH_EXPIRED", "AUTH_NOT_CONFIGURED", "STOPPED",
})


@dataclass(frozen=True)
class LiveInstrument:
    instrument_id: int
    instrument_token: int
    snapshot_version: str
    tradingsymbol: str
    exchange: str
    instrument_type: str
    expiry: date | None = None
    strike: Decimal | None = None


@dataclass(frozen=True)
class DepthLevel:
    price: Decimal
    quantity: int
    orders: int | None = None


@dataclass(frozen=True)
class LiveTick:
    tick_id: str
    instrument_id: int
    instrument_token: int
    tradingsymbol: str
    exchange: str
    instrument_type: str
    event_at: datetime
    received_at: datetime
    last_price: Decimal
    last_quantity: int | None
    average_price: Decimal | None
    cumulative_volume: int | None
    total_buy_quantity: int | None
    total_sell_quantity: int | None
    oi: int | None
    oi_day_high: int | None
    oi_day_low: int | None
    ohlc: dict[str, Decimal | None]
    bids: tuple[DepthLevel, ...]
    asks: tuple[DepthLevel, ...]
    connection_id: str
    subscription_generation: int
    quality_flags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        for key in ("event_at", "received_at"):
            value[key] = getattr(self, key).isoformat()
        for key in ("last_price", "average_price"):
            item = getattr(self, key)
            value[key] = str(item) if item is not None else None
        value["ohlc"] = {
            key: str(item) if item is not None else None for key, item in self.ohlc.items()
        }
        for side in ("bids", "asks"):
            value[side] = [
                {"price": str(item.price), "quantity": item.quantity, "orders": item.orders}
                for item in getattr(self, side)
            ]
        return value


@dataclass(frozen=True)
class LiveConnection:
    connection_id: str
    state: str
    event_at: datetime
    connected_at: datetime | None
    disconnected_at: datetime | None
    reason: str | None
    reconnect_attempt: int
    last_tick_at: datetime | None


@dataclass(frozen=True)
class LiveGap:
    gap_id: str
    source: str
    connection_id: str
    started_at: datetime
    detected_at: datetime
    resolved_at: datetime | None
    cause: str
    affected_instruments: tuple[int, ...]
    affected_minutes: tuple[datetime, ...]
    reconnect_attempts: int
    state: str


@dataclass(frozen=True)
class SubscriptionGeneration:
    generation: int
    created_at: datetime
    desired_tokens: tuple[int, ...]
    active_tokens: tuple[int, ...]
    added_tokens: tuple[int, ...]
    removed_tokens: tuple[int, ...]
    mode: str = "full"


@dataclass(frozen=True)
class OptionUniverse:
    universe_id: str
    selected_at: datetime
    snapshot_version: str
    expiry: date
    eligible_expiries: tuple[date, ...]
    atm_strike: Decimal
    underlying_value: Decimal
    strike_width: int
    instrument_ids: tuple[int, ...]
    tokens: tuple[int, ...]
    missing_pairs: tuple[str, ...]
    rejection_reasons: tuple[str, ...]
    quality_state: str


@dataclass(frozen=True)
class FuturesSelection:
    selection_id: str
    selected_at: datetime
    snapshot_version: str
    instrument_id: int
    instrument_token: int
    expiry: date
    eligible_expiries: tuple[date, ...]
    rule_version: str
    rollover: bool
    reason: str


@dataclass(frozen=True)
class FinalizedMinute:
    minute_id: str
    instrument_id: int
    minute_at: datetime
    revision: int
    first_finalized_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    cumulative_volume: int | None
    volume_delta: int | None
    oi_close: int | None
    oi_change: int | None
    first_tick_at: datetime | None
    last_tick_at: datetime | None
    tick_count: int | None
    connection_ids: tuple[str, ...]
    quality_flags: tuple[str, ...]
    quality_state: str
    checksum: str


@dataclass(frozen=True)
class OptionMinuteSnapshot:
    snapshot_id: str
    instrument_id: int
    minute_at: datetime
    universe_id: str
    expiry: date
    strike: Decimal
    option_type: str
    atm_offset: int
    last_price: Decimal
    cumulative_volume: int | None
    volume_delta: int | None
    oi: int | None
    oi_change: int | None
    best_bid_price: Decimal | None
    best_bid_quantity: int | None
    best_ask_price: Decimal | None
    best_ask_quantity: int | None
    spread: Decimal | None
    spread_percent: Decimal | None
    bids: tuple[DepthLevel, ...]
    asks: tuple[DepthLevel, ...]
    total_buy_quantity: int | None
    total_sell_quantity: int | None
    receive_age_seconds: Decimal
    quality_flags: tuple[str, ...]
    quality_state: str
    first_seen_at: datetime | None = None


@dataclass(frozen=True)
class LiveQualityIssue:
    issue_id: str
    code: str
    detected_at: datetime
    instrument_id: int | None
    minute_at: datetime | None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LiveMinuteCorrection:
    correction_id: str
    instrument_id: int
    minute_at: datetime
    tick_id: str
    received_at: datetime
    disposition: str = "RESEARCH_AMENDMENT_ONLY"
