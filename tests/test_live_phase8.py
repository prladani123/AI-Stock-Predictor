from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import inspect
import json

import pytest

from banknifty_foundation import cli
from banknifty_foundation.live import (
    ConnectionState, ContractSelector, InstrumentTokenMap, LiveCollector,
    MinuteBuilder, SubscriptionRegistry, TickNormalizer,
)
from banknifty_foundation.live_adapters import KiteTickerLiveAdapter, SimulatedLiveAdapter
from banknifty_foundation.live_models import LiveGap, LiveInstrument
from banknifty_foundation.live_repository import (
    LiveMemoryRepository, LiveRepository, PostgresLiveRepository,
)
from banknifty_foundation.live_simulation import (
    _tick, run_phase8_simulation, simulation_instruments,
)
from banknifty_foundation.models import TradingSession


def _session() -> TradingSession:
    start = datetime(2024, 7, 24, 3, 45, tzinfo=timezone.utc)
    return TradingSession(date(2024, 7, 24), start, start + timedelta(minutes=6), "SPECIAL")


def _collector():
    instruments = simulation_instruments()
    repository = LiveMemoryRepository()
    holder = {}
    adapter = SimulatedLiveAdapter(
        lambda ticks: holder["collector"].on_ticks(ticks),
        lambda state, reason: holder["collector"].on_state(state, reason),
    )
    collector = LiveCollector(adapter, repository, InstrumentTokenMap(instruments, instruments[0].snapshot_version), _session(), "test-connection")
    holder["collector"] = collector
    return instruments, repository, adapter, collector


def test_live_adapters_expose_read_only_capabilities_and_real_constructor_does_not_connect():
    forbidden = {"place_order", "modify_order", "cancel_order", "positions", "holdings", "margins", "order_history"}
    assert not forbidden.intersection(dir(KiteTickerLiveAdapter))
    calls = []
    class FakeTicker:
        MODE_FULL = "full"
        def __init__(self, *_args, **_kwargs): calls.append("constructed")
        def connect(self, **_kwargs): calls.append("connected")
    adapter = KiteTickerLiveAdapter("key", "token", lambda _ticks: None, lambda *_: None, ticker_factory=FakeTicker)
    assert calls == ["constructed"]
    assert KiteTickerLiveAdapter.SAFE_PUBLIC_METHODS == frozenset({"connect", "subscribe", "unsubscribe", "set_mode", "close"})


def test_collect_live_fails_closed_without_confirmation_or_credentials(monkeypatch):
    parser = cli.build_parser()
    args = parser.parse_args(["collect-live", "--instrument-snapshot", "x", "--session", "x", "--underlying", "1"])
    with pytest.raises(RuntimeError, match="confirm-real"):
        args.func(args)
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    monkeypatch.setenv("ALLOW_REAL_ZERODHA", "false")
    args.confirm_real = True
    with pytest.raises(RuntimeError, match="disabled"):
        args.func(args)


def test_connection_state_records_reconnect_gap_and_one_way_resolution():
    repository = LiveMemoryRepository()
    state = ConnectionState(repository, "connection")
    start = _session().open_at
    state.transition("CONNECTING", start)
    state.transition("CONNECTED", start + timedelta(seconds=1))
    state.record_tick(start + timedelta(seconds=2))
    state.transition("DISCONNECTED", start + timedelta(seconds=3), "drop")
    assert state.open_gap and state.open_gap.state == "OPEN"
    state.transition("RECONNECTING", start + timedelta(seconds=4))
    state.transition("CONNECTED", start + timedelta(seconds=5))
    resolved = state.resolve_gap(start + timedelta(seconds=6), [1, 2])
    assert resolved and resolved.state == "RESOLVED" and resolved.affected_instruments == (1, 2)


def test_subscriptions_are_idempotent_rotate_new_before_old_and_restore():
    repository = LiveMemoryRepository()
    adapter = SimulatedLiveAdapter(lambda _ticks: None, lambda *_: None)
    registry = SubscriptionRegistry(adapter, repository)
    at = _session().open_at
    registry.replace([1, 2], at)
    registry.confirm_fresh(1, at); registry.confirm_fresh(2, at)
    registry.replace([1, 2], at + timedelta(seconds=1))
    assert adapter.operations[0]["tokens"] == [1, 2]
    assert len(adapter.operations) == 2
    registry.replace([2, 3], at + timedelta(seconds=2))
    assert adapter.operations[-2]["operation"] == "subscribe"
    assert registry.confirm_fresh(3, at + timedelta(seconds=2)) == (1,)
    assert adapter.operations[-1] == {"operation": "unsubscribe", "tokens": [1]}
    adapter.active_tokens.clear()
    registry.restore_after_reconnect(at + timedelta(seconds=3))
    assert adapter.operations[-2]["tokens"] == [2, 3]


