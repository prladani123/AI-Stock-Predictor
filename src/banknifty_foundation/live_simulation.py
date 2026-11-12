from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

from .calendar import IST
from .live import ContractSelector, InstrumentTokenMap, LiveCollector
from .live_adapters import SimulatedLiveAdapter
from .live_models import LiveInstrument
from .live_repository import LiveMemoryRepository
from .models import TradingSession


def simulation_instruments(snapshot_version: str = "fixture-snapshot-2024-07-24") -> list[LiveInstrument]:
    values = [
        LiveInstrument(1, 260105, snapshot_version, "NIFTY BANK", "NSE", "INDEX"),
        LiveInstrument(2, 1100001, snapshot_version, "BANKNIFTY24JULFUT", "NFO", "FUT", date(2024, 7, 25)),
        LiveInstrument(3, 1100002, snapshot_version, "BANKNIFTY24AUGFUT", "NFO", "FUT", date(2024, 8, 29)),
    ]
    instrument_id = 100
    token = 2000000
    for strike in range(51000, 52200, 100):
        for option_type in ("CE", "PE"):
            values.append(LiveInstrument(
                instrument_id, token, snapshot_version,
                f"BANKNIFTY24JUL{strike}{option_type}", "NFO", option_type,
                date(2024, 7, 25), Decimal(str(strike)),
            ))
            instrument_id += 1
            token += 1
    instrument_id = 300
    token = 3000000
    for strike in range(51000, 52200, 100):
        for option_type in ("CE", "PE"):
            values.append(LiveInstrument(
                instrument_id, token, snapshot_version,
                f"BANKNIFTY24AUG{strike}{option_type}", "NFO", option_type,
                date(2024, 8, 1), Decimal(str(strike)),
            ))
            instrument_id += 1
            token += 1
    return values


def _tick(instrument: LiveInstrument, event_at: datetime, price: Decimal, sequence: int, *, stale: bool = False) -> tuple[dict[str, Any], datetime]:
    depth_buy = [
        {"price": float(price - Decimal("0.05") * (level + 1)), "quantity": 100 + level, "orders": 2 + level}
        for level in range(5)
    ]
    depth_sell = [
        {"price": float(price + Decimal("0.05") * (level + 1)), "quantity": 90 + level, "orders": 3 + level}
        for level in range(5)
    ]
    raw = {
        "instrument_token": instrument.instrument_token,
        "tradingsymbol": instrument.tradingsymbol,
        "exchange_timestamp": event_at,
        "last_price": float(price),
        "last_traded_quantity": 5,
        "average_traded_price": float(price),
        "volume_traded": 1000 + sequence * 10 + instrument.instrument_id,
        "total_buy_quantity": 5000,
        "total_sell_quantity": 4800,
        "oi": 20000 + sequence * 20 + instrument.instrument_id,
        "oi_day_high": 25000,
        "oi_day_low": 18000,
        "ohlc": {"open": float(price), "high": float(price), "low": float(price), "close": float(price)},
        "depth": {"buy": depth_buy, "sell": depth_sell},
    }
    received = event_at + timedelta(seconds=20 if stale else 1)
    return raw, received


