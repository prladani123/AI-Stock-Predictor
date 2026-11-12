from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import signal
import sys
import threading
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from .calendar import IST, TradingCalendar, to_utc
from .config import Settings
from .coverage import CoverageReporter
from .datasets import DatasetValidationService
from .fixtures import FixtureZerodhaClient
from .features import FEATURE_NAMES, FEATURE_SET_VERSION, FeatureGenerationService
from .importer import HistoricalImporter
from .instruments import InstrumentSnapshotService
from .labels import LabelGenerationService
from .big_move_labels import BigMoveLabelGenerationService
from .big_move_features import BigMoveFeatureGenerationService
from .kronos_experiment import KronosForecastAdapter
from .banknifty_time_machine import BankniftyTimeMachine
from .external_research import (HISTORICAL_COUNTERFACTUAL, KRONOS_MODEL_NAME, PROSPECTIVE,
                                TIME_MACHINE_MODEL_NAME)
from .models import Candle, TradingSession
from .rate_limit import RateLimiter
from .repository import MemoryRepository, PostgresRepository
from .replay import WalkForwardReplay
from .research_models import ReplayManifest
from .ml_models import ModelArtifactStore
from .ml_walk_forward import DailyMLWalkForward, DailyWalkForwardConfig
from .repository import digest
from .zerodha import ReadOnlyZerodhaAdapter, create_read_only_zerodha_adapter
from .live import ContractSelector, InstrumentTokenMap, LiveCollector
from .live_adapters import KiteTickerLiveAdapter
from .live_models import LiveInstrument
from .live_repository import PostgresLiveRepository
from .live_simulation import write_phase8_report
from .market_memory import FileMarketMemoryRepository, PostgresMarketMemoryRepository
from .memory_builder import build_episodes, build_events
from .daily_rollup import finalize_day
from .news.service import NewsIntelligenceService


ROOT = Path.cwd() if (Path.cwd() / "fixtures").is_dir() else Path(__file__).resolve().parents[2]


def _fixture_session() -> TradingSession:
    session_date = date(2024, 7, 24)
    return TradingSession(
        session_date=session_date,
        open_at=datetime.combine(session_date, time(9, 15), IST).astimezone(timezone.utc),
        close_at=datetime.combine(session_date, time(9, 21), IST).astimezone(timezone.utc),
    )