def test_token_map_requires_current_snapshot_and_rejects_unknown_tokens():
    instruments = simulation_instruments()
    token_map = InstrumentTokenMap(instruments, instruments[0].snapshot_version)
    assert token_map.resolve(260105).instrument_type == "INDEX"
    with pytest.raises(ValueError, match="unknown"):
        token_map.resolve(999)
    with pytest.raises(ValueError, match="unknown"):
        InstrumentTokenMap(instruments, "wrong").resolve(260105)


def test_expiry_atm_pairs_and_futures_selection_are_deterministic_and_point_in_time():
    instruments = simulation_instruments()
    selector = ContractSelector(instruments)
    at = _session().open_at
    first = selector.select_options(Decimal("51520"), at)
    moved = selector.select_options(Decimal("51620"), at + timedelta(minutes=1))
    assert (first.atm_strike, moved.atm_strike) == (Decimal("51500"), Decimal("51600"))
    assert first.underlying_value == Decimal("51520")
    assert len(first.tokens) == 22 and first.quality_state == "PASS"
    assert first != moved  # historical selection is immutable, not retroactively replaced
    future, selected = selector.select_future(at)
    assert future.expiry == date(2024, 7, 25) and len(selected) == 1
    without_near = [item for item in instruments if item.instrument_id != 2]
    assert ContractSelector(without_near).select_future(at)[0].expiry == date(2024, 8, 29)


def test_missing_expiry_pair_and_expired_future_fail_safely():
    instruments = simulation_instruments()
    with pytest.raises(ValueError, match="option expiry"):
        ContractSelector([item for item in instruments if item.instrument_type not in {"CE", "PE"}]).select_options(Decimal("51500"), _session().open_at)
    incomplete = [item for item in instruments if not (item.strike == Decimal("51500") and item.instrument_type == "PE")]
    universe = ContractSelector(incomplete).select_options(Decimal("51500"), _session().open_at)
    assert "51500" in universe.missing_pairs and universe.quality_state == "INCOMPLETE_UNIVERSE"
    assert "MISSING_CE_PE_PAIR" in universe.rejection_reasons
    expired = [replace(item, expiry=date(2024, 7, 23)) if item.instrument_type == "FUT" else item for item in instruments]
    with pytest.raises(ValueError, match="unexpired"):
        ContractSelector(expired).select_future(_session().open_at)


def test_expiry_transition_uses_next_persisted_expiry_without_symbol_guessing():
    selector = ContractSelector(simulation_instruments())
    after_first_expiry = datetime(2024, 7, 26, 3, 45, tzinfo=timezone.utc)
    universe = selector.select_options(Decimal("51500"), after_first_expiry)
    assert universe.expiry == date(2024, 8, 1)
    assert all(token >= 3000000 for token in universe.tokens)


def test_tick_preserves_event_receive_depth_and_quality_flags():
    instruments = simulation_instruments()
    normalizer = TickNormalizer(InstrumentTokenMap(instruments, instruments[0].snapshot_version), _session(), stale_after_seconds=10)
    raw, received = _tick(instruments[3], _session().open_at + timedelta(seconds=1), Decimal("100"), 1, stale=True)
    tick = normalizer.normalize(raw, received, "connection", 1)
    assert tick.event_at != tick.received_at
    assert len(tick.bids) == len(tick.asks) == 5
    assert tick.asks[0].price - tick.bids[0].price == Decimal("0.10")
    assert "STALE_TICK" in tick.quality_flags
    raw["tradingsymbol"] = "WRONG"
    assert "TOKEN_SYMBOL_MISMATCH" in normalizer.normalize(raw, received, "connection", 1).quality_flags


def test_outside_session_and_invalid_market_values_are_not_silent():
    instruments = simulation_instruments()
    normalizer = TickNormalizer(InstrumentTokenMap(instruments, instruments[0].snapshot_version), _session())
    raw, received = _tick(instruments[1], _session().open_at - timedelta(minutes=1), Decimal("100"), 1)
    raw["oi"] = -1
    raw["depth"]["buy"][0]["price"] = 101
    raw["depth"]["sell"][0]["price"] = 100
    flags = normalizer.normalize(raw, received, "connection", 1).quality_flags
    assert {"OUTSIDE_SESSION", "NEGATIVE_VOLUME_OR_OI", "CROSSED_MARKET"}.issubset(flags)


