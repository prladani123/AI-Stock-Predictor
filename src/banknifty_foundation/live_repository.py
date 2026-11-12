from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from typing import Any, Protocol, Sequence

from .live_models import (
    FinalizedMinute, FuturesSelection, LiveConnection, LiveGap, LiveMinuteCorrection, LiveQualityIssue,
    LiveTick, OptionMinuteSnapshot, OptionUniverse, SubscriptionGeneration,
)
from .repository import PostgresRepository, canonical_json


class LiveRepository(Protocol):
    def save_connection(self, value: LiveConnection) -> bool: ...
    def save_gap(self, value: LiveGap) -> bool: ...
    def save_subscription_generation(self, value: SubscriptionGeneration) -> bool: ...
    def save_raw_tick(self, value: LiveTick) -> bool: ...
    def save_option_universe(self, value: OptionUniverse) -> bool: ...
    def save_futures_selection(self, value: FuturesSelection) -> bool: ...
    def save_finalized_minute(self, value: FinalizedMinute) -> bool: ...
    def save_option_snapshot(self, value: OptionMinuteSnapshot) -> bool: ...
    def save_live_issue(self, value: LiveQualityIssue) -> bool: ...
    def save_minute_correction(self, value: LiveMinuteCorrection) -> bool: ...
    def raw_tick_retention(self, before: datetime, *, execute: bool = False) -> dict[str, Any]: ...


class LiveMemoryRepository:
    def __init__(self) -> None:
        self.connections: dict[str, LiveConnection] = {}
        self.gaps: dict[str, LiveGap] = {}
        self.subscription_generations: dict[int, SubscriptionGeneration] = {}
        self.raw_ticks: dict[str, LiveTick] = {}
        self.option_universes: dict[str, OptionUniverse] = {}
        self.futures_selections: dict[str, FuturesSelection] = {}
        self.finalized_minutes: dict[str, FinalizedMinute] = {}
        self.option_snapshots: dict[str, OptionMinuteSnapshot] = {}
        self.live_issues: dict[str, LiveQualityIssue] = {}
        self.minute_corrections: dict[str, LiveMinuteCorrection] = {}
        self.verified_tick_ranges: set[tuple[datetime, datetime]] = set()

    @staticmethod
    def _save(store: dict[Any, Any], key: Any, value: Any) -> bool:
        current = store.get(key)
        if current is not None and current != value:
            raise ValueError("append-only live record cannot be changed")
        if current is not None:
            return False
        store[key] = deepcopy(value)
        return True

    def save_connection(self, value: LiveConnection) -> bool:
        # State transitions are immutable events, keyed by connection and state/time.
        key = f"{value.connection_id}:{value.state}:{value.event_at}"
        return self._save(self.connections, key, value)

    def save_gap(self, value: LiveGap) -> bool:
        current = self.gaps.get(value.gap_id)
        if current is not None:
            if current.resolved_at is None and value.resolved_at is not None:
                self.gaps[value.gap_id] = deepcopy(value)
                return True
            if current != value:
                raise ValueError("resolved gap fields cannot be changed")
            return False
        self.gaps[value.gap_id] = deepcopy(value)
        return True

    def save_subscription_generation(self, value: SubscriptionGeneration) -> bool:
        return self._save(self.subscription_generations, value.generation, value)

    def save_raw_tick(self, value: LiveTick) -> bool:
        if value.tick_id in self.raw_ticks:
            return False
        self.raw_ticks[value.tick_id] = deepcopy(value)
        return True

    def save_option_universe(self, value: OptionUniverse) -> bool:
        return self._save(self.option_universes, value.universe_id, value)

    def save_futures_selection(self, value: FuturesSelection) -> bool:
        return self._save(self.futures_selections, value.selection_id, value)

    def save_finalized_minute(self, value: FinalizedMinute) -> bool:
        return self._save(self.finalized_minutes, value.minute_id, value)

    def save_option_snapshot(self, value: OptionMinuteSnapshot) -> bool:
        return self._save(self.option_snapshots, value.snapshot_id, value)

    def save_live_issue(self, value: LiveQualityIssue) -> bool:
        return self._save(self.live_issues, value.issue_id, value)

    def save_minute_correction(self, value: LiveMinuteCorrection) -> bool:
        return self._save(self.minute_corrections, value.correction_id, value)

    def raw_tick_retention(self, before: datetime, *, execute: bool = False) -> dict[str, Any]:
        candidates = sorted(key for key, item in self.raw_ticks.items() if item.received_at < before)
        verified = any(end <= before for _start, end in self.verified_tick_ranges)
        if execute and candidates and not verified:
            raise ValueError("raw tick range has no verified aggregate/checksum evidence")
        if execute:
            for key in candidates:
                del self.raw_ticks[key]
        return {"dry_run": not execute, "candidate_count": len(candidates), "deleted_count": len(candidates) if execute else 0}