def run_fixture_import(output: Path, fixture_dir: Path | None = None) -> dict:
    fixture_dir = fixture_dir or ROOT / "fixtures"
    fixed_clock = lambda: datetime(2024, 7, 25, 12, 0, tzinfo=timezone.utc)
    repository = MemoryRepository(clock=fixed_clock)
    client = FixtureZerodhaClient(fixture_dir)
    adapter = ReadOnlyZerodhaAdapter(client)
    session = _fixture_session()
    repository.store_session(session)
    calendar = TradingCalendar([session])

    _, instruments, mapping = InstrumentSnapshotService(adapter, repository).capture(
        datetime(2024, 7, 24, 2, 59, tzinfo=timezone.utc)
    )
    results: list[dict] = []
    reports: list[dict] = []
    for instrument in instruments:
        instrument_id = mapping[instrument.instrument_token]
        importer = HistoricalImporter(
            adapter,
            repository,
            calendar,
            rate_limiter=RateLimiter(1000),
            chunk_days=1,
            max_retries=3,
        )
        result = importer.run(
            instrument_id=instrument_id,
            instrument_token=instrument.instrument_token,
            start=session.open_at,
            end=session.close_at - timedelta(minutes=1),
        )
        results.append(
            {
                "instrument_token": instrument.instrument_token,
                "symbol": instrument.tradingsymbol,
                "status": result.status,
                "rows_received": result.rows_received,
                "rows_inserted": result.rows_inserted,
                "duplicate_rows": result.duplicate_rows,
                "rejected_rows": result.rejected_rows,
            }
        )
        reports.append(CoverageReporter(repository).generate(instrument_id, session).to_dict())

    payload = {
        "mode": "OFFLINE_FIXTURE_ONLY",
        "zerodha_network_calls": 0,
        "fixture_client_calls": {
            "instruments": client.instrument_calls,
            "historical_data": client.historical_calls,
        },
        "imports": results,
        "coverage": reports,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return payload


def command_fixture(args: argparse.Namespace) -> int:
    payload = run_fixture_import(Path(args.output), Path(args.fixtures) if args.fixtures else None)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def command_migrate(_: argparse.Namespace) -> int:
    settings = Settings.from_env(require_database=True)
    applied = PostgresRepository(settings.database_url, ROOT / "migrations").apply_migrations()
    print(json.dumps({"applied": applied}))
    return 0


def command_memory_build(args: argparse.Namespace) -> int:
    print(json.dumps(build_episodes(Path(args.directory)), indent=2))
    return 0


def command_memory_events(args: argparse.Namespace) -> int:
    print(json.dumps(build_events(Path(args.directory)), indent=2))
    return 0


def command_memory_ingest(args: argparse.Namespace) -> int:
    settings = Settings.from_env(require_database=True)
    source = FileMarketMemoryRepository(args.directory)
    result = PostgresMarketMemoryRepository(settings.database_url).ingest(source.days(), source.events(), source.episodes())
    print(json.dumps(result, indent=2))
    return 0


def command_news_refresh(_: argparse.Namespace) -> int:
    print(json.dumps(NewsIntelligenceService().refresh(), indent=2))
    return 0


def command_memory_finalize_day(args: argparse.Namespace) -> int:
    target = finalize_day(Path(args.directory), date.fromisoformat(args.date), force=args.force)
    print(json.dumps({"created": str(target)}, indent=2))
    return 0


def command_calendar_load(args: argparse.Namespace) -> int:
    settings = Settings.from_env(require_database=True)
    repository = PostgresRepository(settings.database_url, ROOT / "migrations")
    rows = json.loads(Path(args.file).read_text(encoding="utf-8"))
    stored = 0
    for row in rows:
        status = str(row["status"]).upper()
        if status not in {"OPEN", "CLOSED", "SPECIAL"}:
            raise ValueError("calendar status must be OPEN, CLOSED or SPECIAL")
        session = TradingSession(
            date.fromisoformat(row["session_date"]),
            to_utc(datetime.fromisoformat(row["market_open"])),
            to_utc(datetime.fromisoformat(row["market_close"])),
            status,
        )
        repository.store_session(session)
        stored += 1
    print(json.dumps({"stored_sessions": stored, "source": args.file}))
    return 0


def _write_json(output: Path, payload: dict | list) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def command_dataset_validate(args: argparse.Namespace) -> int:
    settings = Settings.from_env(require_database=True)
    repository = PostgresRepository(settings.database_url, ROOT / "migrations")
    revision, report = DatasetValidationService(repository).create_candidate(
        args.instrument_id,
        date.fromisoformat(args.from_date),
        date.fromisoformat(args.to_date),
    )
    _write_json(Path(args.output), report)
    print(json.dumps({"revision_id": revision.id, "status": revision.status, "report": args.output}, indent=2))
    return 0


def command_dataset_decide(args: argparse.Namespace) -> int:
    settings = Settings.from_env(require_database=True)
    repository = PostgresRepository(settings.database_url, ROOT / "migrations")
    revision = DatasetValidationService(repository).decide(args.revision, args.decision, args.reason)
    print(json.dumps(revision.to_dict(), indent=2, sort_keys=True))
    return 0


def command_generate_labels(args: argparse.Namespace) -> int:
    settings = Settings.from_env(require_database=True)
    repository = PostgresRepository(settings.database_url, ROOT / "migrations")
    labels, inserted = LabelGenerationService(
        repository, settings.threshold_policy_version, settings.label_thresholds()
    ).generate(args.revision, args.instrument_id)
    payload = {
        "revision_id": args.revision,
        "generated": len(labels),
        "inserted": inserted,
        "labels": [item.to_dict() for item in labels],
    }
    _write_json(Path(args.output), payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def command_generate_features(args: argparse.Namespace) -> int:
    settings = Settings.from_env(require_database=True)
    repository = PostgresRepository(settings.database_url, ROOT / "migrations")
    rows, inserted = FeatureGenerationService(repository).generate(
        args.revision, args.instrument_id, args.feature_set_version
    )
    payload = {
        "revision_id": args.revision,
        "feature_set_version": args.feature_set_version,
        "generated": len(rows),
        "inserted": inserted,
        "sample": rows[-1].to_dict() if rows else None,
    }
    _write_json(Path(args.output), payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def command_generate_big_move_labels(args: argparse.Namespace) -> int:
    settings = Settings.from_env(require_database=True)
    repository = PostgresRepository(settings.database_url, ROOT / "migrations")
    labels, inserted = BigMoveLabelGenerationService(repository).generate(args.revision, args.instrument_id)
    payload = {"revision_id": args.revision, "schema": "BIG_MOVE_V2",
               "generated": len(labels), "inserted": inserted,
               "labels": [item.to_dict() for item in labels]}
    _write_json(Path(args.output), payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def command_generate_big_move_features(args: argparse.Namespace) -> int:
    settings = Settings.from_env(require_database=True)
    repository = PostgresRepository(settings.database_url, ROOT / "migrations")
    rows, inserted = BigMoveFeatureGenerationService(repository).generate(args.revision, args.instrument_id)
    payload = {"revision_id": args.revision, "feature_set_version": "banknifty-bigmove-v2",
               "generated": len(rows), "inserted": inserted,
               "sample": rows[-1].to_dict() if rows else None}
    _write_json(Path(args.output), payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def command_kronos_experiment(args: argparse.Namespace) -> int:
    adapter = KronosForecastAdapter(model_name=args.model)
    payload = {"status": "EXPERIMENT_NOT_RUN", "as_of": args.as_of,
               "lookback": args.lookback, "pred_len": 30, **adapter.availability(),
               "message": "Use kronos-research-run with the local Docker research service."}
    _write_json(Path(args.output), payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _research_repository():
    settings = Settings.from_env(require_database=True)
    return PostgresRepository(settings.database_url, ROOT / "migrations")


def _research_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("research timestamps must be timezone-aware")
    return parsed


def command_external_research_preflight(args: argparse.Namespace) -> int:
    third_party = ROOT / "third_party"
    kronos_root = third_party / "Kronos"
    india_root = third_party / "india-trader-skill"
    model_root = Path(args.models_dir) if args.models_dir else kronos_root / "models"
    required = {
        "kronosSource": kronos_root / "model" / "__init__.py",
        "kronosServer": kronos_root / "kronos_research_server.py",
        "kronosUpstream": kronos_root / "UPSTREAM.md",
        "kronosMini": model_root / "Kronos-mini" / "model.safetensors",
        "kronosTokenizer": model_root / "Kronos-Tokenizer-2k" / "model.safetensors",
        "indiaTraderSource": india_root / "bharat-timemachine" / "timemachine.py",
        "indiaTraderUpstream": india_root / "UPSTREAM.md",
    }
    checks = {name: path.is_file() for name, path in required.items()}
    service = KronosForecastAdapter(service_url=args.service_url or None).health()
    payload = {
        "downloadsPerformed": False,
        "thirdPartySources": {"kronos": str(kronos_root), "indiaTrader": str(india_root)},
        "checks": checks,
        "kronosService": service,
        "timeMachine": {"available": checks["indiaTraderSource"], "minimumPriorSessions": 20,
                        "slotWindowMinutes": 2, "pathCount": 10_000},
        "status": "READY" if all(checks.values()) and service.get("status") == "READY"
                  else "DEGRADED",
    }
    _write_json(Path(args.output), payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def command_kronos_research_run(args: argparse.Namespace) -> int:
    repository = _research_repository()
    reference = _research_timestamp(args.as_of)
    candles = repository.revision_candles(args.revision, args.instrument_id)
    adapter = KronosForecastAdapter(service_url=args.service_url or None,
                                    sample_paths=args.paths, seed=args.seed,
                                    timeout_seconds=args.timeout)
    result = adapter.forecast(candles, as_of=reference, lookback=args.lookback,
        sample_count=args.paths,
        point_in_time_status=PROSPECTIVE if args.prospective else HISTORICAL_COUNTERFACTUAL)
    persisted = repository.save_experimental_output(result, args.revision,
        args.instrument_id) if result.status == "COMPLETED" else False
    payload = result.to_dict()
    payload["persisted"] = persisted
    payload["modelName"] = KRONOS_MODEL_NAME
    payload["researchOnly"] = True
    payload["usedByPriceCoreV2"] = False
    _write_json(Path(args.output), payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if result.status == "COMPLETED" else 1


def command_time_machine_run(args: argparse.Namespace) -> int:
    repository = _research_repository()
    reference = _research_timestamp(args.as_of)
    machine = BankniftyTimeMachine(repository, sample_paths=args.paths,
        slot_window_minutes=args.slot_window, minimum_prior_sessions=args.min_prior_sessions,
        seed=args.seed)
    result = machine.run(revision_id=args.revision, instrument_id=args.instrument_id,
        reference_minute=reference,
        point_in_time_status=PROSPECTIVE if args.prospective else HISTORICAL_COUNTERFACTUAL)
    persisted = repository.save_experimental_output(result, args.revision,
        args.instrument_id) if result.status == "COMPLETED" else False
    payload = result.to_dict()
    payload["persisted"] = persisted
    payload["modelName"] = TIME_MACHINE_MODEL_NAME
    payload["researchOnly"] = True
    payload["usedByPriceCoreV2"] = False
    _write_json(Path(args.output), payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if result.status == "COMPLETED" else 1


def command_external_research_backfill(args: argparse.Namespace) -> int:
    repository = _research_repository()
    from_date = date.fromisoformat(args.from_date)
    to_date = date.fromisoformat(args.to_date)
    if to_date < from_date:
        raise ValueError("--from must not be after --to")
    revision = repository.get_revision(args.revision)
    sessions = {item.session_date: item for item in repository.sessions_between(from_date, to_date)
                if item.status in {"OPEN", "SPECIAL"}}
    candles = sorted(repository.revision_candles(args.revision, args.instrument_id),
                     key=lambda item: item.timestamp)
    candidates: list[datetime] = []
    last_by_date: dict[date, datetime] = {}
    for candle in candles:
        day = candle.timestamp.astimezone(IST).date()
        session = sessions.get(day)
        if session is None or candle.timestamp + timedelta(minutes=30) >= session.close_at:
            continue
        prior = last_by_date.get(day)
        if prior is not None and (candle.timestamp - prior).total_seconds() < args.stride * 60:
            continue
        last_by_date[day] = candle.timestamp
        candidates.append(candle.timestamp)
    if args.max_references > 0:
        candidates = candidates[-args.max_references:]
    machine = BankniftyTimeMachine(repository, sample_paths=args.paths,
        slot_window_minutes=args.slot_window, minimum_prior_sessions=args.min_prior_sessions,
        seed=args.seed)
    adapters: list[tuple[str, Any]] = [("time_machine", machine)]
    kronos = None
    kronos_available = False
    if args.include_kronos:
        kronos = KronosForecastAdapter(service_url=args.service_url or None,
                                       sample_paths=args.kronos_paths, seed=args.seed)
        kronos_health = kronos.health()
        kronos_available = kronos_health.get("status") == "READY"
        if kronos_available:
            adapters.append(("kronos", kronos))
    outcomes: list[dict[str, Any]] = []
    counts = {name: 0 for name, _ in adapters}
    failures = 0
    for reference in candidates:
        for name, service in adapters:
            try:
                if name == "time_machine":
                    result = service.run(revision_id=args.revision,
                        instrument_id=args.instrument_id, reference_minute=reference,
                        point_in_time_status=HISTORICAL_COUNTERFACTUAL)
                else:
                    result = service.forecast(candles, as_of=reference,
                        sample_count=args.kronos_paths,
                        point_in_time_status=HISTORICAL_COUNTERFACTUAL)
                persisted = repository.save_experimental_output(
                    result, args.revision, args.instrument_id) if result.status == "COMPLETED" else False
                counts[name] += int(persisted)
                if result.status != "COMPLETED":
                    failures += 1
                outcomes.append({"referenceMinute": reference.isoformat(), "model": name,
                                 "status": result.status, "persisted": persisted})
            except Exception as error:
                failures += 1
                outcomes.append({"referenceMinute": reference.isoformat(), "model": name,
                                 "status": "FAILED",
                                 "detail": f"{type(error).__name__}: {error}"})
    payload = {
        "revision": revision.id, "instrumentId": args.instrument_id,
        "from": from_date.isoformat(), "to": to_date.isoformat(),
        "strideMinutes": args.stride, "referenceCount": len(candidates),
        "kronosRequested": bool(args.include_kronos),
        "kronosServiceAvailable": kronos_available,
        "completedByModel": counts, "failureCount": failures,
        "results": outcomes[-args.max_output:],
        "researchOnly": True, "usedByPriceCoreV2": False,
    }
    _write_json(Path(args.output), payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if failures == 0 else 1


def _replay_manifest(args: argparse.Namespace) -> ReplayManifest:
    return ReplayManifest(
        dataset_revision_id=args.revision,
        instrument_id=args.instrument_id,
        feature_set_version=args.feature_set_version,
        test_date=date.fromisoformat(args.test_date),
        training_mode=args.training_mode,
        rolling_days=args.rolling_days,
        purge_minutes=args.purge_minutes,
        baseline_versions=("majority-v1", "persistence-v1"),
        persistence_return_minutes=args.persistence_return_minutes,
        persistence_threshold=Decimal(args.persistence_threshold),
    )


def command_run_walk_forward(args: argparse.Namespace) -> int:
    settings = Settings.from_env(require_database=True)
    repository = PostgresRepository(settings.database_url, ROOT / "migrations")
    run_id, report = WalkForwardReplay(repository).run(_replay_manifest(args))
    _write_json(Path(args.output), report)
    print(json.dumps({"run_id": run_id, "report": args.output}, indent=2))
    return 0


def command_show_run_report(args: argparse.Namespace) -> int:
    settings = Settings.from_env(require_database=True)
    report = PostgresRepository(
        settings.database_url, ROOT / "migrations"
    ).run_report(args.run_id)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def _expand_phase45_prices(specification: dict) -> list[tuple[TradingSession, list[Candle]]]:
    result: list[tuple[TradingSession, list[Candle]]] = []
    open_hour, open_minute = map(int, specification["session_open_ist"].split(":"))
    expected_minutes = int(specification["session_minutes"])
    for day_spec in specification["sessions"]:
        session_date = date.fromisoformat(day_spec["date"])
        opened = datetime.combine(
            session_date, time(open_hour, open_minute), IST
        ).astimezone(timezone.utc)
        session = TradingSession(
            session_date,
            opened,
            opened + timedelta(minutes=expected_minutes),
            day_spec.get("status", "SPECIAL"),
        )
        if session.status == "CLOSED":
            result.append((session, []))
            continue
        close = Decimal(day_spec["start_close"])
        closes = [close]
        for segment in day_spec["segments"]:
            increment = Decimal(segment["per_minute"])
            for _ in range(int(segment["increments"])):
                close += increment
                closes.append(close)
        if len(closes) != expected_minutes:
            raise ValueError(f"fixture {session_date} does not contain {expected_minutes} closes")
        candles = [
            Candle(
                opened + timedelta(minutes=minute),
                value,
                value,
                value,
                value,
                1000 + minute,
                500 + minute,
            )
            for minute, value in enumerate(closes)
        ]
        result.append((session, candles))
    return result


def build_phase45_fixture_repository(
    fixture_dir: Path | None = None,
) -> tuple[MemoryRepository, str, int]:
    fixture_dir = fixture_dir or ROOT / "fixtures"
    specification = json.loads(
        (fixture_dir / "phase45_market.json").read_text(encoding="utf-8")
    )
    fixed_clock = lambda: datetime(2024, 7, 25, 12, 0, tzinfo=timezone.utc)
    repository = MemoryRepository(clock=fixed_clock)
    client = FixtureZerodhaClient(fixture_dir)
    _, instruments, mapping = InstrumentSnapshotService(
        ReadOnlyZerodhaAdapter(client), repository
    ).capture(datetime(2024, 7, 22, 2, 0, tzinfo=timezone.utc))
    index = next(
        item for item in instruments
        if item.instrument_token == int(specification["instrument_token"])
    )
    instrument_id = mapping[index.instrument_token]
    expanded = _expand_phase45_prices(specification)
    for session, _ in expanded:
        repository.store_session(session)
    first_session = expanded[0][0]
    last_session = expanded[-1][0]
    job = repository.get_or_create_job(
        instrument_id,
        first_session.open_at,
        last_session.close_at - timedelta(minutes=1),
    )
    repository.mark_job_running(job.id)
    candles = [candle for _, rows in expanded for candle in rows]
    inserted, _ = repository.insert_candles(instrument_id, job.id, candles)
    repository.update_checkpoint(
        job.id, last_session.close_at - timedelta(minutes=1), len(candles), inserted
    )
    repository.complete_job(job.id)
    dataset_service = DatasetValidationService(
        repository,
        clock=fixed_clock,
        revision_id_factory=lambda: str(uuid5(NAMESPACE_URL, "banknifty-fixture-phase4-5")),
    )
    candidate, _ = dataset_service.create_candidate(
        [instrument_id], first_session.session_date, last_session.session_date
    )
    accepted = dataset_service.decide(
        candidate.id, "ACCEPTED", "Phase 4/5 multi-session fixture passed"
    )
    settings = Settings.from_env(require_database=False)
    LabelGenerationService(
        repository, settings.threshold_policy_version, settings.label_thresholds()
    ).generate(accepted.id, instrument_id)
    FeatureGenerationService(repository).generate(accepted.id, instrument_id)
    return repository, accepted.id, instrument_id


def run_phase45_fixture(
    output: Path,
    fixture_dir: Path | None = None,
) -> dict:
    repository, revision_id, instrument_id = build_phase45_fixture_repository(fixture_dir)
    manifest = ReplayManifest(
        revision_id,
        instrument_id,
        FEATURE_SET_VERSION,
        date(2024, 7, 24),
        "EXPANDING",
        None,
        20,
        ("majority-v1", "persistence-v1"),
        5,
        Decimal("0.001"),
    )
    run_id, report = WalkForwardReplay(repository).run(manifest)
    _, second_report = WalkForwardReplay(repository).run(manifest)
    features = repository.feature_rows(revision_id, instrument_id, FEATURE_SET_VERSION)
    predictions = repository.predictions_for_run(run_id)
    ten_am = datetime.combine(date(2024, 7, 24), time(10, 0), IST).astimezone(timezone.utc)
    sample_prediction = next(
        item for item in predictions
        if item.as_of == ten_am
        and item.horizon_minutes == 5
        and item.baseline_version.startswith("persistence")
    )
    sample_feature = next(item for item in features if item.as_of == ten_am)
    payload = {
        "mode": "OFFLINE_FIXTURE_ONLY",
        "zerodha_network_calls": 0,
        "dataset_revision_id": revision_id,
        "feature_set_version": FEATURE_SET_VERSION,
        "feature_rows": len(features),
        "sample_feature_row": sample_feature.to_dict(),
        "sample_10_00_prediction": sample_prediction.to_dict(),
        "deterministic_rerun_identical": report == second_report,
        "walk_forward_report": report,
    }
    _write_json(output, payload)
    return payload


def command_phase45_fixture(args: argparse.Namespace) -> int:
    payload = run_phase45_fixture(
        Path(args.output), Path(args.fixtures) if args.fixtures else None
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def run_phase07_fixture(
    output: Path, artifact_dir: Path, fixture_dir: Path | None = None
) -> dict:
    """Explicit synthetic verification workflow; production commands never call this."""
    repository, revision_id, instrument_id = build_phase45_fixture_repository(
        fixture_dir
    )
    report = DailyMLWalkForward(
        repository, ModelArtifactStore(artifact_dir)
    ).run(
        revision_id,
        instrument_id,
        DailyWalkForwardConfig(
            test_dates=(date(2024, 7, 23), date(2024, 7, 24)),
            minimum_training_days=3,
            validation_days=1,
            calibration_fit_days=1,
            calibration_selection_days=1,
            purge_minutes=20,
            seed=1729,
            threads=2,
        ),
    )
    payload = {
        **report,
        "mode": "SYNTHETIC_FIXTURE_VERIFICATION_ONLY",
        "zerodha_network_calls": 0,
    }
    payload["report_checksum"] = digest(
        {key: value for key, value in payload.items() if key != "report_checksum"}
    )
    _write_json(output, payload)
    return payload


def command_phase07_fixture(args: argparse.Namespace) -> int:
    payload = run_phase07_fixture(
        Path(args.output),
        Path(args.artifact_dir),
        Path(args.fixtures) if args.fixtures else None,
    )
    print(json.dumps({
        "report": args.output,
        "report_checksum": payload["report_checksum"],
        "bundle_checksums": [
            item["bundle"]["bundle_checksum"] for item in payload["folds"]
        ],
        "prediction_count": payload["prediction_count"],
    }, indent=2, sort_keys=True))
    return 0


def command_audit_phase05(args: argparse.Namespace) -> int:
    import ast

    prohibited_names = {".venv", "venv", "build", "dist"}
    prohibited = [
        str(path.relative_to(ROOT))
        for path in ROOT.rglob("*")
        if path.name in prohibited_names
    ]
    transient_cache_names = {"__pycache__", ".pytest_cache"}
    transient_caches = [
        str(path.relative_to(ROOT))
        for path in ROOT.rglob("*")
        if path.name in transient_cache_names or path.name.endswith(".egg-info")
    ]
    forbidden = {
        "orders", "place_order", "modify_order", "cancel_order", "positions",
        "holdings", "margins", "place_gtt", "modify_gtt", "delete_gtt",
    }
    forbidden_calls: list[str] = []
    for path in (ROOT / "src" / "banknifty_foundation").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in forbidden
            ):
                forbidden_calls.append(f"{path.name}:{node.lineno}:{node.func.attr}")
    required_migrations = [
        ROOT / "migrations" / f"{number:03d}_{name}.sql"
        for number, name in (
            (1, "phase_0_1"), (2, "accepted_datasets"), (3, "target_labels"),
            (4, "point_in_time_features"), (5, "walk_forward_replay"),
        )
    ]
    checks = {
        "no_generated_environments": not prohibited,
        "read_only_adapter_capabilities": (
            ReadOnlyZerodhaAdapter.SAFE_PUBLIC_METHODS
            == frozenset({"instruments", "historical_data"})
        ),
        "no_forbidden_broker_calls": not forbidden_calls,
        "phase0_5_migrations_present": all(path.is_file() for path in required_migrations),
        "explicit_calendar_only": "return only an explicitly stored session"
        in (ROOT / "src" / "banknifty_foundation" / "calendar.py")
        .read_text(encoding="utf-8")
        .lower(),
        "no_forward_fill_in_feature_definition": (
            FeatureGenerationService.__module__ == "banknifty_foundation.features"
            and "UNAVAILABLE_NO_FILL"
            in (ROOT / "src" / "banknifty_foundation" / "features.py")
            .read_text(encoding="utf-8")
        ),
    }
    payload = {
        "schema_version": "phase-0-5-audit-v1",
        "status": "PASS" if all(checks.values()) else "FAIL",
        "evidence": "docs/PHASE_0_5_AUDIT.md",
        "computed_checks": checks,
        "generated_paths": prohibited,
        "transient_cache_warnings": transient_caches,
        "forbidden_broker_calls": forbidden_calls,
        "phase_boundaries": [0, 1, 2, 3, 4, 5],
    }
    _write_json(Path(args.output), payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["status"] == "PASS" else 1


def _read_report(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def command_simulate_live_feed(args: argparse.Namespace) -> int:
    report = write_phase8_report(Path(args.output))
    print(json.dumps({
        "report": args.output,
        "deterministic_checksum": report["deterministic_checksum"],
        "tick_count": report["tick_count"],
        "minutes_finalized": report["minutes_finalized"],
        "option_snapshots": report["option_snapshots"],
    }, indent=2, sort_keys=True))
    return 0


def _live_report_section(args: argparse.Namespace, key: str | None = None) -> int:
    report = _read_report(args.report)
    value = report if key is None else report[key]
    print(json.dumps(value, indent=2, sort_keys=True))
    return 0


def command_inspect_live_status(args: argparse.Namespace) -> int:
    report = _read_report(args.report)
    print(json.dumps({key: report[key] for key in (
        "mode", "simulated_connection_count", "reconnect_count", "gap_count",
        "resolved_gap_count", "tick_count", "minutes_finalized", "quality_issues",
    )}, indent=2, sort_keys=True))
    return 0


def command_show_subscriptions(args: argparse.Namespace) -> int:
    return _live_report_section(args, "subscription_operations")


def command_show_live_gaps(args: argparse.Namespace) -> int:
    report = _read_report(args.report)
    print(json.dumps({"gap_count": report["gap_count"], "resolved_gap_count": report["resolved_gap_count"], "quality_issues": report["quality_issues"]}, indent=2, sort_keys=True))
    return 0


def command_show_option_universe(args: argparse.Namespace) -> int:
    return _live_report_section(args, "atm_history")


def command_show_option_snapshots(args: argparse.Namespace) -> int:
    report = _read_report(args.report)
    print(json.dumps({"count": report["option_snapshots"], "depth_levels": report["full_depth_levels"], "oi_delta_examples": report["oi_delta_examples"], "volume_delta_examples": report["volume_delta_examples"]}, indent=2, sort_keys=True))
    return 0


def command_show_futures_selection(args: argparse.Namespace) -> int:
    return _live_report_section(args, "selected_futures")


def command_validate_live_session(args: argparse.Namespace) -> int:
    report = _read_report(args.report)
    checks = {
        "simulated_no_network": report["mode"] == "DETERMINISTIC_SIMULATED_LIVE_FEED_NO_NETWORK",
        "gap_resolved": report["gap_count"] == report["resolved_gap_count"] == 1,
        "atm_rotated": len(report["atm_history"]) >= 2,
        "small_universe": report["option_universe_size"] <= 22,
        "five_level_depth": report["full_depth_levels"] == 5,
        "timestamps_preserved": report["event_and_receive_time_preserved"],
        "no_predictions": report["no_predictions_created"],
    }
    payload = {"status": "PASS" if all(checks.values()) else "FAIL", "checks": checks}
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["status"] == "PASS" else 1


def command_raw_tick_retention(args: argparse.Namespace) -> int:
    settings = Settings.from_env(require_database=True)
    repository = PostgresLiveRepository(settings.database_url)
    before = datetime.fromisoformat(args.before).astimezone(timezone.utc)
    result = repository.raw_tick_retention(before, execute=args.execute)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _load_live_snapshot(path: str) -> list[LiveInstrument]:
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    return [LiveInstrument(
        int(row["instrument_id"]), int(row["instrument_token"]), row["snapshot_version"],
        row["tradingsymbol"], row["exchange"], row["instrument_type"],
        date.fromisoformat(row["expiry"]) if row.get("expiry") else None,
        Decimal(str(row["strike"])) if row.get("strike") is not None else None,
    ) for row in rows]


def command_collect_live(args: argparse.Namespace) -> int:
    """Explicit terminal entry point for the same durable worker used by the API."""
    if not args.confirm_real:
        raise RuntimeError("collect-live requires --confirm-real")
    from dataclasses import replace
    from .live_market_worker import LiveMarketWorker
    settings=Settings.from_env(require_database=True)
    settings.require_live_credentials()
    worker=LiveMarketWorker(replace(settings,live_market_enabled=True))
    stopping=threading.Event()
    signal.signal(signal.SIGINT,lambda *_:stopping.set())
    if hasattr(signal,'SIGTERM'): signal.signal(signal.SIGTERM,lambda *_:stopping.set())
    worker.start()
    try:
        while not stopping.wait(1):
            if not worker.thread or not worker.thread.is_alive():
                raise RuntimeError(worker.status()['message'] or worker.status()['state'])
    finally:
        worker.stop()
    return 0


def command_compare_models(args: argparse.Namespace) -> int:
    report = _read_report(args.report)
    result = [
        {
            "test_dates": item["partitions"]["test_dates"],
            "horizon_minutes": item["horizon_minutes"],
            "majority": item["majority_validation_metrics"],
            "persistence": item["persistence_validation_metrics"],
            "logistic": item["logistic_validation_metrics"],
            "lightgbm": item["lightgbm_validation_metrics"],
            "selected": item["selected_algorithm"],
            "reason": item["selection_reason"],
        }
        for item in report["training"]
    ]
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def command_inspect_model(args: argparse.Namespace) -> int:
    store = ModelArtifactStore(Path(args.artifact_dir))
    model = store.load(args.manifest)
    print(json.dumps(model.metadata(), indent=2, sort_keys=True))
    return 0


def command_inspect_calibration(args: argparse.Namespace) -> int:
    report = _read_report(args.report)
    item = next(
        row for row in report["calibration"]
        if int(row["horizon_minutes"]) == args.horizon
        and (
            args.test_date is None
            or row["selection_range"]["to"][:10] < args.test_date
        )
    )
    print(json.dumps(item, indent=2, sort_keys=True))
    return 0


def run_phase_2_3_fixture(
    quality_output: Path,
    labels_output: Path,
    fixture_dir: Path | None = None,
) -> tuple[dict, dict]:
    fixture_dir = fixture_dir or ROOT / "fixtures"
    fixed_clock = lambda: datetime(2024, 7, 25, 12, 0, tzinfo=timezone.utc)
    repository = MemoryRepository(clock=fixed_clock)
    client = FixtureZerodhaClient(fixture_dir)
    adapter = ReadOnlyZerodhaAdapter(client)
    _, instruments, mapping = InstrumentSnapshotService(adapter, repository).capture(
        datetime(2024, 7, 25, 2, 59, tzinfo=timezone.utc)
    )
    index = next(item for item in instruments if item.instrument_token == 260105)
    instrument_id = mapping[index.instrument_token]

    session_date = date(2024, 7, 25)
    session = TradingSession(
        session_date,
        datetime.combine(session_date, time(9, 15), IST).astimezone(timezone.utc),
        datetime.combine(session_date, time(9, 36), IST).astimezone(timezone.utc),
        "SPECIAL",
    )
    repository.store_session(session)
    job = repository.get_or_create_job(
        instrument_id, session.open_at, session.close_at - timedelta(minutes=1)
    )
    repository.mark_job_running(job.id)
    prices = json.loads((fixture_dir / "label_prices.json").read_text(encoding="utf-8"))
    candles = [
        Candle(
            session.open_at + timedelta(minutes=int(item["minute"])),
            Decimal(item["close"]),
            Decimal(item["close"]),
            Decimal(item["close"]),
            Decimal(item["close"]),
            1000 + int(item["minute"]),
            500 + int(item["minute"]),
        )
        for item in prices
    ]
    inserted, _ = repository.insert_candles(instrument_id, job.id, candles)
    repository.update_checkpoint(
        job.id, session.close_at - timedelta(minutes=1), len(candles), inserted
    )
    repository.complete_job(job.id)

    service = DatasetValidationService(
        repository,
        clock=fixed_clock,
        revision_id_factory=lambda: str(uuid5(NAMESPACE_URL, "banknifty-fixture-phase2-3")),
    )
    revision, quality = service.create_candidate([instrument_id], session_date, session_date)
    accepted = service.decide(revision.id, "ACCEPTED", "offline hand-calculated fixture passed")
    quality["revision"] = accepted.to_dict()
    _write_json(quality_output, quality)

    settings = Settings.from_env(require_database=False)
    generated, labels_inserted = LabelGenerationService(
        repository, settings.threshold_policy_version, settings.label_thresholds()
    ).generate(accepted.id, instrument_id)
    sample = [
        item.to_dict()
        for item in generated
        if item.as_of == session.open_at and item.horizon_minutes in {5, 10, 15, 20}
    ]
    label_payload = {
        "schema_version": "target-labels-v1",
        "revision_id": accepted.id,
        "generated": len(generated),
        "inserted": labels_inserted,
        "sample_as_of": session.open_at.isoformat(),
        "sample_labels_all_horizons": sample,
        "zerodha_network_calls": 0,
    }
    _write_json(labels_output, label_payload)
    return quality, label_payload


def command_phase_2_3_fixture(args: argparse.Namespace) -> int:
    quality, labels = run_phase_2_3_fixture(
        Path(args.quality_output),
        Path(args.labels_output),
        Path(args.fixtures) if args.fixtures else None,
    )
    print(json.dumps({"quality": quality, "labels": labels}, indent=2, sort_keys=True))
    return 0


def command_import_history(args: argparse.Namespace) -> int:
    if not args.confirm_read_only:
        raise RuntimeError("--confirm-read-only is required")
    settings = Settings.from_env(require_database=True)
    api_key, access_token = settings.require_live_credentials()
    repository = PostgresRepository(settings.database_url, ROOT / "migrations")
    adapter = create_read_only_zerodha_adapter(
        api_key,
        access_token,
        allow_real_api=settings.allow_real_zerodha,
    )
    captured_at = datetime.now(timezone.utc)
    _, instruments, mapping = InstrumentSnapshotService(adapter, repository).capture(captured_at)
    requested_tokens = set(args.instrument_token)
    selected = [item for item in instruments if item.instrument_token in requested_tokens]
    missing = requested_tokens - {item.instrument_token for item in selected}
    if missing:
        raise ValueError(f"Requested tokens are absent or are not BANKNIFTY index/futures: {sorted(missing)}")

    start_date = date.fromisoformat(args.from_date)
    end_date = date.fromisoformat(args.to_date)
    stored_sessions = repository.sessions_between(start_date, end_date)
    calendar = TradingCalendar(stored_sessions)
    summaries = []
    sessions = calendar.sessions_between(start_date, end_date)
    if not sessions:
        raise ValueError("No explicitly stored OPEN or SPECIAL sessions exist in the requested date range")
    start = sessions[0].open_at
    end = sessions[-1].close_at - timedelta(minutes=1)
    for instrument in selected:
        result = HistoricalImporter(
            adapter,
            repository,
            calendar,
            rate_limiter=RateLimiter(settings.requests_per_second),
            chunk_days=settings.chunk_days,
            max_retries=settings.max_retries,
        ).run(
            instrument_id=mapping[instrument.instrument_token],
            instrument_token=instrument.instrument_token,
            start=start,
            end=end,
        )
        summaries.append(
            {
                "instrument_token": instrument.instrument_token,
                "symbol": instrument.tradingsymbol,
                "job_id": result.job_id,
                "status": result.status,
                "rows_inserted": result.rows_inserted,
            }
        )
    print(json.dumps({"mode": "APPROVED_READ_ONLY_REAL_IMPORT", "imports": summaries}, indent=2))
    return 0


def command_service(args: argparse.Namespace) -> int:
    """Run the private HTTP API used by the Symfony application."""
    import uvicorn

    uvicorn.run(
        "banknifty_foundation.internal_api:app",
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        access_log=True,
    )
    return 0


def _accepted_ml_command(args: argparse.Namespace, operation: str) -> int:
    settings = Settings.from_env(require_database=True)
    repository = PostgresRepository(settings.database_url, ROOT / "migrations")
    revision = repository.get_revision(args.revision)
    if revision.status != "ACCEPTED":
        raise ValueError("production ML commands require an ACCEPTED dataset revision")
    if not repository.feature_rows(
        args.revision, args.instrument_id, args.feature_set_version
    ):
        raise ValueError("accepted revision has no stored feature rows")
    if not repository.target_labels(args.revision, args.instrument_id):
        raise ValueError("accepted revision has no stored target labels")
    report = DailyMLWalkForward(
        repository, ModelArtifactStore(Path(args.artifact_dir))
    ).run(
        args.revision,
        args.instrument_id,
        DailyWalkForwardConfig(
            test_dates=tuple(date.fromisoformat(item) for item in args.test_date),
            minimum_training_days=args.minimum_training_days,
            validation_days=args.validation_days,
            calibration_fit_days=args.calibration_fit_days,
            calibration_selection_days=args.calibration_selection_days,
            purge_minutes=args.purge_minutes,
            seed=settings.ml_random_seed,
            threads=settings.ml_threads,
        ),
    )
    payload = {
        **report,
        "mode": "ACCEPTED_DATASET_POSTGRESQL",
        "operation": operation,
    }
    payload["report_checksum"] = digest(
        {key: value for key, value in payload.items() if key != "report_checksum"}
    )
    _write_json(Path(args.output), payload)
    print(
        json.dumps(
            {
                "operation": operation,
                "revision": args.revision,
                "folds": len(report["folds"]),
                "predictions": report["prediction_count"],
                "report": args.output,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def command_train_models(args: argparse.Namespace) -> int:
    return _accepted_ml_command(args, "TRAIN_AND_WALK_FORWARD")


def command_calibrate_models(args: argparse.Namespace) -> int:
    return _accepted_ml_command(args, "TRAIN_CALIBRATE_AND_WALK_FORWARD")


def command_model_walk_forward(args: argparse.Namespace) -> int:
    return _accepted_ml_command(args, "DAILY_MODEL_WALK_FORWARD")


def _add_accepted_ml_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--revision", required=True)
    parser.add_argument("--instrument-id", type=int, required=True)
    parser.add_argument("--test-date", action="append", required=True)
    parser.add_argument("--feature-set-version", default=FEATURE_SET_VERSION)
    parser.add_argument("--minimum-training-days", type=int, default=20)
    parser.add_argument("--validation-days", type=int, default=5)
    parser.add_argument("--calibration-fit-days", type=int, default=5)
    parser.add_argument("--calibration-selection-days", type=int, default=5)
    parser.add_argument("--purge-minutes", type=int, default=20)
    parser.add_argument("--artifact-dir", default="model_artifacts")
    parser.add_argument("--output", default="artifacts/accepted-ml-report.json")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BANKNIFTY Big Move V2, Market Memory, News and Live Market collection")
    sub = parser.add_subparsers(dest="command", required=True)
    context = sub.add_parser('enrich-research-context', help='Explicit research-only historical context retrieval; never trains or modifies V2')
    context.add_argument('--from', dest='from_date', type=date.fromisoformat)
    context.add_argument('--to', dest='to_date', type=date.fromisoformat)
    context.add_argument('--provider', choices=('auto', 'dhan', 'upstox'), default=None)
    context.add_argument('--missing-only', action='store_true', default=True,
                         help='Reuse checksum-verified cached responses (default; immutable caches are never overwritten)')
    context.set_defaults(func=command_enrich_research_context)
    structure = sub.add_parser('enrich-market-structure', help='Explicit historical constituent/futures context only')
    structure.add_argument('--from', dest='from_date', required=True, type=date.fromisoformat)
    structure.add_argument('--to', dest='to_date', required=True, type=date.fromisoformat)
    structure.set_defaults(func=command_enrich_market_structure)
    official = sub.add_parser('import-official-banknifty-constituents',
        help='Import one explicit local official Nifty Bank constituent CSV; never downloads or infers dates/weights')
    official.add_argument('--csv', required=True)
    official.add_argument('--effective-from', required=True)
    official.add_argument('--effective-to', required=True)
    official.add_argument('--source-observed-at', required=True)
    official.add_argument('--effective-range-basis', required=True)
    official.set_defaults(func=command_import_official_banknifty_constituents)
    historical_official = sub.add_parser('import-official-banknifty-history', help='Local official CSV/ZIP only; never downloads')
    for name in ('input','effective-from','effective-to','source-publication-date','source-observed-at',
                 'source-reference','source-url','effective-range-basis'):
        historical_official.add_argument('--'+name, required=True)
    historical_official.add_argument('--weight-unit', choices=('PERCENT','FRACTION','NONE'), required=True)
    historical_official.set_defaults(func=command_import_official_banknifty_history)
    context_plan = sub.add_parser('context-v3-plan', help='Deterministic read-only Context V3 generation plan')
    context_plan.add_argument('--revision-id', required=True)
    context_plan.add_argument('--instrument-id', required=True, type=int)
    context_plan.add_argument('--feature-profile', required=True)
    context_plan.add_argument('--output', required=True)
    context_plan.set_defaults(func=command_context_v3_plan)
    context_generate = sub.add_parser('generate-context-v3',
        help='Generate Context V3 rows only from an exact READY plan with revalidated immutable pins')
    context_generate.add_argument('--revision-id', required=True)
    context_generate.add_argument('--instrument-id', required=True, type=int)
    context_generate.add_argument('--feature-profile', required=True)
    context_generate.add_argument('--pins-file', required=True)
    context_generate.set_defaults(func=command_generate_context_v3)
    context_activate = sub.add_parser('context-v3-activate',
        help='Plan, revalidate and generate one or more Context V3 profiles with isolated outcomes')
    context_activate.add_argument('--revision-id', required=True)
    context_activate.add_argument('--instrument-id', required=True, type=int)
    context_activate.add_argument('--profiles', default='all',
        help="Comma-separated profile versions, or 'all'")
    context_activate.add_argument('--output-dir', required=True)
    context_activate.add_argument('--dry-run', action='store_true')
    context_activate.set_defaults(func=command_context_v3_activate)
    preflight = sub.add_parser('context-v3-history-preflight',
        help='Read-only historical Context V3 readiness and provider capability preflight')
    preflight.add_argument('--revision-id', required=True)
    preflight.add_argument('--instrument-id', required=True, type=int)
    preflight.add_argument('--from', dest='from_date', required=True, type=date.fromisoformat)
    preflight.add_argument('--to', dest='to_date', required=True, type=date.fromisoformat)
    preflight.add_argument('--profiles', default='all')
    preflight.add_argument('--allow-network', action='store_true',
        help='Explicitly permit one configured-provider capability probe; never implied')
    preflight.add_argument('--output', required=True)
    preflight.set_defaults(func=command_context_v3_history_preflight)
    prepare = sub.add_parser('prepare-context-v3-history',
        help='Preflight, validate pinned evidence and generate Context V3 for an explicit date range')
    prepare.add_argument('--revision-id', required=True)
    prepare.add_argument('--instrument-id', required=True, type=int)
    prepare.add_argument('--from', dest='from_date', required=True, type=date.fromisoformat)
    prepare.add_argument('--to', dest='to_date', required=True, type=date.fromisoformat)
    prepare.add_argument('--profiles', default='all')
    prepare.add_argument('--output-dir', required=True)
    prepare.add_argument('--dry-run', action='store_true')
    prepare.add_argument('--allow-network', action='store_true')
    prepare.set_defaults(func=command_prepare_context_v3_history)
    research_run=sub.add_parser('context-v3-research-run',help='Prepare, activate and evaluate independent research profiles')
    research_run.add_argument('--revision-id',required=True)
    research_run.add_argument('--instrument-id',required=True,type=int)
    research_run.add_argument('--from',dest='from_date',required=True,type=date.fromisoformat)
    research_run.add_argument('--to',dest='to_date',required=True,type=date.fromisoformat)
    research_run.add_argument('--profiles',default='all')
    research_run.add_argument('--algorithms',default='LOGISTIC_REGRESSION,LIGHTGBM')
    research_run.add_argument('--horizons',default='10,20,30')
    research_run.add_argument('--output-dir',required=True)
    research_run.add_argument('--allow-network',action='store_true')
    research_run.add_argument('--dry-run',action='store_true')
    research_run.set_defaults(func=command_context_v3_research_run)
    evaluate_context = sub.add_parser('evaluate-context-v3',
        help='Strictly paired chronological V2 versus V2 + Context V3 experiment; never promotes')
    evaluate_context.add_argument('--revision-id', required=True)
    evaluate_context.add_argument('--instrument-id', required=True, type=int)
    profile_choice=evaluate_context.add_mutually_exclusive_group(required=True)
    profile_choice.add_argument('--profile')
    profile_choice.add_argument('--profiles')
    evaluate_context.add_argument('--horizons', default='10,20,30')
    evaluate_context.add_argument('--evaluation-step-days', type=int, default=1)
    evaluate_context.add_argument('--from', dest='from_date', type=date.fromisoformat)
    evaluate_context.add_argument('--to', dest='to_date', type=date.fromisoformat)
    evaluate_context.add_argument('--minimum-training-days', type=int, default=5)
    evaluate_context.add_argument('--holdout-days', type=int, default=5)
    evaluate_context.add_argument('--algorithms', default='LOGISTIC_REGRESSION,LIGHTGBM')
    evaluate_context.add_argument('--output')
    evaluate_context.add_argument('--no-persist', action='store_true')
    evaluate_context.set_defaults(func=command_evaluate_context_v3)
    local_import = sub.add_parser('research-context-import-local',
        help='Import caller-supplied local historical Research Context observations without network access')
    local_import.add_argument('--input', required=True)
    local_import.add_argument('--instrument-id', required=True, type=int)
    local_import.add_argument('--source-name', required=True)
    local_import.add_argument('--source-observed-at', required=True)
    local_import.add_argument('--output')
    local_import.set_defaults(func=command_research_context_import_local)
    backfill = sub.add_parser('backfill-research-context',
        help='Chronological, restart-safe Research Context backfill/audit; never calls providers')
    backfill.add_argument('--revision-id')
    backfill.add_argument('--instrument-id', type=int)
    backfill.add_argument('--from', dest='from_date', type=date.fromisoformat)
    backfill.add_argument('--to', dest='to_date', type=date.fromisoformat)
    backfill.add_argument('--input-dir')
    backfill.add_argument('--source-name')
    backfill.add_argument('--source-observed-at')
    backfill.add_argument('--output')
    backfill.set_defaults(func=command_backfill_research_context)
    train_v2=sub.add_parser('train-big-move-v2',help='Train immutable 10/20/30m Big Move V2 artifacts from accepted data')
    train_v2.add_argument('--revision',required=True)
    train_v2.add_argument('--instrument-id',required=True,type=int)
    train_v2.add_argument('--algorithm',choices=['LOGISTIC_REGRESSION','LIGHTGBM'],default='LOGISTIC_REGRESSION')
    train_v2.add_argument('--artifact-dir',default=None)
    train_v2.set_defaults(func=command_train_big_move_v2)
    evaluate_v2=sub.add_parser('evaluate-big-move-v2',help='Expanding chronological V2 evidence and candidate comparison')
    approve_v2=sub.add_parser('approve-big-move-v2',help='Promote the exact candidate only through stored evidence gates')
    for command in (evaluate_v2,approve_v2):
        command.add_argument('--revision',required=True)
        command.add_argument('--instrument-id',required=True,type=int)
        command.add_argument('--artifact-dir',default=None)
    for command in (train_v2,evaluate_v2):
        command.add_argument('--minimum-training-days',type=int,default=5)
        command.add_argument('--evaluation-step-days',type=int,default=1)
    approve_v2.add_argument('--policy-file',help='JSON BigMoveApprovalPolicy; changed thresholds require a new version')
    evaluate_v2.set_defaults(func=command_evaluate_big_move_v2)
    approve_v2.set_defaults(func=command_approve_big_move_v2)
    enrich_parser=sub.add_parser('memory-enrich-intraday',help='enrich missing curated sessions using a read-only provider')
    enrich_parser.add_argument('--from',dest='from_date')
    enrich_parser.add_argument('--to',dest='to_date')
    enrich_parser.add_argument('--missing-only',action='store_true')
    enrich_parser.add_argument('--provider',choices=['stored','zerodha'],default='stored')
    enrich_parser.set_defaults(func=command_memory_enrich)
    memory_build = sub.add_parser("memory-build", help="rebuild derived Big Move V2 episodes; never writes curated Markdown")
    memory_build.add_argument("--directory", default="market_memory")
    memory_build.set_defaults(func=command_memory_build)
    memory_events = sub.add_parser("memory-build-events", help="rebuild structured V2 historical events and reactions")
    memory_events.add_argument("--directory", default="market_memory")
    memory_events.set_defaults(func=command_memory_events)
    memory_ingest = sub.add_parser("memory-ingest", help="idempotently index Market Memory in PostgreSQL")
    memory_ingest.add_argument("--directory", default="market_memory")
    memory_ingest.set_defaults(func=command_memory_ingest)
    news_refresh = sub.add_parser("news-refresh", help="fetch configured real news sources once")
    news_refresh.set_defaults(func=command_news_refresh)
    memory_finalize = sub.add_parser("memory-finalize-day", help="create a factual V2 daily rollup without overwriting curated memory")
    memory_finalize.add_argument("date")
    memory_finalize.add_argument("--directory", default="market_memory")
    memory_finalize.add_argument("--force", action="store_true")
    memory_finalize.set_defaults(func=command_memory_finalize_day)
    fixture = sub.add_parser("fixture-import", help="run a completely offline fixture import")
    fixture.add_argument("--fixtures", help="fixture directory (defaults to project fixtures)")
    fixture.add_argument("--output", default="artifacts/sample-coverage-report.json")
    fixture.set_defaults(func=command_fixture)
    migrate = sub.add_parser("migrate", help="apply PostgreSQL migrations")
    migrate.set_defaults(func=command_migrate)
    calendar_load = sub.add_parser("calendar-load", help="load an explicit trading-session JSON calendar")
    calendar_load.add_argument("--file", required=True)
    calendar_load.set_defaults(func=command_calendar_load)
    validate = sub.add_parser("dataset-validate", help="validate imports and create a CANDIDATE revision")
    validate.add_argument("--instrument-id", type=int, action="append", required=True)
    validate.add_argument("--from", dest="from_date", required=True)
    validate.add_argument("--to", dest="to_date", required=True)
    validate.add_argument("--output", default="artifacts/dataset-quality-report.json")
    validate.set_defaults(func=command_dataset_validate)
    decide = sub.add_parser("dataset-decide", help="explicitly accept or reject a CANDIDATE revision")
    decide.add_argument("--revision", required=True)
    decide.add_argument("--decision", choices=("ACCEPTED", "REJECTED"), required=True)
    decide.add_argument("--reason", required=True)
    decide.set_defaults(func=command_dataset_decide)
    labels = sub.add_parser("generate-labels", help="generate labels from an ACCEPTED revision")
    labels.add_argument("--revision", required=True)
    labels.add_argument("--instrument-id", type=int, required=True)
    labels.add_argument("--output", default="artifacts/target-labels.json")
    labels.set_defaults(func=command_generate_labels)
    features = sub.add_parser("generate-features", help="generate immutable point-in-time features")
    features.add_argument("--revision", required=True)
    features.add_argument("--instrument-id", type=int, required=True)
    features.add_argument("--feature-set-version", default=FEATURE_SET_VERSION)
    features.add_argument("--output", default="artifacts/features.json")
    features.set_defaults(func=command_generate_features)
    big_labels = sub.add_parser("generate-big-move-labels", help="generate immutable 10/20/30-minute path labels")
    big_labels.add_argument("--revision", required=True)
    big_labels.add_argument("--instrument-id", type=int, required=True)
    big_labels.add_argument("--output", default="artifacts/big-move-labels.json")
    big_labels.set_defaults(func=command_generate_big_move_labels)
    big_features = sub.add_parser("generate-big-move-features", help="generate immutable banknifty-bigmove-v2 features")
    big_features.add_argument("--revision", required=True)
    big_features.add_argument("--instrument-id", type=int, required=True)
    big_features.add_argument("--output", default="artifacts/big-move-features.json")
    big_features.set_defaults(func=command_generate_big_move_features)
    kronos = sub.add_parser("kronos-forecast-experiment", help="inspect optional Kronos experiment readiness")
    kronos.add_argument("--as-of", required=True, help="aware ISO-8601 research cutoff")
    kronos.add_argument("--lookback", type=int, default=400)
    kronos.add_argument("--model", choices=("Kronos-mini", "Kronos-small"), default="Kronos-mini")
    kronos.add_argument("--output", default="artifacts/kronos-experimental-output.json")
    kronos.set_defaults(func=command_kronos_experiment)

    external_preflight = sub.add_parser("external-research-preflight",
        help="check vendored Kronos/India Trader sources and local model cache without downloading")
    external_preflight.add_argument("--models-dir")
    external_preflight.add_argument("--service-url")
    external_preflight.add_argument("--output", default="artifacts/external-research-preflight.json")
    external_preflight.set_defaults(func=command_external_research_preflight)

    kronos_run = sub.add_parser("kronos-research-run",
        help="run one local Kronos research forecast and persist accepted output")
    kronos_run.add_argument("--revision", required=True)
    kronos_run.add_argument("--instrument-id", type=int, required=True)
    kronos_run.add_argument("--as-of", required=True, help="aware ISO-8601 reference minute")
    kronos_run.add_argument("--lookback", type=int, default=400)
    kronos_run.add_argument("--paths", type=int, default=8)
    kronos_run.add_argument("--seed", type=int, default=1729)
    kronos_run.add_argument("--timeout", type=float, default=90.0)
    kronos_run.add_argument("--service-url")
    kronos_run.add_argument("--prospective", action="store_true",
        help="mark decision-available prospective research instead of historical counterfactual")
    kronos_run.add_argument("--output", default="artifacts/kronos-research-output.json")
    kronos_run.set_defaults(func=command_kronos_research_run)

    time_machine_run = sub.add_parser("time-machine-run",
        help="run one BANKNIFTY time-of-day Monte Carlo research forecast")
    time_machine_run.add_argument("--revision", required=True)
    time_machine_run.add_argument("--instrument-id", type=int, required=True)
    time_machine_run.add_argument("--as-of", required=True, help="aware ISO-8601 reference minute")
    time_machine_run.add_argument("--paths", type=int, default=10_000)
    time_machine_run.add_argument("--seed", type=int, default=1729)
    time_machine_run.add_argument("--slot-window", type=int, default=2)
    time_machine_run.add_argument("--min-prior-sessions", type=int, default=20)
    time_machine_run.add_argument("--prospective", action="store_true",
        help="mark decision-available prospective research instead of historical counterfactual")
    time_machine_run.add_argument("--output", default="artifacts/time-machine-output.json")
    time_machine_run.set_defaults(func=command_time_machine_run)

    backfill_external = sub.add_parser("external-research-backfill",
        help="bounded deterministic historical research backfill; never samples future data")
    backfill_external.add_argument("--revision", required=True)
    backfill_external.add_argument("--instrument-id", type=int, required=True)
    backfill_external.add_argument("--from", dest="from_date", required=True)
    backfill_external.add_argument("--to", dest="to_date", required=True)
    backfill_external.add_argument("--stride", type=int, default=30)
    backfill_external.add_argument("--max-references", type=int, default=50)
    backfill_external.add_argument("--paths", type=int, default=10_000)
    backfill_external.add_argument("--seed", type=int, default=1729)
    backfill_external.add_argument("--slot-window", type=int, default=2)
    backfill_external.add_argument("--min-prior-sessions", type=int, default=20)
    backfill_external.add_argument("--include-kronos", action="store_true")
    backfill_external.add_argument("--kronos-paths", type=int, default=8)
    backfill_external.add_argument("--service-url")
    backfill_external.add_argument("--max-output", type=int, default=200)
    backfill_external.add_argument("--output", default="artifacts/external-research-backfill.json")
    backfill_external.set_defaults(func=command_external_research_backfill)

    replay = sub.add_parser("run-walk-forward", help="run deterministic chronological replay")
    replay.add_argument("--revision", required=True)
    replay.add_argument("--instrument-id", type=int, required=True)
    replay.add_argument("--feature-set-version", default=FEATURE_SET_VERSION)
    replay.add_argument("--test-date", required=True)
    replay.add_argument("--training-mode", choices=("EXPANDING", "ROLLING"), default="EXPANDING")
    replay.add_argument("--rolling-days", type=int)
    replay.add_argument("--purge-minutes", type=int, default=20)
    replay.add_argument("--persistence-return-minutes", type=int, default=5)
    replay.add_argument("--persistence-threshold", default="0.001")
    replay.add_argument("--output", default="artifacts/walk-forward-report.json")
    replay.set_defaults(func=command_run_walk_forward)
    show_report = sub.add_parser("show-run-report", help="print a stored replay report")
    show_report.add_argument("--run-id", required=True)
    show_report.set_defaults(func=command_show_run_report)
    phase45 = sub.add_parser("fixture-phase4-5", help="LEGACY research fixture workflow")
    phase45.add_argument("--fixtures")
    phase45.add_argument("--output", default="artifacts/sample-phase4-5-report.json")
    phase45.set_defaults(func=command_phase45_fixture)
    phase_fixture = sub.add_parser("fixture-phase2-3", help="LEGACY accepted-dataset and label fixture workflow")
    phase_fixture.add_argument("--fixtures")
    phase_fixture.add_argument(
        "--quality-output", default="artifacts/sample-dataset-quality-report.json"
    )
    phase_fixture.add_argument("--labels-output", default="artifacts/sample-target-labels.json")
    phase_fixture.set_defaults(func=command_phase_2_3_fixture)
    audit = sub.add_parser("audit-phase0-5", help="LEGACY research audit tool")
    audit.add_argument("--output", default="artifacts/phase-0-5-audit.json")
    audit.set_defaults(func=command_audit_phase05)
    phase07 = sub.add_parser("fixture-phase0-7", help="LEGACY deterministic research fixture workflow")
    phase07.add_argument("--fixtures")
    phase07.add_argument("--artifact-dir", default="model_artifacts")
    phase07.add_argument("--output", default="artifacts/phase-0-7-final-report.json")
    phase07.set_defaults(func=command_phase07_fixture)
    train_models = sub.add_parser(
        "train-models",
        help="train from an ACCEPTED PostgreSQL dataset revision",
    )
    _add_accepted_ml_arguments(train_models)
    train_models.set_defaults(func=command_train_models)
    compare_models = sub.add_parser("compare-models", help="show baseline/logistic/LightGBM comparisons")
    compare_models.add_argument("--report", default="artifacts/phase-0-7-final-report.json")
    compare_models.set_defaults(func=command_compare_models)
    inspect_model = sub.add_parser("inspect-model", help="verify and inspect a trusted local model manifest")
    inspect_model.add_argument("--artifact-dir", default="model_artifacts")
    inspect_model.add_argument("--manifest", required=True)
    inspect_model.set_defaults(func=command_inspect_model)
    calibrate_models = sub.add_parser(
        "calibrate-models",
        help="fit/select calibrators from an ACCEPTED PostgreSQL dataset revision",
    )
    _add_accepted_ml_arguments(calibrate_models)
    calibrate_models.set_defaults(func=command_calibrate_models)
    inspect_calibration = sub.add_parser("inspect-calibration", help="inspect one horizon calibration")
    inspect_calibration.add_argument("--report", default="artifacts/phase-0-7-final-report.json")
    inspect_calibration.add_argument("--horizon", type=int, choices=(5, 10, 15, 20), required=True)
    inspect_calibration.add_argument("--test-date")
    inspect_calibration.set_defaults(func=command_inspect_calibration)
    model_walk = sub.add_parser(
        "run-model-walk-forward",
        help="run daily walk-forward against an ACCEPTED PostgreSQL dataset revision",
    )
    _add_accepted_ml_arguments(model_walk)
    model_walk.set_defaults(func=command_model_walk_forward)
    history = sub.add_parser("import-history", help="guarded real read-only import; never use without approval")
    history.add_argument("--from", dest="from_date", required=True, help="first session date (YYYY-MM-DD)")
    history.add_argument("--to", dest="to_date", required=True, help="last session date (YYYY-MM-DD)")
    history.add_argument("--instrument-token", type=int, action="append", required=True)
    history.add_argument("--confirm-read-only", action="store_true")
    history.set_defaults(func=command_import_history)
    simulated = sub.add_parser("simulate-live-feed", help="LEGACY developer tool: offline SIMULATION")
    simulated.add_argument("--output", default="artifacts/phase-8-live-data-report.json")
    simulated.set_defaults(func=command_simulate_live_feed)
    for name, help_text, handler in (
        ("inspect-live-status", "inspect connection and quality status", command_inspect_live_status),
        ("show-subscriptions", "show subscription changes", command_show_subscriptions),
        ("show-live-gaps", "show gap-journal summary", command_show_live_gaps),
        ("show-option-universe", "show point-in-time ATM universe history", command_show_option_universe),
        ("show-option-snapshots", "show option snapshot summary", command_show_option_snapshots),
        ("show-futures-selection", "show exact futures selection", command_show_futures_selection),
        ("validate-live-session", "LEGACY developer tool: inspect a simulation report", command_validate_live_session),
    ):
        item = sub.add_parser(name, help=help_text)
        item.add_argument("--report", default="artifacts/phase-8-live-data-report.json")
        item.set_defaults(func=handler)
    retention = sub.add_parser("raw-tick-retention", help="dry-run or execute verified raw-tick retention")
    retention.add_argument("--before", required=True, help="aware ISO-8601 cutoff")
    retention.add_argument("--execute", action="store_true", help="delete only fully verified ranges")
    retention.set_defaults(func=command_raw_tick_retention)
    live = sub.add_parser("collect-live", help="guarded durable live worker using stored sessions and automatic instrument selection")
    live.add_argument("--confirm-real", action="store_true")
    live.set_defaults(func=command_collect_live)
    service = sub.add_parser("service", help="run the private product API for Symfony")
    service.add_argument("--host", default="0.0.0.0")
    service.add_argument("--port", type=int, default=8000)
    service.add_argument("--log-level", choices=("critical", "error", "warning", "info", "debug"), default="info")
    service.set_defaults(func=command_service)
    return parser


def command_train_big_move_v2(args):
    from .big_move_runtime import BigMoveArtifactStore, BigMoveTrainingService
    from .big_move_evaluation import EvaluationConfiguration
    settings=Settings.from_env(require_database=True)
    service=BigMoveTrainingService(PostgresRepository(settings.database_url),BigMoveArtifactStore(args.artifact_dir))
    config=EvaluationConfiguration(minimumTrainingDays=args.minimum_training_days,evaluationStepDays=args.evaluation_step_days)
    print(json.dumps(service.train(args.revision,args.instrument_id,args.algorithm,config),indent=2,default=str))
    return 0


def command_evaluate_big_move_v2(args):
    from .big_move_evaluation import BigMoveEvaluationService, EvaluationConfiguration
    from .big_move_runtime import BigMoveArtifactStore
    settings=Settings.from_env(require_database=True)
    config=EvaluationConfiguration(minimumTrainingDays=args.minimum_training_days,evaluationStepDays=args.evaluation_step_days)
    service=BigMoveEvaluationService(PostgresRepository(settings.database_url),BigMoveArtifactStore(args.artifact_dir),config)
    print(json.dumps(service.evaluate(args.revision,args.instrument_id),indent=2,default=str))
    return 0


def command_approve_big_move_v2(args):
    from .big_move_evaluation import BigMoveApprovalService, BigMoveApprovalPolicy
    from .big_move_runtime import BigMoveArtifactStore
    settings=Settings.from_env(require_database=True)
    policy=BigMoveApprovalPolicy(**json.loads(Path(args.policy_file).read_text(encoding='utf-8'))) if args.policy_file else BigMoveApprovalPolicy()
    service=BigMoveApprovalService(PostgresRepository(settings.database_url),BigMoveArtifactStore(args.artifact_dir),policy)
    print(json.dumps(service.approve(args.revision,args.instrument_id),indent=2,default=str))
    return 0


def command_memory_enrich(args):
    from .memory_enrichment import enrich
    print(json.dumps(enrich(from_date=args.from_date, to_date=args.to_date,
                           provider=args.provider, missing_only=args.missing_only), indent=2))
    return 0


def command_enrich_research_context(args):
    from .research_context import ResearchContextEnrichmentService
    result = ResearchContextEnrichmentService().enrich(args.from_date, args.to_date,
        provider=args.provider, missing_only=args.missing_only)
    print(json.dumps(result, indent=2))
    return 0 if result['coverage']['status'] == 'READY' else 2


def command_enrich_market_structure(args):
    from .market_structure_context import MarketStructureContextService
    if args.to_date < args.from_date or args.to_date >= datetime.now(IST).date():
        raise ValueError('Supply an ordered range of completed historical sessions')
    service=MarketStructureContextService()
    results=[]
    for i in range((args.to_date-args.from_date).days+1):
        results.append(service.enrich_day(args.from_date+timedelta(days=i)))
    print(json.dumps(results,indent=2))
    return 0 if all(r['status'] in ('PASS','SESSION_UNAVAILABLE') for r in results) else 2


def command_import_official_banknifty_constituents(args):
    from .market_structure_context import OfficialConstituentImportService
    basis=args.effective_range_basis.strip()
    if not basis: raise ValueError('--effective-range-basis is required')
    result=OfficialConstituentImportService().import_csv(Path(args.csv),effective_from=args.effective_from,
        effective_to=args.effective_to,source_observed_at=args.source_observed_at,
        source_url=OfficialConstituentImportService.SOURCE_URL,effective_range_basis=basis)
    print(json.dumps(result,indent=2))
    return 0


def command_import_official_banknifty_history(args):
    from .market_structure_context import OfficialConstituentImportService
    result=OfficialConstituentImportService().import_history(args.input,
        **{name:getattr(args,name) for name in ('effective_from','effective_to','source_publication_date',
            'source_observed_at','source_reference','source_url','effective_range_basis','weight_unit')})
    print(json.dumps(result,indent=2))
    return 0


def command_context_v3_plan(args):
    from .context_v3 import ContextV3PlanningService
    plan=ContextV3PlanningService(PostgresRepository(Settings.from_env().database_url)).plan(
        args.revision_id,args.instrument_id,args.feature_profile)
    _write_json(Path(args.output),plan)
    print(json.dumps({'status':plan['status'],'output':args.output,'blockerCount':len(plan['blockers'])},indent=2))
    return 0 if plan['status']=='READY' else 2


def command_generate_context_v3(args):
    from .context_v3 import ContextV3FeatureGenerationService, ContextV3PlanValidationService
    try:
        plan=json.loads(Path(args.pins_file).read_text(encoding='utf-8'))
    except (OSError,ValueError,TypeError):
        print(json.dumps({'status':'BLOCKED','error':'PINS_FILE_INVALID'},indent=2)); return 2
    repository=PostgresRepository(Settings.from_env().database_url)
    try:
        pins=ContextV3PlanValidationService(repository).validate(
            plan,args.revision_id,args.instrument_id,args.feature_profile)
    except (ValueError,TypeError) as exc:
        print(json.dumps({'status':'BLOCKED','error':str(exc)},indent=2)); return 2
    service=ContextV3FeatureGenerationService(repository)
    try:
        rows,inserted=service.generate(args.revision_id,args.instrument_id,args.feature_profile,
            research_context_runs=pins['researchContextRuns'],
            market_structure_runs=pins['marketStructureRuns'],
            membership_snapshot_checksums=pins['membershipSnapshotChecksums'],
            scheduled_configuration=pins['scheduledConfiguration'],
            market_structure_rules=pins['marketStructureRules'])
    except (ValueError,TypeError,OSError) as exc:
        print(json.dumps({'status':'BLOCKED','error':str(exc)},indent=2)); return 2
    unavailable=[row for row in rows if row.quality_status!='PASS']; reasons={}
    for row in unavailable:
        blocks=(row.values.get('_contextV3') or {}).get('blocks') or {}; found=False
        for block in blocks.values():
            if block.get('status')=='PASS': continue
            for reason in block.get('missingReasons') or ():
                key='MISSING_REQUIRED_FEATURES' if str(reason).startswith('MISSING:') else str(reason)
                reasons[key]=reasons.get(key,0)+1; found=True
        if not found:
            reasons['NO_ELIGIBLE_HORIZON_OR_BLOCK_REASON']=reasons.get('NO_ELIGIBLE_HORIZON_OR_BLOCK_REASON',0)+1
    status='PASS' if rows and not unavailable else 'PARTIAL' if rows else 'UNAVAILABLE'
    result={'status':status,'profile':args.feature_profile,'revision':args.revision_id,
        'rows':{'considered':len(rows),'generated':inserted,'reused':len(rows)-inserted},
        'passCount':len(rows)-len(unavailable),'unavailableCount':len(unavailable),
        'unavailableReasons':dict(sorted(reasons.items()))}
    if service.last_manifest:
        result['manifestVersion']=service.last_manifest['version']
        result['manifestChecksum']=service.last_manifest['checksum']
    print(json.dumps(result,indent=2))
    return 0


def command_context_v3_activate(args):
    from .context_v3 import ContextV3ActivationService
    try:
        repository=PostgresRepository(Settings.from_env().database_url)
        result=ContextV3ActivationService(repository).activate(args.revision_id,args.instrument_id,
            args.profiles,output_dir=args.output_dir,dry_run=args.dry_run)
    except (ValueError,TypeError,OSError) as exc:
        print(json.dumps({'status':'BLOCKED','error':str(exc)},indent=2)); return 2
    print(json.dumps(result,indent=2))
    return 0 if result['status'] in ('PASS','READY') else 2


def command_context_v3_history_preflight(args):
    from .context_v3_history import ContextV3HistoryPreflightService
    try:
        repository=PostgresRepository(Settings.from_env().database_url)
        result=ContextV3HistoryPreflightService(repository).inspect(
            args.revision_id,args.instrument_id,date_from=args.from_date,date_to=args.to_date,
            profiles=args.profiles,allow_network=args.allow_network)
    except (ValueError,TypeError,OSError) as exc:
        print(json.dumps({'status':'BLOCKED','error':str(exc)},indent=2)); return 2
    _write_json(Path(args.output),result)
    print(json.dumps({'status':result['status'],'output':args.output,
        'blockerCount':len(result['blockers']),'networkProbe':result['networkProbe']},indent=2))
    return 0 if result['status']=='READY' else 2


def command_prepare_context_v3_history(args):
    from .context_v3_history import ContextV3HistoryPreparationService
    try:
        repository=PostgresRepository(Settings.from_env().database_url)
        result=ContextV3HistoryPreparationService(repository).prepare(
            args.revision_id,args.instrument_id,date_from=args.from_date,date_to=args.to_date,
            profiles=args.profiles,output_dir=args.output_dir,dry_run=args.dry_run,
            allow_network=args.allow_network)
    except (ValueError,TypeError,OSError) as exc:
        print(json.dumps({'status':'BLOCKED','error':str(exc)},indent=2)); return 2
    print(json.dumps(result,indent=2,default=str))
    return 0 if result['status'] in ('PASS','READY') else 2


def command_evaluate_context_v3(args):
    from .context_v3_experiments import ContextV3ExperimentService
    from .context_v3 import PROFILES
    algorithms=tuple(part.strip() for part in args.algorithms.split(',') if part.strip())
    requested=args.profiles or args.profile
    profiles=list(PROFILES) if requested=='all' else list(dict.fromkeys(part.strip() for part in requested.split(',') if part.strip()))
    repository=PostgresRepository(Settings.from_env().database_url)
    results=[]
    for profile in profiles:
        try:
            result=ContextV3ExperimentService(repository).run(
                args.revision_id,args.instrument_id,profile,date_from=args.from_date,
                date_to=args.to_date,minimum_training_days=args.minimum_training_days,
                horizons=tuple(int(v) for v in args.horizons.split(',')),
                evaluation_step_days=args.evaluation_step_days,
                holdout_days=args.holdout_days,algorithms=algorithms,persist=not args.no_persist)
        except Exception as exc:
            result={'status':'BLOCKED','error':str(exc),'requestedHorizons':args.horizons,'requestedAlgorithms':list(algorithms)}
        results.append({'profileVersion':profile,**result})
    result=results[0] if args.profile else {'profiles':results}
    if args.output: _write_json(Path(args.output),result)
    print(json.dumps({'profiles':[{'profileVersion':r['profileVersion'],'status':r['status'],
        'error':r.get('error'),'experimentId':r.get('manifest',{}).get('experimentId')} for r in results],
        'output':args.output},indent=2))
    return 0 if results and all(r['status'] in ('V3_BETTER','NO_CLEAR_IMPROVEMENT','V3_WORSE') for r in results) else 2


def command_context_v3_research_run(args):
    from .context_v3_history import ContextV3ResearchRunService
    result=ContextV3ResearchRunService(PostgresRepository(Settings.from_env().database_url)).run(
        args.revision_id,args.instrument_id,date_from=args.from_date,date_to=args.to_date,profiles=args.profiles,
        algorithms=tuple(v.strip() for v in args.algorithms.split(',')),horizons=tuple(int(v) for v in args.horizons.split(',')),
        output_dir=args.output_dir,dry_run=args.dry_run,allow_network=args.allow_network)
    print(json.dumps(result,indent=2,default=str))
    return 0 if args.dry_run or all(r['status'] in ('V3_BETTER','V3_WORSE','NO_CLEAR_IMPROVEMENT') for r in result['summary']['experiments']) else 2


def command_research_context_import_local(args):
    from .research_context import LocalResearchContextImportService
    try:
        result=LocalResearchContextImportService().import_file(args.input,
            instrument_id=args.instrument_id,source_name=args.source_name,
            source_observed_at=args.source_observed_at)
    except (ValueError,TypeError,OSError) as exc:
        print(json.dumps({'status':'BLOCKED','error':str(exc)},indent=2)); return 2
    if args.output: _write_json(Path(args.output),result)
    print(json.dumps(result,indent=2))
    return 0 if result['status']=='COMPLETE' else 2


def command_backfill_research_context(args):
    from .research_context import ResearchContextBackfillService
    try:
        result=ResearchContextBackfillService().backfill(revision_id=args.revision_id,
            instrument_id=args.instrument_id,from_date=args.from_date,to_date=args.to_date,
            input_dir=args.input_dir,source_name=args.source_name,source_observed_at=args.source_observed_at)
    except (ValueError,TypeError,OSError) as exc:
        print(json.dumps({'status':'BLOCKED','error':str(exc)},indent=2)); return 2
    if args.output: _write_json(Path(args.output),result)
    print(json.dumps(result,indent=2))
    return 0 if result['status']=='COMPLETE' else 2


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