def test_duplicate_and_out_of_order_ticks_are_idempotent_and_flagged():
    instruments, repository, adapter, collector = _collector()
    collector.clock = _session().open_at
    adapter.connect()
    collector.registry.replace([instruments[0].instrument_token], collector.clock)
    first, received = _tick(instruments[0], collector.clock + timedelta(seconds=20), Decimal("100"), 1)
    collector.on_ticks([first], received_at=received)
    collector.on_ticks([first], received_at=received)
    older, received2 = _tick(instruments[0], collector.clock + timedelta(seconds=10), Decimal("99"), 2)
    collector.on_ticks([older], received_at=received2)
    assert len(repository.raw_ticks) == 2
    assert collector.stats["duplicates"] == collector.stats["out_of_order"] == 1
    assert any("OUT_OF_ORDER" in item.quality_flags for item in repository.raw_ticks.values())


def test_minute_ohlc_volume_oi_and_missing_prior_semantics():
    instruments, repository, adapter, collector = _collector()
    index = instruments[0]
    at = _session().open_at
    adapter.connect(); collector.registry.replace([index.instrument_token], at)
    for second, price, sequence in ((5, "100", 1), (20, "102", 2), (40, "99", 3), (50, "101", 4)):
        raw, received = _tick(index, at + timedelta(seconds=second), Decimal(price), sequence)
        collector.on_ticks([raw], received_at=received)
    first, _ = collector.finalize(at + timedelta(minutes=1, seconds=4))
    assert (first[0].open, first[0].high, first[0].low, first[0].close) == tuple(map(Decimal, ("100", "102", "99", "101")))
    assert first[0].volume_delta is None and first[0].oi_change is None
    raw, received = _tick(index, at + timedelta(minutes=1, seconds=20), Decimal("103"), 5)
    collector.on_ticks([raw], received_at=received)
    second, _ = collector.finalize(at + timedelta(minutes=2, seconds=4))
    assert second[0].volume_delta == 10 and second[0].oi_change == 20


def test_negative_volume_delta_and_gap_make_deltas_unavailable():
    instruments, repository, adapter, collector = _collector()
    index = instruments[0]; at = _session().open_at
    adapter.connect(); collector.registry.replace([index.instrument_token], at)
    raw, received = _tick(index, at + timedelta(seconds=10), Decimal("100"), 10)
    collector.on_ticks([raw], received_at=received); collector.finalize(at + timedelta(minutes=1, seconds=4))
    raw, received = _tick(index, at + timedelta(minutes=1, seconds=10), Decimal("101"), 1)
    collector.on_ticks([raw], received_at=received)
    values, _ = collector.finalize(at + timedelta(minutes=2, seconds=4))
    assert values[0].volume_delta is None and "INVALID_VOLUME_DELTA" in values[0].quality_flags
    assert values[0].quality_state == "DEGRADED"


def test_late_tick_cannot_amend_first_finalization_or_prediction_storage():
    instruments, repository, adapter, collector = _collector()
    index = instruments[0]; at = _session().open_at
    adapter.connect(); collector.registry.replace([index.instrument_token], at)
    raw, received = _tick(index, at + timedelta(seconds=10), Decimal("100"), 1)
    collector.on_ticks([raw], received_at=received)
    values, _ = collector.finalize(at + timedelta(minutes=1, seconds=4))
    checksum = values[0].checksum
    late, late_received = _tick(index, at + timedelta(seconds=30), Decimal("200"), 2)
    collector.on_ticks([late], received_at=at + timedelta(minutes=2))
    assert next(iter(repository.finalized_minutes.values())).checksum == checksum
    assert any(item.code == "LATE_TICK" for item in repository.live_issues.values())
    assert len(repository.minute_corrections) == 1
    assert not hasattr(repository, "predictions")


def test_option_snapshot_spread_depth_delta_and_universe_lineage():
    repository, report = run_phase8_simulation()
    snapshots = list(repository.option_snapshots.values())
    assert report["full_depth_levels"] == 5
    assert all(item.spread == Decimal("0.10") and len(item.bids) == len(item.asks) == 5 for item in snapshots)
    assert any(item.oi_change == 20 and item.volume_delta == 10 for item in snapshots)
    assert all(item.universe_id in repository.option_universes for item in snapshots)


def test_reconnect_restores_every_desired_subscription_and_marks_gap_quality():
    repository, report = run_phase8_simulation()
    reconnect_index = next(i for i, item in enumerate(report["subscription_operations"]) if item["operation"] == "reconnect")
    restored = next(item for item in report["subscription_operations"][reconnect_index:] if item["operation"] == "subscribe")
    assert len(restored["tokens"]) == 24
    assert report["resolved_gap_count"] == 1
    assert "RECONNECT_GAP" in report["quality_issues"]
    assert any("RECONNECT_GAP" in item.quality_flags for item in repository.finalized_minutes.values())


def test_simulation_rotates_atm_with_overlap_and_never_uses_full_chain():
    _repository, report = run_phase8_simulation()
    assert [item["atm"] for item in report["atm_history"]] == ["51500", "51600"]
    operations = report["subscription_operations"]
    add_new = next(i for i, item in enumerate(operations) if item["operation"] == "subscribe" and item.get("tokens") == [2000022, 2000023])
    remove_old = next(i for i, item in enumerate(operations) if item["operation"] == "unsubscribe")
    assert add_new < remove_old and report["option_universe_size"] == 22