class PostgresLiveRepository:
    """PostgreSQL implementation with the same semantic keys as memory storage."""

    def __init__(self, database_url: str) -> None:
        self.base = PostgresRepository(database_url)
        self.base.connection_options={'connect_timeout':5,'options':'-c statement_timeout=10000'}

    @contextmanager
    def worker_lease(self):
        import psycopg
        with psycopg.connect(self.base.database_url,autocommit=True,connect_timeout=5) as db:
            acquired=db.execute('SELECT pg_try_advisory_lock(8172402)').fetchone()[0]
            try: yield acquired
            finally:
                if acquired: db.execute('SELECT pg_advisory_unlock(8172402)')

    def latest_generation(self):
        with self.base._connection() as db:
            return db.execute('SELECT COALESCE(max(generation),0) FROM live_subscription_generations').fetchone()[0]

    def index_minute_keys(self, instrument_id, session):
        with self.base._connection() as db:
            return {r[0] for r in db.execute('SELECT DISTINCT minute_at FROM live_minutes_1m WHERE instrument_id=%s AND minute_at>=%s AND minute_at<%s',
                (instrument_id,session.open_at,session.close_at)).fetchall()}

    def save_session_catchup(self, value):
        with self.base._connection() as db:
            db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(f'live-minute:{value.instrument_id}:{value.minute_at.isoformat()}',))
            if db.execute('SELECT 1 FROM live_minutes_1m WHERE instrument_id=%s AND minute_at=%s LIMIT 1',
                          (value.instrument_id,value.minute_at)).fetchone(): return False
            return db.execute('INSERT INTO live_minutes_1m(minute_id,payload) VALUES (%s,%s::jsonb) ON CONFLICT DO NOTHING RETURNING 1',
                              (value.minute_id,canonical_json(asdict(value)))).fetchone() is not None

    def _insert(self, table: str, key_column: str, key: Any, payload: Any) -> bool:
        with self.base._connection() as connection:
            row = connection.execute(
                f"INSERT INTO {table}({key_column}, payload) VALUES (%s,%s::jsonb) "
                "ON CONFLICT DO NOTHING RETURNING 1",
                (key, canonical_json(asdict(payload))),
            ).fetchone()
        return row is not None

    def save_connection(self, value: LiveConnection) -> bool:
        key = f"{value.connection_id}:{value.state}:{value.event_at}"
        return self._insert("live_connection_events", "event_key", key, value)

    def save_gap(self, value: LiveGap) -> bool:
        with self.base._connection() as connection:
            current = connection.execute("SELECT payload FROM live_gaps WHERE gap_id=%s", (value.gap_id,)).fetchone()
            if current and current[0].get("resolved_at") is None and value.resolved_at is not None:
                connection.execute("UPDATE live_gaps SET payload=%s::jsonb WHERE gap_id=%s", (canonical_json(asdict(value)), value.gap_id))
                return True
        return self._insert("live_gaps", "gap_id", value.gap_id, value)

    def save_subscription_generation(self, value: SubscriptionGeneration) -> bool:
        return self._insert("live_subscription_generations", "generation", value.generation, value)

    def save_raw_tick(self, value: LiveTick) -> bool:
        return self._insert("live_raw_ticks", "tick_id", value.tick_id, value)

    def save_option_universe(self, value: OptionUniverse) -> bool:
        return self._insert("live_option_universes", "universe_id", value.universe_id, value)

    def save_futures_selection(self, value: FuturesSelection) -> bool:
        return self._insert("live_futures_selections", "selection_id", value.selection_id, value)

    def save_finalized_minute(self, value: FinalizedMinute) -> bool:
        with self.base._connection() as db:
            db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(f'live-minute:{value.instrument_id}:{value.minute_at.isoformat()}',))
            return db.execute('INSERT INTO live_minutes_1m(minute_id,payload) VALUES (%s,%s::jsonb) ON CONFLICT DO NOTHING RETURNING 1',
                              (value.minute_id,canonical_json(asdict(value)))).fetchone() is not None

    def save_option_snapshot(self, value: OptionMinuteSnapshot) -> bool:
        return self._insert("live_option_snapshots_1m", "snapshot_id", value.snapshot_id, value)

    def save_live_issue(self, value: LiveQualityIssue) -> bool:
        return self._insert("live_quality_issues", "issue_id", value.issue_id, value)

    def save_minute_correction(self, value: LiveMinuteCorrection) -> bool:
        return self._insert("live_minute_corrections", "correction_id", value.correction_id, value)

    def raw_tick_retention(self, before: datetime, *, execute: bool = False) -> dict[str, Any]:
        with self.base._connection() as connection:
            count = connection.execute("SELECT count(*) FROM live_raw_ticks WHERE received_at < %s", (before,)).fetchone()[0]
            unverified = connection.execute(
                "SELECT 1 FROM live_raw_ticks tick WHERE tick.received_at < %s "
                "AND NOT EXISTS (SELECT 1 FROM live_verified_ranges verified "
                "WHERE verified.range_start <= tick.received_at AND verified.range_end > tick.received_at) LIMIT 1",
                (before,),
            ).fetchone()
            if execute and count and unverified:
                raise ValueError("raw tick range has no verified aggregate/checksum evidence")
            if execute:
                connection.execute("DELETE FROM live_raw_ticks WHERE received_at < %s", (before,))
        return {"dry_run": not execute, "candidate_count": count, "deleted_count": count if execute else 0}
