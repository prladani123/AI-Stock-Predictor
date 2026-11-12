from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
from typing import Any, Sequence
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .live_adapters import LiveMarketAdapter
from .live_models import (
    DepthLevel, FinalizedMinute, FuturesSelection, LiveConnection, LiveGap,
    LiveInstrument, LiveMinuteCorrection, LiveQualityIssue, LiveTick, OptionMinuteSnapshot,
    OptionUniverse, SubscriptionGeneration,
)
from .live_repository import LiveRepository
from .models import TradingSession

try:
    IST = ZoneInfo("Asia/Kolkata")
except ZoneInfoNotFoundError:
    IST = timezone(timedelta(hours=5, minutes=30), "Asia/Kolkata")
UTC = timezone.utc


def _id(prefix: str, *parts: Any) -> str:
    return f"{prefix}-{uuid5(NAMESPACE_URL, '|'.join(str(item) for item in parts)).hex}"


def _decimal(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _aware(value: Any) -> tuple[datetime, bool]:
    if not isinstance(value, datetime):
        raise ValueError("broker tick timestamp is required")
    naive = value.tzinfo is None
    if naive:
        value = value.replace(tzinfo=IST)
    return value.astimezone(UTC), naive


class InstrumentTokenMap:
    def __init__(self, instruments: Sequence[LiveInstrument], snapshot_version: str) -> None:
        eligible = [item for item in instruments if item.snapshot_version == snapshot_version]
        self.snapshot_version = snapshot_version
        self.by_token = {item.instrument_token: item for item in eligible}
        if len(self.by_token) != len(eligible):
            raise ValueError("instrument snapshot has duplicate tokens")

    def resolve(self, token: int) -> LiveInstrument:
        try:
            return self.by_token[token]
        except KeyError as exc:
            raise ValueError(f"unknown instrument token: {token}") from exc


class ContractSelector:
    RULE_VERSION = "banknifty-live-universe-v1"

    def __init__(self, instruments: Sequence[LiveInstrument]) -> None:
        self.instruments = tuple(instruments)

    def select_future(self, at: datetime, *, include_next: bool = False) -> tuple[FuturesSelection, tuple[LiveInstrument, ...]]:
        session_date = at.astimezone(IST).date()
        futures = sorted(
            (item for item in self.instruments if item.instrument_type == "FUT" and item.expiry and item.expiry >= session_date),
            key=lambda item: (item.expiry, item.instrument_id),
        )
        if not futures:
            raise ValueError("no unexpired BANKNIFTY future in instrument snapshot")
        selected = futures[0]
        selection = FuturesSelection(
            _id("future", selected.snapshot_version, at.isoformat(), selected.instrument_id),
            at, selected.snapshot_version, selected.instrument_id, selected.instrument_token,
            selected.expiry, tuple(sorted({item.expiry for item in futures})), self.RULE_VERSION,
            (selected.expiry - session_date).days <= 1, "nearest unexpired persisted contract",
        )
        return selection, tuple(futures[:2] if include_next else futures[:1])

    def select_options(self, underlying: Decimal, at: datetime, *, width: int = 5) -> OptionUniverse:
        session_date = at.astimezone(IST).date()
        options = [
            item for item in self.instruments
            if item.instrument_type in {"CE", "PE"} and item.expiry and item.expiry >= session_date and item.strike is not None
        ]
        expiries = tuple(sorted({item.expiry for item in options}))
        if not expiries:
            raise ValueError("no eligible BANKNIFTY option expiry in instrument snapshot")
        expiry = expiries[0]
        current = [item for item in options if item.expiry == expiry]
        strikes = sorted({item.strike for item in current if item.strike is not None})
        if not strikes:
            raise ValueError("selected expiry has no valid strikes")
        atm = min(strikes, key=lambda strike: (abs(strike - underlying), strike))
        center = strikes.index(atm)
        selected_strikes = strikes[max(0, center - width): center + width + 1]
        by_pair = {(item.strike, item.instrument_type): item for item in current}
        selected: list[LiveInstrument] = []
        missing: list[str] = []
        for strike in selected_strikes:
            pair = [by_pair.get((strike, kind)) for kind in ("CE", "PE")]
            if any(item is None for item in pair):
                missing.append(str(strike))
                continue
            selected.extend(item for item in pair if item is not None)
        quality = "PASS" if len(selected_strikes) == 2 * width + 1 and not missing else "INCOMPLETE_UNIVERSE"
        rejection_reasons = []
        if len(selected_strikes) != 2 * width + 1:
            rejection_reasons.append("INSUFFICIENT_AVAILABLE_STRIKES")
        if missing:
            rejection_reasons.append("MISSING_CE_PE_PAIR")
        universe_id = _id("universe", current[0].snapshot_version, at.isoformat(), expiry, atm, width)
        return OptionUniverse(
            universe_id, at, current[0].snapshot_version, expiry, expiries, atm,
            underlying, width, tuple(item.instrument_id for item in selected),
            tuple(item.instrument_token for item in selected), tuple(missing),
            tuple(rejection_reasons), quality,
        )


class SubscriptionRegistry:
    def __init__(self, adapter: LiveMarketAdapter, repository: LiveRepository) -> None:
        self.adapter = adapter
        self.repository = repository
        self.desired: set[int] = set()
        self.active: set[int] = set()
        self.generation = 0
        self.pending_retire: set[int] = set()
        self.awaiting_fresh: set[int] = set()

    def replace(self, tokens: Sequence[int], at: datetime) -> SubscriptionGeneration:
        target = set(int(item) for item in tokens)
        if target == self.desired:
            return SubscriptionGeneration(
                self.generation, at, tuple(sorted(self.desired)), tuple(sorted(self.active)), (), ()
            )
        added = target - self.desired
        removed = self.desired - target
        if added:
            self.adapter.subscribe(sorted(added))
            self.adapter.set_mode("full", sorted(added))
        self.pending_retire.update(removed)
        self.pending_retire.difference_update(target)
        self.awaiting_fresh = (self.awaiting_fresh & target) | added
        self.desired = target
        self.generation += 1
        record = SubscriptionGeneration(
            self.generation, at, tuple(sorted(self.desired)), tuple(sorted(self.active)),
            tuple(sorted(added)), tuple(sorted(removed)),
        )
        self.repository.save_subscription_generation(record)
        return record

    def restore_after_reconnect(self, at: datetime) -> SubscriptionGeneration:
        tokens = sorted(self.desired)
        self.active.clear()
        if tokens:
            self.adapter.subscribe(tokens)
            self.adapter.set_mode("full", tokens)
        self.awaiting_fresh = set(tokens)
        self.generation += 1
        record = SubscriptionGeneration(self.generation, at, tuple(tokens), (), tuple(tokens), ())
        self.repository.save_subscription_generation(record)
        return record

    def confirm_fresh(self, token: int, at: datetime) -> tuple[int, ...]:
        was_awaited = token in self.awaiting_fresh
        if token in self.desired:
            self.active.add(token)
        self.awaiting_fresh.discard(token)
        if self.awaiting_fresh:
            return ()
        if not was_awaited and not self.pending_retire:
            return ()
        retired = tuple(sorted(self.pending_retire))
        if retired:
            self.adapter.unsubscribe(retired)
            self.active.difference_update(retired)
            self.pending_retire.clear()
        self.generation += 1
        self.repository.save_subscription_generation(SubscriptionGeneration(
            self.generation, at, tuple(sorted(self.desired)), tuple(sorted(self.active)),
            (), retired,
        ))
        return retired


class ConnectionState:
    def __init__(self, repository: LiveRepository, connection_id: str) -> None:
        self.repository = repository
        self.connection_id = connection_id
        self.state = "DISCONNECTED"
        self.connected_at: datetime | None = None
        self.last_tick_at: datetime | None = None
        self.reconnect_attempt = 0
        self.open_gap: LiveGap | None = None

    def transition(self, state: str, at: datetime, reason: str | None = None) -> None:
        if state not in {"DISCONNECTED", "CONNECTING", "CONNECTED", "DEGRADED", "RECONNECTING", "AUTH_EXPIRED", "AUTH_NOT_CONFIGURED", "STOPPED"}:
            raise ValueError(f"unknown live state: {state}")
        previous = self.state
        self.state = state
        if state == "CONNECTED":
            self.connected_at = self.connected_at or at
        if state == "RECONNECTING":
            self.reconnect_attempt += 1
        if state in {"DISCONNECTED", "DEGRADED", "RECONNECTING"} and previous in {"CONNECTED", "DEGRADED"} and self.open_gap is None:
            self.open_gap = LiveGap(
                _id("gap", self.connection_id, at.isoformat()), "ZERODHA_TICKER", self.connection_id,
                self.last_tick_at or at, at, None, reason or state, (), (), self.reconnect_attempt, "OPEN",
            )
            self.repository.save_gap(self.open_gap)
        self.repository.save_connection(LiveConnection(
            self.connection_id, state, at, self.connected_at,
            at if state in {"DISCONNECTED", "AUTH_EXPIRED", "STOPPED"} else None,
            reason, self.reconnect_attempt, self.last_tick_at,
        ))

    def record_tick(self, at: datetime) -> None:
        self.last_tick_at = at

    def resolve_gap(self, at: datetime, affected: Sequence[int]) -> LiveGap | None:
        if self.open_gap is None:
            return None
        minute = self.open_gap.started_at.replace(second=0, microsecond=0)
        affected_minutes: list[datetime] = []
        while minute <= at.replace(second=0, microsecond=0):
            affected_minutes.append(minute)
            minute += timedelta(minutes=1)
        resolved = replace(
            self.open_gap, resolved_at=at, affected_instruments=tuple(sorted(set(affected))),
            affected_minutes=tuple(affected_minutes), reconnect_attempts=self.reconnect_attempt,
            state="RESOLVED",
        )
        self.repository.save_gap(resolved)
        self.open_gap = None
        return resolved


class TickNormalizer:
    def __init__(self, token_map: InstrumentTokenMap, session: TradingSession, *, stale_after_seconds: int = 10) -> None:
        self.token_map = token_map
        self.session = session
        self.stale_after = Decimal(stale_after_seconds)

    @staticmethod
    def _depth(values: Any) -> tuple[DepthLevel, ...]:
        if not values:
            return ()
        result = []
        for item in list(values)[:5]:
            result.append(DepthLevel(Decimal(str(item["price"])), int(item["quantity"]), int(item["orders"]) if item.get("orders") is not None else None))
        return tuple(result)

    def normalize(self, raw: dict[str, Any], received_at: datetime, connection_id: str, generation: int) -> LiveTick:
        if received_at.tzinfo is None:
            raise ValueError("receive timestamp must be timezone-aware")
        token = int(raw["instrument_token"])
        instrument = self.token_map.resolve(token)
        event_at, was_naive = _aware(raw.get("exchange_timestamp") or raw.get("last_trade_time"))
        bids = self._depth(raw.get("depth", {}).get("buy"))
        asks = self._depth(raw.get("depth", {}).get("sell"))
        flags: set[str] = set()
        price = Decimal(str(raw["last_price"]))
        if was_naive:
            flags.add("BROKER_LOCAL_TIMESTAMP_INTERPRETED")
        if not (self.session.open_at <= event_at < self.session.close_at):
            flags.add("OUTSIDE_SESSION")
        if instrument.expiry is not None and instrument.expiry < self.session.session_date:
            flags.add("EXPIRED_CONTRACT")
        age = Decimal(str((received_at.astimezone(UTC) - event_at).total_seconds()))
        if age > self.stale_after:
            flags.add("STALE_TICK")
        if age < Decimal("-2"):
            flags.add("HOST_CLOCK_SKEW")
        if price <= 0:
            flags.add("IMPOSSIBLE_PRICE")
        for key in ("volume_traded", "oi", "oi_day_high", "oi_day_low"):
            if raw.get(key) is not None and int(raw[key]) < 0:
                flags.add("NEGATIVE_VOLUME_OR_OI")
                flags.add("NEGATIVE_VOLUME" if key == "volume_traded" else "NEGATIVE_OI")
        if len(bids) > 5 or len(asks) > 5 or any(item.price < 0 or item.quantity < 0 for item in bids + asks):
            flags.add("BROKEN_DEPTH")
        if bids and asks and bids[0].price > asks[0].price:
            flags.add("CROSSED_MARKET")
        if raw.get("tradingsymbol") and raw["tradingsymbol"] != instrument.tradingsymbol:
            flags.add("TOKEN_SYMBOL_MISMATCH")
        raw_checksum = sha256(json.dumps(raw, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
        identity = (connection_id, token, event_at.isoformat(), raw_checksum)
        tick_id = _id("tick", *identity)
        ohlc = raw.get("ohlc") or {}
        return LiveTick(
            tick_id, instrument.instrument_id, token, instrument.tradingsymbol, instrument.exchange,
            instrument.instrument_type, event_at, received_at.astimezone(UTC), price,
            int(raw["last_traded_quantity"]) if raw.get("last_traded_quantity") is not None else None,
            _decimal(raw.get("average_traded_price")), int(raw["volume_traded"]) if raw.get("volume_traded") is not None else None,
            int(raw["total_buy_quantity"]) if raw.get("total_buy_quantity") is not None else None,
            int(raw["total_sell_quantity"]) if raw.get("total_sell_quantity") is not None else None,
            int(raw["oi"]) if raw.get("oi") is not None else None,
            int(raw["oi_day_high"]) if raw.get("oi_day_high") is not None else None,
            int(raw["oi_day_low"]) if raw.get("oi_day_low") is not None else None,
            {key: _decimal(ohlc.get(key)) for key in ("open", "high", "low", "close")},
            bids, asks, connection_id, generation, tuple(sorted(flags)),
        )


class MinuteBuilder:
    def __init__(self, repository: LiveRepository, instruments: dict[int, LiveInstrument], *, lateness_seconds: int = 3) -> None:
        self.repository = repository
        self.instruments = instruments
        self.lateness = timedelta(seconds=lateness_seconds)
        self.buckets: dict[tuple[int, datetime], list[LiveTick]] = defaultdict(list)
        self.finalized_keys: set[tuple[int, datetime]] = set()
        self.previous: dict[int, FinalizedMinute] = {}
        self.universe_by_token: dict[int, OptionUniverse] = {}

    def set_universe(self, universe: OptionUniverse, instruments: Sequence[LiveInstrument]) -> None:
        for item in instruments:
            if item.instrument_token in universe.tokens:
                self.universe_by_token[item.instrument_token] = universe

    def add(self, tick: LiveTick) -> bool:
        minute = tick.event_at.replace(second=0, microsecond=0)
        key = (tick.instrument_id, minute)
        if key in self.finalized_keys:
            self.repository.save_minute_correction(LiveMinuteCorrection(
                _id("correction", tick.tick_id), tick.instrument_id, minute,
                tick.tick_id, tick.received_at,
            ))
            self.repository.save_live_issue(LiveQualityIssue(
                _id("issue", "LATE_TICK", tick.tick_id), "LATE_TICK", tick.received_at,
                tick.instrument_id, minute, {"tick_id": tick.tick_id},
            ))
            return False
        self.buckets[key].append(tick)
        return True

    def finalize_ready(self, clock: datetime, *, unresolved_gap: bool = False, awaiting_option_fresh: bool = False) -> tuple[list[FinalizedMinute], list[OptionMinuteSnapshot]]:
        ready = [key for key in self.buckets if clock >= key[1] + timedelta(minutes=1) + self.lateness]
        minutes: list[FinalizedMinute] = []
        options: list[OptionMinuteSnapshot] = []
        for key in sorted(ready, key=lambda item: (item[1], item[0])):
            ticks = sorted(self.buckets.pop(key), key=lambda item: (item.event_at, item.received_at, item.tick_id))
            flags = set(flag for tick in ticks for flag in tick.quality_flags)
            if unresolved_gap:
                flags.add("RECONNECT_GAP")
            if awaiting_option_fresh and self.instruments[key[0]].instrument_type in {'CE','PE'}:
                flags.add('OPTION_UNIVERSE_NOT_FRESH')
            previous = self.previous.get(key[0])
            cumulative = None if "NEGATIVE_VOLUME" in flags else ticks[-1].cumulative_volume
            volume_delta = None
            if cumulative is not None and previous and previous.cumulative_volume is not None:
                delta = cumulative - previous.cumulative_volume
                if delta < 0:
                    flags.add("INVALID_VOLUME_DELTA")
                else:
                    volume_delta = delta
            oi_close = None if "NEGATIVE_OI" in flags else ticks[-1].oi
            oi_change = None
            if oi_close is not None and previous and previous.oi_close is not None and "RECONNECT_GAP" not in flags:
                oi_change = oi_close - previous.oi_close
            values = [item.last_price for item in ticks]
            payload = {
                "instrument": key[0], "minute": key[1].isoformat(), "open": str(values[0]),
                "high": str(max(values)), "low": str(min(values)), "close": str(values[-1]),
                "volume_delta": volume_delta, "oi_close": oi_close, "oi_change": oi_change,
                "ticks": [item.tick_id for item in ticks], "flags": sorted(flags),
            }
            checksum = sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            minute = FinalizedMinute(
                _id("minute", key[0], key[1].isoformat(), 1), key[0], key[1], 1, clock,
                values[0], max(values), min(values), values[-1], cumulative, volume_delta,
                oi_close, oi_change, ticks[0].event_at, ticks[-1].event_at, len(ticks),
                tuple(sorted({item.connection_id for item in ticks})), tuple(sorted(flags)),
                "PASS" if not (flags - {"BROKER_LOCAL_TIMESTAMP_INTERPRETED"}) else "DEGRADED",
                checksum,
            )
            self.repository.save_finalized_minute(minute)
            self.finalized_keys.add(key)
            if minute.quality_state == "PASS":
                self.previous[key[0]] = minute
            minutes.append(minute)
            instrument = self.instruments[key[0]]
            universe = self.universe_by_token.get(instrument.instrument_token)
            if universe and instrument.instrument_type in {"CE", "PE"}:
                options.append(self._option_snapshot(minute, ticks[-1], instrument, universe))
        return minutes, options

    def _option_snapshot(self, minute: FinalizedMinute, tick: LiveTick, instrument: LiveInstrument, universe: OptionUniverse) -> OptionMinuteSnapshot:
        bid = tick.bids[0] if tick.bids else None
        ask = tick.asks[0] if tick.asks else None
        spread = ask.price - bid.price if bid and ask else None
        spread_pct = spread / tick.last_price if spread is not None and tick.last_price else None
        strikes = sorted({self.instruments[item].strike for item in universe.instrument_ids})
        offset = strikes.index(instrument.strike) - strikes.index(universe.atm_strike)
        snapshot = OptionMinuteSnapshot(
            _id("option-minute", instrument.instrument_id, minute.minute_at.isoformat(), universe.universe_id),
            instrument.instrument_id, minute.minute_at, universe.universe_id, instrument.expiry,
            instrument.strike, instrument.instrument_type, offset, tick.last_price,
            minute.cumulative_volume, minute.volume_delta, minute.oi_close, minute.oi_change,
            bid.price if bid else None, bid.quantity if bid else None,
            ask.price if ask else None, ask.quantity if ask else None, spread, spread_pct,
            tick.bids, tick.asks, tick.total_buy_quantity, tick.total_sell_quantity,
            Decimal(str((tick.received_at - tick.event_at).total_seconds())),
            minute.quality_flags, minute.quality_state,
            first_seen_at=datetime.now(UTC),
        )
        self.repository.save_option_snapshot(snapshot)
        return snapshot


class LiveCollector:
    def __init__(
        self, adapter: LiveMarketAdapter, repository: LiveRepository,
        token_map: InstrumentTokenMap, session: TradingSession, connection_id: str,
        *, stale_after_seconds: int = 10, lateness_seconds: int = 3, queue_limit: int = 2048,
    ) -> None:
        if queue_limit < 32:
            raise ValueError("live queue limit is too small")
        self.adapter = adapter
        self.repository = repository
        self.token_map = token_map
        self.session = session
        self.connection = ConnectionState(repository, connection_id)
        self.registry = SubscriptionRegistry(adapter, repository)
        self.normalizer = TickNormalizer(token_map, session, stale_after_seconds=stale_after_seconds)
        self.builder = MinuteBuilder(repository, {item.instrument_id: item for item in token_map.by_token.values()}, lateness_seconds=lateness_seconds)
        self.queue_limit = queue_limit
        self.last_event_by_token: dict[int, datetime] = {}
        self.stats = defaultdict(int)
        self.clock = session.open_at

    def on_state(self, state: str, reason: str | None = None, *, at: datetime | None = None) -> None:
        at = at or self.clock
        self.connection.transition(state, at, reason)
        if state == "CONNECTED" and self.connection.reconnect_attempt:
            self.registry.restore_after_reconnect(at)

    def on_ticks(self, raw_ticks: list[dict[str, Any]], *, received_at: datetime | None = None) -> None:
        received_at = received_at or self.clock
        if len(raw_ticks) > self.queue_limit:
            self.repository.save_live_issue(LiveQualityIssue(
                _id("issue", "BACKPRESSURE", received_at.isoformat()), "BACKPRESSURE_LIMIT", received_at,
                None, None, {"batch_size": len(raw_ticks), "limit": self.queue_limit},
            ))
            raise OverflowError("live tick batch exceeds bounded queue limit")
        for raw in raw_ticks:
            try:
                tick = self.normalizer.normalize(raw, received_at, self.connection.connection_id, self.registry.generation)
            except ValueError as exc:
                self.stats["unknown"] += 1
                self.repository.save_live_issue(LiveQualityIssue(
                    _id("issue", "NORMALIZE", received_at.isoformat(), raw.get("instrument_token")),
                    "UNKNOWN_TOKEN", received_at, None, None, {"error": str(exc)},
                ))
                continue
            previous_event = self.last_event_by_token.get(tick.instrument_token)
            if self.registry.desired and tick.instrument_token not in self.registry.desired:
                tick = replace(tick, quality_flags=tuple(sorted(set(tick.quality_flags) | {"UNEXPECTED_SUBSCRIPTION"})))
            if self.connection.open_gap is not None:
                tick = replace(tick, quality_flags=tuple(sorted(set(tick.quality_flags) | {"RECONNECT_GAP"})))
            if previous_event and tick.event_at < previous_event:
                tick = replace(tick, quality_flags=tuple(sorted(set(tick.quality_flags) | {"OUT_OF_ORDER"})))
                self.stats["out_of_order"] += 1
            inserted = self.repository.save_raw_tick(tick)
            if not inserted:
                self.stats["duplicates"] += 1
                continue
            self.last_event_by_token[tick.instrument_token] = max(previous_event or tick.event_at, tick.event_at)
            self.connection.record_tick(received_at)
            self.stats["ticks"] += 1
            self.stats["stale"] += int("STALE_TICK" in tick.quality_flags)
            for flag in tick.quality_flags:
                self.repository.save_live_issue(LiveQualityIssue(
                    _id("issue", flag, tick.tick_id), flag, received_at,
                    tick.instrument_id, tick.event_at.replace(second=0, microsecond=0),
                    {"tick_id": tick.tick_id},
                ))
            fatal = {
                "OUTSIDE_SESSION", "IMPOSSIBLE_PRICE", "NEGATIVE_VOLUME_OR_OI",
                "BROKEN_DEPTH", "TOKEN_SYMBOL_MISMATCH", "EXPIRED_CONTRACT",
                "UNEXPECTED_SUBSCRIPTION",
            }
            if not fatal.intersection(tick.quality_flags):
                self.builder.add(tick)
            self.registry.confirm_fresh(tick.instrument_token, tick.received_at)
        if self.connection.open_gap and not self.registry.awaiting_fresh:
            self.connection.resolve_gap(received_at, self.registry.desired)

    def finalize(self, clock: datetime) -> tuple[list[FinalizedMinute], list[OptionMinuteSnapshot]]:
        self.clock = clock
        awaiting = bool(self.registry.awaiting_fresh)
        if awaiting:
            self.repository.save_live_issue(LiveQualityIssue(
                _id("issue", "NO_FRESH_AFTER_ROTATION", self.registry.generation, clock.isoformat()),
                "NO_FRESH_OPTION_AFTER_ROTATION", clock, None, clock.replace(second=0, microsecond=0),
                {"tokens": sorted(self.registry.awaiting_fresh)},
            ))
        return self.builder.finalize_ready(clock, unresolved_gap=self.connection.open_gap is not None,
                                           awaiting_option_fresh=awaiting)