def run_phase8_simulation() -> tuple[LiveMemoryRepository, dict[str, Any]]:
    fixture_path = Path(__file__).resolve().parents[2] / "fixtures" / "phase8_live_scenario.json"
    scenario = json.loads(fixture_path.read_text(encoding="utf-8"))
    session_date = date.fromisoformat(scenario["session_date"])
    open_at = datetime.combine(session_date, time(9, 15), IST).astimezone(timezone.utc)
    close_at = datetime.combine(session_date, time(9, 21), IST).astimezone(timezone.utc)
    session = TradingSession(session_date, open_at, close_at, scenario["session_status"])
    instruments = simulation_instruments(scenario["snapshot_version"])
    token_map = InstrumentTokenMap(instruments, instruments[0].snapshot_version)
    repository = LiveMemoryRepository()
    holder: dict[str, LiveCollector] = {}
    adapter = SimulatedLiveAdapter(
        lambda ticks: holder["collector"].on_ticks(ticks),
        lambda state, reason: holder["collector"].on_state(state, reason),
    )
    collector = LiveCollector(adapter, repository, token_map, session, "fixture-connection-1")
    holder["collector"] = collector
    selector = ContractSelector(instruments)
    future_selection, futures = selector.select_future(open_at, include_next=False)
    repository.save_futures_selection(future_selection)
    index = next(item for item in instruments if item.instrument_type == "INDEX")
    universe1 = selector.select_options(Decimal(scenario["initial_underlying"]), open_at, width=int(scenario["strike_width"]))
    repository.save_option_universe(universe1)
    collector.builder.set_universe(universe1, instruments)
    initial_tokens = [index.instrument_token, futures[0].instrument_token, *universe1.tokens]
    collector.clock = open_at
    adapter.connect()
    collector.registry.replace(initial_tokens, open_at)

    selected1 = [token_map.resolve(token) for token in initial_tokens]
    for offset, instrument in enumerate(selected1):
        event = open_at + timedelta(seconds=20 + offset % 5)
        price = Decimal("51520") if instrument.instrument_type == "INDEX" else Decimal("100") + Decimal(offset)
        raw, received = _tick(instrument, event, price, 1, stale=(instrument.instrument_type == "CE" and offset == 2))
        collector.on_ticks([raw], received_at=received)
    # Duplicate is idempotent.
    duplicate_raw, duplicate_received = _tick(selected1[0], open_at + timedelta(seconds=20), Decimal("51520"), 1)
    collector.on_ticks([duplicate_raw], received_at=duplicate_received)
    first_minutes, first_options = collector.finalize(open_at + timedelta(minutes=1, seconds=4))
    late_raw, _late_received = _tick(index, open_at + timedelta(seconds=40), Decimal("51525"), 4)
    collector.on_ticks([late_raw], received_at=open_at + timedelta(minutes=1, seconds=6))

    universe2 = selector.select_options(Decimal(scenario["rotated_underlying"]), open_at + timedelta(minutes=1, seconds=5), width=int(scenario["strike_width"]))
    repository.save_option_universe(universe2)
    collector.builder.set_universe(universe2, instruments)
    rotated_tokens = [index.instrument_token, futures[0].instrument_token, *universe2.tokens]
    collector.registry.replace(rotated_tokens, open_at + timedelta(minutes=1, seconds=5))
    newly_added = sorted(set(universe2.tokens) - set(universe1.tokens))
    for token in newly_added:
        instrument = token_map.resolve(token)
        raw, received = _tick(instrument, open_at + timedelta(minutes=1, seconds=10), Decimal("105"), 2)
        collector.on_ticks([raw], received_at=received)

    selected2 = [token_map.resolve(token) for token in rotated_tokens]
    for offset, instrument in enumerate(selected2):
        event = open_at + timedelta(minutes=1, seconds=25 + offset % 5)
        price = Decimal("51620") if instrument.instrument_type == "INDEX" else Decimal("102") + Decimal(offset)
        raw, received = _tick(instrument, event, price, 2)
        collector.on_ticks([raw], received_at=received)
    # Deliberately out-of-order unique tick.
    out_raw, out_received = _tick(index, open_at + timedelta(minutes=1, seconds=15), Decimal("51610"), 3)
    collector.on_ticks([out_raw], received_at=out_received)
    second_minutes, second_options = collector.finalize(open_at + timedelta(minutes=2, seconds=4))

    collector.clock = open_at + timedelta(minutes=2, seconds=10)
    adapter.drop()
    collector.clock = open_at + timedelta(minutes=2, seconds=15)
    adapter.reconnect()
    for offset, instrument in enumerate(selected2):
        event = open_at + timedelta(minutes=2, seconds=20 + offset % 5)
        price = Decimal("51640") if instrument.instrument_type == "INDEX" else Decimal("104") + Decimal(offset)
        raw, received = _tick(instrument, event, price, 3)
        collector.on_ticks([raw], received_at=received)
    third_minutes, third_options = collector.finalize(open_at + timedelta(minutes=3, seconds=4))
    final_minutes, final_options = collector.finalize(close_at + timedelta(seconds=4))
    retention = repository.raw_tick_retention(open_at + timedelta(days=30), execute=False)

    all_minutes = first_minutes + second_minutes + third_minutes + final_minutes
    all_options = first_options + second_options + third_options + final_options
    gaps = list(repository.gaps.values())
    operations = adapter.operations
    payload: dict[str, Any] = {
        "schema_version": "phase-8-live-data-report-v1",
        "scenario_fixture": "fixtures/phase8_live_scenario.json",
        "mode": "DETERMINISTIC_SIMULATED_LIVE_FEED_NO_NETWORK",
        "simulated_connection_count": sum(item["operation"] == "connect" for item in operations),
        "reconnect_count": sum(item["operation"] == "reconnect" for item in operations),
        "gap_count": len(gaps),
        "resolved_gap_count": sum(item.state == "RESOLVED" for item in gaps),
        "instrument_snapshot_version": instruments[0].snapshot_version,
        "selected_futures": [{"instrument_id": item.instrument_id, "expiry": item.expiry.isoformat(), "reason": item.reason} for item in repository.futures_selections.values()],
        "selected_expiry": universe1.expiry.isoformat(),
        "atm_history": [
            {"selected_at": item.selected_at.isoformat(), "underlying": str(item.underlying_value), "atm": str(item.atm_strike), "universe_id": item.universe_id}
            for item in repository.option_universes.values()
        ],
        "option_universe_versions": len(repository.option_universes),
        "option_universe_size": len(universe2.tokens),
        "subscriptions_added": sum(len(item.get("tokens", [])) for item in operations if item["operation"] == "subscribe"),
        "subscriptions_removed": sum(len(item.get("tokens", [])) for item in operations if item["operation"] == "unsubscribe"),
        "subscription_operations": operations,
        "tick_count": collector.stats["ticks"],
        "duplicate_count": collector.stats["duplicates"],
        "out_of_order_count": collector.stats["out_of_order"],
        "stale_count": collector.stats["stale"],
        "late_correction_count": len(repository.minute_corrections),
        "minutes_finalized": len(all_minutes),
        "index_candles": sum(next(item for item in instruments if item.instrument_id == minute.instrument_id).instrument_type == "INDEX" for minute in all_minutes),
        "futures_snapshots": sum(next(item for item in instruments if item.instrument_id == minute.instrument_id).instrument_type == "FUT" for minute in all_minutes),
        "option_snapshots": len(all_options),
        "incomplete_universe_events": sum(item.quality_state != "PASS" for item in repository.option_universes.values()),
        "oi_delta_examples": [item.oi_change for item in all_options if item.oi_change is not None][:4],
        "volume_delta_examples": [item.volume_delta for item in all_options if item.volume_delta is not None][:4],
        "quality_issues": sorted({issue.code for issue in repository.live_issues.values()} | {flag for minute in all_minutes for flag in minute.quality_flags}),
        "retention_state": retention,
        "event_and_receive_time_preserved": all(tick.event_at != tick.received_at for tick in repository.raw_ticks.values()),
        "full_depth_levels": min((len(item.bids) for item in all_options), default=0),
        "no_predictions_created": True,
    }
    payload["deterministic_checksum"] = sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    return repository, payload


def write_phase8_report(path: Path) -> dict[str, Any]:
    _repository, report = run_phase8_simulation()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    return report