def test_retention_defaults_dry_run_and_requires_verified_range():
    repository, _report = run_phase8_simulation()
    cutoff = _session().open_at + timedelta(days=30)
    before = len(repository.raw_ticks)
    result = repository.raw_tick_retention(cutoff)
    assert result["dry_run"] and len(repository.raw_ticks) == before
    with pytest.raises(ValueError, match="verified"):
        repository.raw_tick_retention(cutoff, execute=True)
    repository.verified_tick_ranges.add((_session().open_at, cutoff))
    assert repository.raw_tick_retention(cutoff, execute=True)["deleted_count"] == before


def test_memory_and_postgres_live_repositories_expose_same_protocol():
    required = {name for name, value in inspect.getmembers(LiveRepository, inspect.isfunction)}
    assert required <= set(dir(LiveMemoryRepository))
    assert required <= set(dir(PostgresLiveRepository))
    repository, _report = run_phase8_simulation()
    tick = next(iter(repository.raw_ticks.values()))
    snapshot = next(iter(repository.option_snapshots.values()))
    assert repository.save_raw_tick(tick) is False
    assert repository.save_option_snapshot(snapshot) is False
    postgres_source = inspect.getsource(PostgresLiveRepository)
    assert '"tick_id"' in postgres_source and '"snapshot_id"' in postgres_source


def test_bounded_batch_backpressure_and_unconfirmed_rotation_are_explicit():
    instruments = simulation_instruments()
    repository = LiveMemoryRepository()
    adapter = SimulatedLiveAdapter(lambda _ticks: None, lambda *_: None)
    collector = LiveCollector(
        adapter, repository, InstrumentTokenMap(instruments, instruments[0].snapshot_version),
        _session(), "bounded", queue_limit=32,
    )
    with pytest.raises(OverflowError, match="bounded"):
        collector.on_ticks([{}] * 33, received_at=_session().open_at)
    assert any(item.code == "BACKPRESSURE_LIMIT" for item in repository.live_issues.values())
    collector.registry.replace([instruments[0].instrument_token], _session().open_at)
    collector.finalize(_session().open_at + timedelta(minutes=1, seconds=4))
    assert any(item.code == "NO_FRESH_OPTION_AFTER_ROTATION" for item in repository.live_issues.values())


def test_session_close_finalizes_only_in_session_buckets():
    repository, _report = run_phase8_simulation()
    session = _session()
    assert repository.finalized_minutes
    assert all(session.open_at <= item.minute_at < session.close_at for item in repository.finalized_minutes.values())


def test_phase8_migration_has_semantic_keys_indexes_retention_and_immutability():
    migration = (cli.ROOT / "migrations" / "009_phase8_live_market_data.sql").read_text().lower()
    for table in ("live_connection_events", "live_subscription_generations", "live_gaps", "live_raw_ticks", "live_option_universes", "live_futures_selections", "live_minutes_1m", "live_option_snapshots_1m", "live_quality_issues", "live_minute_corrections"):
        assert f"create table if not exists {table}" in migration
    assert "unique(instrument_id, minute_at, revision)" in migration
    assert "permit_verified_tick_retention_only" in migration
    assert "resolve_live_gaps_only" in migration
    assert "received_at timestamptz" in migration and "event_at timestamptz" in migration


def test_phase8_cli_report_is_deterministic_valid_and_offline(tmp_path, monkeypatch):
    output = tmp_path / "phase8.json"
    def no_network(*_args, **_kwargs): raise AssertionError("network access attempted")
    monkeypatch.setattr(KiteTickerLiveAdapter, "connect", no_network)
    assert cli.main(["simulate-live-feed", "--output", str(output)]) == 0
    first = output.read_bytes()
    assert cli.main(["simulate-live-feed", "--output", str(output)]) == 0
    assert output.read_bytes() == first
    assert cli.main(["validate-live-session", "--report", str(output)]) == 0
    report = json.loads(first)
    assert report["duplicate_count"] == report["out_of_order_count"] == 1
    assert report["stale_count"] == 2  # one deliberately stale option plus the late correction tick
    assert report["late_correction_count"] == 1
    assert report["no_predictions_created"]


def test_live_source_has_no_runtime_trading_capability():
    source = "\n".join(path.read_text(encoding="utf-8") for path in (cli.ROOT / "src" / "banknifty_foundation").glob("live*.py"))
    forbidden = ("place_" + "order", "modify_" + "order", "cancel_" + "order", "get_" + "gtt", "order_" + "history")
    assert not any(item in source for item in forbidden)
