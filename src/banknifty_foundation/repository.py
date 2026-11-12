from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from typing import Any, Iterator, Protocol
from uuid import NAMESPACE_URL, uuid4, uuid5

from .models import (
    Candle,
    DatasetRevision,
    Instrument,
    QualityIssue,
    RevisionCandle,
    TargetLabel,
    TradingSession,
)
from .research_models import (
    BaselineDefinition,
    FeatureRow,
    FeatureSetDefinition,
    PredictionOutcome,
    ResearchPrediction,
)
from .ml_records import MLPrediction, MLPredictionOutcome, validate_prediction
from .big_move_labels import BigMoveLabel, BigMoveThresholdPolicy


@dataclass
class JobState:
    id: str
    instrument_id: int
    requested_from: datetime
    requested_to: datetime
    checkpoint_at: datetime | None = None
    status: str = "PENDING"
    attempts: int = 0
    rows_received: int = 0
    rows_inserted: int = 0
    error_code: str | None = None
    error_message: str | None = None


class Repository(Protocol):
    def store_session(self, session: TradingSession) -> None: ...
    def store_snapshot(self, raw: list[dict[str, Any]], instruments: list[Instrument], captured_at: datetime) -> tuple[int, dict[int, int]]: ...
    def get_or_create_job(self, instrument_id: int, start: datetime, end: datetime) -> JobState: ...
    def mark_job_running(self, job_id: str) -> None: ...
    def update_checkpoint(self, job_id: str, checkpoint: datetime, received: int, inserted: int) -> None: ...
    def complete_job(self, job_id: str) -> None: ...
    def fail_job(self, job_id: str, code: str, message: str) -> None: ...
    def insert_candles(self, instrument_id: int, job_id: str, candles: list[Candle]) -> tuple[int, int]: ...
    def store_issues(self, job_id: str, instrument_id: int, issues: list[QualityIssue]) -> None: ...
    def candle_times(self, instrument_id: int, start: datetime, end: datetime) -> set[datetime]: ...
    def issue_counts(self, instrument_id: int, session_date: date) -> dict[str, int]: ...
    def sessions_between(self, start: date, end: date) -> list[TradingSession]: ...
    def revision_source_rows(
        self, instrument_ids: tuple[int, ...], start: datetime, end: datetime
    ) -> list[RevisionCandle]: ...
    def issues_between(
        self, instrument_ids: tuple[int, ...], start: datetime, end: datetime
    ) -> list[QualityIssue]: ...
    def create_revision(self, revision: DatasetRevision, rows: list[RevisionCandle]) -> DatasetRevision: ...
    def get_revision(self, revision_id: str) -> DatasetRevision: ...
    def decide_revision(self, revision_id: str, decision: str, reason: str) -> DatasetRevision: ...
    def revision_candles(self, revision_id: str, instrument_id: int) -> list[Candle]: ...
    def save_labels(self, labels: list[TargetLabel]) -> int: ...
    def get_instrument(self, instrument_id: int) -> Instrument: ...
    def target_labels(self, revision_id: str, instrument_id: int) -> list[TargetLabel]: ...
    def save_feature_set(self, definition: FeatureSetDefinition) -> None: ...
    def save_feature_rows(self, rows: list[FeatureRow]) -> int: ...
    def feature_rows(self, revision_id: str, instrument_id: int, version: str) -> list[FeatureRow]: ...
    def save_baseline_definition(self, definition: BaselineDefinition) -> None: ...
    def save_research_run(self, run_id: str, manifest: dict[str, Any], checksum: str) -> None: ...
    def save_fold(self, run_id: str, fold_id: str, values: dict[str, Any]) -> None: ...
    def save_predictions(self, predictions: list[ResearchPrediction]) -> int: ...
    def predictions_for_run(self, run_id: str) -> list[ResearchPrediction]: ...
    def append_outcome(self, outcome: PredictionOutcome) -> bool: ...
    def outcomes_for_run(self, run_id: str) -> list[PredictionOutcome]: ...
    def save_run_report(self, run_id: str, report: dict[str, Any], checksum: str) -> None: ...
    def run_report(self, run_id: str) -> dict[str, Any]: ...
    def save_model_definition(self, manifest: dict[str, Any]) -> bool: ...
    def save_training_manifest(self, manifest: dict[str, Any]) -> bool: ...
    def save_model_comparison(self, comparison: dict[str, Any]) -> bool: ...
    def save_model_bundle(self, bundle: dict[str, Any]) -> bool: ...
    def save_calibration_run(self, calibration: dict[str, Any]) -> bool: ...
    def save_abstention_policy(self, version: str, checksum: str, definition: dict[str, Any]) -> bool: ...
    def save_ml_predictions(self, records: list[MLPrediction]) -> int: ...
    def append_ml_prediction_outcome(self, outcome: MLPredictionOutcome) -> bool: ...
    def save_big_move_policy(self, policy: BigMoveThresholdPolicy) -> None: ...
    def save_big_move_labels(self, labels: list[BigMoveLabel]) -> int: ...
    def big_move_labels(self, revision_id: str, instrument_id: int) -> list[BigMoveLabel]: ...


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class MemoryRepository:
    """Deterministic offline repository used only by fixtures and tests."""

    def __init__(self, *, clock: Any | None = None) -> None:
        from datetime import timezone

        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.sessions: dict[date, TradingSession] = {}
        self.snapshots: dict[int, dict[str, Any]] = {}
        self.instruments: dict[int, Instrument] = {}
        self.instrument_snapshots: dict[int, int] = {}
        self.jobs: dict[str, JobState] = {}
        self._job_keys: dict[tuple[int, datetime, datetime], str] = {}
        self.candles: dict[tuple[int, datetime], Candle] = {}
        self.candle_job_ids: dict[tuple[int, datetime], str] = {}
        self.issues: dict[str, tuple[str, int, QualityIssue]] = {}
        self.revisions: dict[str, DatasetRevision] = {}
        self.revision_rows: dict[str, tuple[RevisionCandle, ...]] = {}
        self.labels: dict[tuple[str, int, datetime, int, str], TargetLabel] = {}
        self.threshold_policies: dict[str, dict[int, Decimal]] = {}
        self.feature_sets: dict[str, FeatureSetDefinition] = {}
        self.features: dict[tuple[str, int, datetime, str], FeatureRow] = {}
        self.baseline_definitions: dict[str, BaselineDefinition] = {}
        self.research_runs: dict[str, dict[str, Any]] = {}
        self.folds: dict[tuple[str, str], dict[str, Any]] = {}
        self.predictions: dict[str, ResearchPrediction] = {}
        self.outcomes: dict[str, PredictionOutcome] = {}
        self.run_reports: dict[str, dict[str, Any]] = {}
        self.model_definitions: dict[str, dict[str, Any]] = {}
        self.training_manifests: dict[str, dict[str, Any]] = {}
        self.model_comparisons: dict[str, dict[str, Any]] = {}
        self.model_bundles: dict[str, dict[str, Any]] = {}
        self.calibration_runs: dict[str, dict[str, Any]] = {}
        self.abstention_policies: dict[str, dict[str, Any]] = {}
        self.ml_predictions: dict[str, MLPrediction] = {}
        self._ml_prediction_keys: dict[
            tuple[str, int, int, datetime, str], str
        ] = {}
        self.ml_prediction_outcomes: dict[str, MLPredictionOutcome] = {}
        self.big_move_threshold_policies: dict[str, BigMoveThresholdPolicy] = {}
        self.big_move_label_rows: dict[tuple[str, int, datetime, int, str], BigMoveLabel] = {}
        self._next_snapshot = 1
        self._next_instrument = 1
        self._next_job = 1

    def store_session(self, session: TradingSession) -> None:
        self.sessions[session.session_date] = session

    def store_snapshot(
        self, raw: list[dict[str, Any]], instruments: list[Instrument], captured_at: datetime
    ) -> tuple[int, dict[int, int]]:
        raw_hash = digest(raw)
        for snapshot_id, item in self.snapshots.items():
            if item["hash"] == raw_hash:
                mapping = {
                    instrument.instrument_token: instrument_id
                    for instrument_id, instrument in self.instruments.items()
                    if self.instrument_snapshots[instrument_id] == snapshot_id
                }
                return snapshot_id, mapping
        snapshot_id = self._next_snapshot
        self._next_snapshot += 1
        self.snapshots[snapshot_id] = {"id": snapshot_id, "hash": raw_hash, "captured_at": captured_at, "raw": raw}
        mapping: dict[int, int] = {}
        for instrument in instruments:
            instrument_id = self._next_instrument
            self._next_instrument += 1
            self.instruments[instrument_id] = instrument
            self.instrument_snapshots[instrument_id] = snapshot_id
            mapping[instrument.instrument_token] = instrument_id
        return snapshot_id, mapping

    def get_or_create_job(self, instrument_id: int, start: datetime, end: datetime) -> JobState:
        key = (instrument_id, start, end)
        if key in self._job_keys:
            return self.jobs[self._job_keys[key]]
        job = JobState(
            str(uuid5(NAMESPACE_URL, f"banknifty-memory-job-{self._next_job}")),
            instrument_id,
            start,
            end,
        )
        self._next_job += 1
        self.jobs[job.id] = job
        self._job_keys[key] = job.id
        return job

    def mark_job_running(self, job_id: str) -> None:
        job = self.jobs[job_id]
        job.status = "RUNNING"
        job.attempts += 1

    def update_checkpoint(self, job_id: str, checkpoint: datetime, received: int, inserted: int) -> None:
        job = self.jobs[job_id]
        job.checkpoint_at = checkpoint
        job.rows_received += received
        job.rows_inserted += inserted

    def complete_job(self, job_id: str) -> None:
        self.jobs[job_id].status = "COMPLETED"

    def fail_job(self, job_id: str, code: str, message: str) -> None:
        job = self.jobs[job_id]
        job.status = "FAILED"
        job.error_code = code
        job.error_message = message

    def insert_candles(self, instrument_id: int, job_id: str, candles: list[Candle]) -> tuple[int, int]:
        inserted = 0
        duplicates = 0
        for candle in candles:
            key = (instrument_id, candle.timestamp)
            if key in self.candles:
                duplicates += 1
                continue
            self.candles[key] = candle
            self.candle_job_ids[key] = job_id
            inserted += 1
        return inserted, duplicates

    def store_issues(self, job_id: str, instrument_id: int, issues: list[QualityIssue]) -> None:
        for issue in issues:
            fingerprint = digest(
                {
                    "job": job_id,
                    "instrument": instrument_id,
                    "code": issue.code,
                    "timestamp": issue.timestamp,
                    "message": issue.message,
                }
            )
            self.issues.setdefault(fingerprint, (job_id, instrument_id, issue))

    def candle_times(self, instrument_id: int, start: datetime, end: datetime) -> set[datetime]:
        return {
            timestamp
            for (stored_instrument, timestamp) in self.candles
            if stored_instrument == instrument_id and start <= timestamp < end
        }

    def issue_counts(self, instrument_id: int, session_date: date) -> dict[str, int]:
        counts: Counter[str] = Counter()
        for _, stored_instrument, issue in self.issues.values():
            if stored_instrument == instrument_id and issue.timestamp and issue.timestamp.date() == session_date:
                counts[issue.code] += 1
        return dict(sorted(counts.items()))

    def sessions_between(self, start: date, end: date) -> list[TradingSession]:
        return [self.sessions[item] for item in sorted(self.sessions) if start <= item <= end]

    def revision_source_rows(
        self, instrument_ids: tuple[int, ...], start: datetime, end: datetime
    ) -> list[RevisionCandle]:
        return [
            RevisionCandle(instrument_id, candle, self.candle_job_ids[(instrument_id, timestamp)])
            for (instrument_id, timestamp), candle in sorted(self.candles.items())
            if instrument_id in instrument_ids and start <= timestamp < end
        ]

    def issues_between(
        self, instrument_ids: tuple[int, ...], start: datetime, end: datetime
    ) -> list[QualityIssue]:
        result: list[QualityIssue] = []
        for job_id, instrument_id, issue in self.issues.values():
            if instrument_id not in instrument_ids:
                continue
            if issue.timestamp is not None and start <= issue.timestamp < end:
                result.append(issue)
            elif issue.timestamp is None:
                job = self.jobs[job_id]
                if job.requested_from < end and job.requested_to >= start:
                    result.append(issue)
        return result

    def create_revision(self, revision: DatasetRevision, rows: list[RevisionCandle]) -> DatasetRevision:
        if revision.id in self.revisions:
            raise ValueError("dataset revision already exists")
        self.revisions[revision.id] = deepcopy(revision)
        self.revision_rows[revision.id] = tuple(rows)
        return revision

    def get_revision(self, revision_id: str) -> DatasetRevision:
        try:
            return deepcopy(self.revisions[revision_id])
        except KeyError as exc:
            raise ValueError(f"Unknown dataset revision: {revision_id}") from exc

    def decide_revision(self, revision_id: str, decision: str, reason: str) -> DatasetRevision:
        current = self.get_revision(revision_id)
        if current.status != "CANDIDATE":
            raise ValueError("only a CANDIDATE revision can be decided")
        if decision not in {"ACCEPTED", "REJECTED"}:
            raise ValueError("decision must be ACCEPTED or REJECTED")
        from dataclasses import replace
        updated = replace(
            current,
            status=decision,
            decided_at=self._clock(),
            decision_reason=reason,
        )
        self.revisions[revision_id] = deepcopy(updated)
        return updated

    def revision_candles(self, revision_id: str, instrument_id: int) -> list[Candle]:
        return [
            row.candle
            for row in self.revision_rows.get(revision_id, ())
            if row.instrument_id == instrument_id
        ]

    def save_labels(self, labels: list[TargetLabel]) -> int:
        inserted = 0
        for label in labels:
            key = (
                label.dataset_revision_id,
                label.instrument_id,
                label.as_of,
                label.horizon_minutes,
                label.threshold_policy_version,
            )
            if key not in self.labels:
                self.labels[key] = label
                inserted += 1
        return inserted

    def save_threshold_policy(self, version: str, thresholds: dict[int, Decimal]) -> None:
        existing = self.threshold_policies.get(version)
        if existing is not None and existing != thresholds:
            raise ValueError("threshold policy versions are immutable")
        self.threshold_policies[version] = dict(thresholds)

    def get_instrument(self, instrument_id: int) -> Instrument:
        try:
            return self.instruments[instrument_id]
        except KeyError as exc:
            raise ValueError(f"Unknown instrument: {instrument_id}") from exc

    def target_labels(self, revision_id: str, instrument_id: int) -> list[TargetLabel]:
        return sorted(
            [
                deepcopy(item)
                for item in self.labels.values()
                if item.dataset_revision_id == revision_id and item.instrument_id == instrument_id
            ],
            key=lambda item: (item.as_of, item.horizon_minutes),
        )

    def save_big_move_policy(self, policy: BigMoveThresholdPolicy) -> None:
        existing = self.big_move_threshold_policies.get(policy.version)
        if existing is not None and existing != policy:
            raise ValueError("big-move threshold policy versions are immutable")
        self.big_move_threshold_policies[policy.version] = deepcopy(policy)

    def save_big_move_labels(self, labels: list[BigMoveLabel]) -> int:
        inserted = 0
        for label in labels:
            key = (label.dataset_revision_id, label.instrument_id, label.as_of,
                   label.horizon_minutes, label.threshold_policy_version)
            existing = self.big_move_label_rows.get(key)
            if existing is not None and existing != label:
                raise ValueError("big-move labels are immutable")
            if existing is None:
                self.big_move_label_rows[key] = deepcopy(label)
                inserted += 1
        return inserted

    def big_move_labels(self, revision_id: str, instrument_id: int) -> list[BigMoveLabel]:
        return sorted((deepcopy(item) for item in self.big_move_label_rows.values()
                       if item.dataset_revision_id == revision_id and item.instrument_id == instrument_id),
                      key=lambda item: (item.as_of, item.horizon_minutes))

    def save_feature_set(self, definition: FeatureSetDefinition) -> None:
        existing = self.feature_sets.get(definition.version)
        if existing is not None and existing.checksum != definition.checksum:
            raise ValueError("feature-set versions are immutable")
        if existing is None:
            self.feature_sets[definition.version] = deepcopy(definition)

    def save_feature_rows(self, rows: list[FeatureRow]) -> int:
        inserted = 0
        for row in rows:
            key = (row.dataset_revision_id, row.instrument_id, row.as_of, row.feature_set_version)
            if '_contextV3' in row.values and key in self.features and self.features[key].to_dict()!=row.to_dict():
                raise ValueError('IMMUTABLE_V3_ROW_CONFLICT')
            if key not in self.features:
                self.features[key] = deepcopy(row)
                inserted += 1
        return inserted

    def feature_rows(self, revision_id: str, instrument_id: int, version: str) -> list[FeatureRow]:
        return sorted(
            [
                deepcopy(row)
                for key, row in self.features.items()
                if key[0] == revision_id and key[1] == instrument_id and key[3] == version
            ],
            key=lambda row: row.as_of,
        )

    def save_baseline_definition(self, definition: BaselineDefinition) -> None:
        existing = self.baseline_definitions.get(definition.version)
        if existing is not None and existing != definition:
            raise ValueError("baseline versions are immutable")
        self.baseline_definitions[definition.version] = deepcopy(definition)

    def save_research_run(self, run_id: str, manifest: dict[str, Any], checksum: str) -> None:
        value = {"manifest": deepcopy(manifest), "checksum": checksum}
        existing = self.research_runs.get(run_id)
        if existing is not None and existing != value:
            raise ValueError("research run is immutable")
        self.research_runs[run_id] = value

    def save_fold(self, run_id: str, fold_id: str, values: dict[str, Any]) -> None:
        key = (run_id, fold_id)
        if key in self.folds and self.folds[key] != values:
            raise ValueError("walk-forward fold is immutable")
        self.folds[key] = deepcopy(values)

    def save_predictions(self, predictions: list[ResearchPrediction]) -> int:
        inserted = 0
        for prediction in predictions:
            existing = self.predictions.get(prediction.id)
            if existing is not None and existing != prediction:
                raise ValueError("prediction is immutable")
            if existing is None:
                self.predictions[prediction.id] = deepcopy(prediction)
                inserted += 1
        return inserted

    def predictions_for_run(self, run_id: str) -> list[ResearchPrediction]:
        return sorted(
            [deepcopy(item) for item in self.predictions.values() if item.run_id == run_id],
            key=lambda item: (item.as_of, item.baseline_version, item.horizon_minutes),
        )

    def append_outcome(self, outcome: PredictionOutcome) -> bool:
        existing = self.outcomes.get(outcome.prediction_id)
        if existing is not None and existing != outcome:
            raise ValueError("prediction outcomes are append-only")
        if existing is not None:
            return False
        self.outcomes[outcome.prediction_id] = deepcopy(outcome)
        return True

    def outcomes_for_run(self, run_id: str) -> list[PredictionOutcome]:
        prediction_ids = {item.id for item in self.predictions_for_run(run_id)}
        return sorted(
            [deepcopy(item) for key, item in self.outcomes.items() if key in prediction_ids],
            key=lambda item: item.prediction_id,
        )

    def save_run_report(self, run_id: str, report: dict[str, Any], checksum: str) -> None:
        value = {"report": deepcopy(report), "checksum": checksum}
        existing = self.run_reports.get(run_id)
        if existing is not None and existing != value:
            raise ValueError("run report is immutable")
        self.run_reports[run_id] = value

    def run_report(self, run_id: str) -> dict[str, Any]:
        try:
            return deepcopy(self.run_reports[run_id]["report"])
        except KeyError as exc:
            raise ValueError(f"Unknown research run report: {run_id}") from exc

    def _save_immutable(self, collection: dict[str, Any], key: str, value: Any, label: str) -> bool:
        existing = collection.get(key)
        if existing is not None and existing != value:
            raise ValueError(f"{label} is immutable")
        if existing is not None:
            return False
        collection[key] = deepcopy(value)
        return True

    def save_model_definition(self, manifest: dict[str, Any]) -> bool:
        return self._save_immutable(
            self.model_definitions, str(manifest["model_checksum"]), manifest, "model definition"
        )

    def save_training_manifest(self, manifest: dict[str, Any]) -> bool:
        return self._save_immutable(
            self.training_manifests,
            str(manifest["training_manifest_checksum"]),
            manifest,
            "training manifest",
        )

    def save_model_comparison(self, comparison: dict[str, Any]) -> bool:
        return self._save_immutable(
            self.model_comparisons,
            str(comparison["comparison_checksum"]),
            comparison,
            "model comparison",
        )

    def save_model_bundle(self, bundle: dict[str, Any]) -> bool:
        return self._save_immutable(
            self.model_bundles, str(bundle["bundle_checksum"]), bundle, "model bundle"
        )

    def save_calibration_run(self, calibration: dict[str, Any]) -> bool:
        return self._save_immutable(
            self.calibration_runs, str(calibration["checksum"]), calibration, "calibration run"
        )

    def save_abstention_policy(self, version: str, checksum: str, definition: dict[str, Any]) -> bool:
        for stored in self.abstention_policies.values():
            if stored["version"] == version and stored["checksum"] != checksum:
                raise ValueError("abstention policy version is immutable")
        return self._save_immutable(
            self.abstention_policies,
            checksum,
            {"version": version, "checksum": checksum, "definition": deepcopy(definition)},
            "abstention policy",
        )

    def save_ml_predictions(self, records: list[MLPrediction]) -> int:
        inserted = 0
        for record in records:
            validate_prediction(record)
            semantic_key = (
                record.model_checksum,
                record.instrument_id,
                record.horizon_minutes,
                record.as_of,
                record.policy_checksum,
            )
            existing_id = self._ml_prediction_keys.get(semantic_key)
            if existing_id is not None and existing_id != record.id:
                raise ValueError("ML prediction semantic identity is immutable")
            inserted += int(self._save_immutable(self.ml_predictions, record.id, record, "ML prediction"))
            self._ml_prediction_keys[semantic_key] = record.id
        return inserted

    def append_ml_prediction_outcome(self, outcome: MLPredictionOutcome) -> bool:
        prediction = self.ml_predictions.get(outcome.prediction_id)
        if prediction is None:
            raise ValueError("unknown ML prediction")
        if outcome.attached_replay_clock < prediction.expires_at:
            raise ValueError("prediction outcome cannot be attached before expiry")
        return self._save_immutable(
            self.ml_prediction_outcomes, outcome.prediction_id, outcome, "ML prediction outcome"
        )


class PostgresRepository:
    def _save_v2_evidence(self, table, value):
        from psycopg.types.json import Jsonb
        from psycopg.rows import dict_row
        allowed={'big_move_evaluation_runs':'evaluation_id', 'big_move_model_comparisons':'comparison_id',
                 'big_move_approval_decisions':'approval_id'}
        if table not in allowed: raise ValueError('Unknown V2 evidence table')
        key=allowed[table]
        columns=tuple(value)
        # Callers supply fixed internal records; SQL identifiers still use psycopg quoting.
        from psycopg import sql
        query=sql.SQL('INSERT INTO {} ({}) VALUES ({}) ON CONFLICT DO NOTHING').format(
            sql.Identifier(table),sql.SQL(',').join(map(sql.Identifier,columns)),
            sql.SQL(',').join(sql.Placeholder() for _ in columns))
        with self._connection() as connection:
            if table=='big_move_approval_decisions':
                connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('v2-policy:'+value['policy_version'],))
                prior=connection.execute('SELECT policy_definition FROM big_move_approval_decisions WHERE policy_version=%s LIMIT 1',
                                         (value['policy_version'],)).fetchone()
                if prior and prior[0]!=value['policy_definition']:
                    raise ValueError('Approval policy changes require a new version')
            connection.execute(query,[Jsonb(value[k]) if isinstance(value[k],(dict,list)) else value[k] for k in columns])
            stored=connection.cursor(row_factory=dict_row).execute(sql.SQL('SELECT * FROM {} WHERE {}=%s').format(
                sql.Identifier(table),sql.Identifier(key)),(value[key],)).fetchone()
            if not stored or digest({k:stored[k] for k in columns})!=digest(value):
                raise ValueError('Immutable V2 evidence conflict')

    def save_big_move_evaluation(self,value):
        self._save_v2_evidence('big_move_evaluation_runs',value)

    def save_big_move_comparison(self,value):
        self._save_v2_evidence('big_move_model_comparisons',value)

    def save_big_move_approval(self,value):
        self._save_v2_evidence('big_move_approval_decisions',value)

    def big_move_comparisons(self,revision,instrument):
        from psycopg.rows import dict_row
        with self._connection() as connection:
            return connection.cursor(row_factory=dict_row).execute('''SELECT DISTINCT ON(horizon_minutes) *
                FROM big_move_model_comparisons WHERE dataset_revision_id=%s AND instrument_id=%s
                ORDER BY horizon_minutes,created_at DESC,comparison_checksum DESC''',(revision,instrument)).fetchall()

    def big_move_evaluation(self,checksum):
        from psycopg.rows import dict_row
        with self._connection() as connection:
            row=connection.cursor(row_factory=dict_row).execute('SELECT * FROM big_move_evaluation_runs WHERE evaluation_checksum=%s',(checksum,)).fetchone()
        if row is None: raise ValueError('V2 evaluation not found')
        return row

    def big_move_model_definition(self,checksum):
        from psycopg.rows import dict_row
        with self._connection() as connection:
            row=connection.cursor(row_factory=dict_row).execute('SELECT * FROM big_move_model_definitions WHERE model_checksum=%s',(checksum,)).fetchone()
        if row is None: raise ValueError('V2 model not found')
        return row

    def big_move_policy(self, version):
        with self._connection() as connection:
            row=connection.execute('SELECT definition FROM big_move_threshold_policies WHERE version=%s',(version,)).fetchone()
        if row is None: raise ValueError('Big Move threshold policy not found')
        value=row[0]
        return BigMoveThresholdPolicy(version,
            {int(k):Decimal(str(v)) for k,v in value['minimum_returns'].items()},
            {int(k):Decimal(str(v)) for k,v in value['volatility_multipliers'].items()},
            value['volatility_lookback_minutes'])

    def save_big_move_model_definition(self, definition):
        return self.save_big_move_model_definitions([definition])

    def save_big_move_model_definitions(self, definitions):
        """Insert complete two-stage bundles atomically; approval never mutates a row."""
        columns=('model_checksum','model_family','stage','horizon_minutes','algorithm','model_version',
                 'dataset_revision_id','feature_set_version','artifact_path','artifact_checksum','manifest','status')
        from psycopg.types.json import Jsonb
        inserted=0
        with self._connection() as connection:
            for item in definitions:
                values=[Jsonb(item[k]) if k=='manifest' else item[k] for k in columns]
                row=connection.execute('INSERT INTO big_move_model_definitions ('+','.join(columns)+') VALUES ('+
                    ','.join(['%s']*len(columns))+') ON CONFLICT DO NOTHING RETURNING model_checksum',values).fetchone()
                if row: inserted+=1
                else:
                    existing=connection.execute('SELECT artifact_checksum,status,model_version,manifest FROM big_move_model_definitions WHERE model_checksum=%s',
                                                (item['model_checksum'],)).fetchone()
                    if not existing or existing != (item['artifact_checksum'],item['status'],item['model_version'],item['manifest']):
                        raise ValueError('Immutable Big Move model definition conflict')
        return inserted

    def approved_big_move_models(self):
        from psycopg.rows import dict_row
        with self._connection() as connection:
            rows=connection.cursor(row_factory=dict_row).execute('''SELECT * FROM big_move_model_definitions
                WHERE status='APPROVED' AND model_family='BIG_MOVE_V2'
                  AND EXISTS (SELECT 1 FROM big_move_approval_decisions a
                    WHERE a.approval_checksum=manifest->>'approval_decision_checksum' AND a.decision='PASS')
                ORDER BY created_at DESC,model_version DESC''').fetchall()
        # Require both stages from the same immutable combined artifact/version.
        result={}
        for move in (r for r in rows if r['stage']=='MOVE_DETECTOR'):
            horizon=move['horizon_minutes']
            if horizon in result: continue
            direction=next((r for r in rows if r['stage']=='DIRECTION' and r['horizon_minutes']==horizon
                and r['model_version']==move['model_version'] and r['artifact_checksum']==move['artifact_checksum']
                and r['dataset_revision_id']==move['dataset_revision_id'] and r['feature_set_version']==move['feature_set_version']),None)
            if direction: result[horizon]={'move':move,'direction':direction}
        return result

    def save_big_move_prediction_with_evidence(self, value, journal):
        from psycopg import sql
        from psycopg.types.json import Jsonb
        from .evidence_journal import IMPLEMENTATION, _plain
        identity=value['prediction_id']
        if journal['prediction_id']!=identity or journal['journal_id']!=digest({'prediction_id':identity,'implementation':IMPLEMENTATION}):
            raise ValueError('Prediction/journal identity mismatch')
        if digest(_plain({k:v for k,v in journal.items() if k!='journal_checksum'}))!=journal['journal_checksum']:
            raise ValueError('Evidence checksum mismatch')
        for jk,pk in (('instrument_id','instrument_id'),('market_as_of','as_of'),('horizon_minutes','horizon_minutes'),
                      ('model_version','model_version'),('move_model_checksum','move_model_checksum'),
                      ('direction_model_checksum','direction_model_checksum')):
            if journal[jk]!=value[pk]: raise ValueError('Prediction/evidence lineage mismatch')
        for key,column in {'horizon':'horizon_minutes','state':'state','bigMoveProbability':'big_move_probability',
            'quietProbability':'quiet_probability','direction':'direction','directionProbability':'direction_probability',
            'upGivenBigMoveProbability':'up_given_big_move_probability','downGivenBigMoveProbability':'down_given_big_move_probability',
            'expectedMovePoints':'expected_move_points','expectedMovePercent':'expected_move_percent',
            'referencePrice':'reference_price','marketRegime':'market_regime','sessionBias':'session_bias',
            'modelVersion':'model_version','featureSetVersion':'feature_set_version','qualityStatus':'quality_status'}.items():
            if journal['baseline_prediction'].get(key)!=_plain(value[column]):
                raise ValueError('Journal baseline differs from the generated prediction')
        if journal['market_evidence']['fullFeatureValues']!=value['session_bias']['runtime_lineage']['feature_values']:
            raise ValueError('Journal features differ from inference lineage')
        with self._connection() as connection:
            connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('evidence:'+identity,))
            existing=connection.execute('SELECT prediction_id FROM big_move_predictions WHERE prediction_id=%s',(identity,)).fetchone()
            if existing:
                evidence=connection.execute('SELECT journal_id FROM big_move_evidence_journal WHERE prediction_id=%s',(identity,)).fetchone()
                if evidence: return 'ALREADY_PERSISTED'
                raise ValueError('EVIDENCE_JOURNAL_MISSING: existing prediction cannot be backfilled')
            if not self.save_big_move_prediction(value,connection=connection):
                raise ValueError('EVIDENCE_JOURNAL_MISSING: prediction identity conflict')
            columns=tuple(journal)
            connection.execute(sql.SQL('INSERT INTO big_move_evidence_journal ({}) VALUES ({})').format(
                sql.SQL(',').join(map(sql.Identifier,columns)),sql.SQL(',').join(sql.Placeholder() for _ in columns)),
                [Jsonb(journal[k]) if isinstance(journal[k],dict) else journal[k] for k in columns])
        return 'PERSISTED'

    def save_big_move_prediction(self, value, *, connection=None):
        from contextlib import nullcontext
        from psycopg.types.json import Jsonb
        columns=('prediction_id','dataset_revision_id','instrument_id','feature_set_version','as_of','horizon_minutes',
            'expires_at','reference_price','move_model_checksum','direction_model_checksum','big_move_probability',
            'quiet_probability','state','direction','direction_probability','up_given_big_move_probability',
            'down_given_big_move_probability','expected_move_points','expected_move_percent','market_regime',
            'session_bias','quality_status','model_version')
        with (self._connection() if connection is None else nullcontext(connection)) as connection:
            stages=connection.execute('SELECT stage,status,horizon_minutes,model_version FROM big_move_model_definitions WHERE model_checksum IN (%s,%s)',
                (value['move_model_checksum'],value['direction_model_checksum'])).fetchall()
            if len(stages)!=2 or {r[0] for r in stages}!={'MOVE_DETECTOR','DIRECTION'} or any(
                r[1]!='APPROVED' or r[2]!=value['horizon_minutes'] or r[3]!=value['model_version'] for r in stages):
                raise ValueError('Prediction requires a matching APPROVED two-stage model')
            row=connection.execute('INSERT INTO big_move_predictions ('+','.join(columns)+') VALUES ('+
                ','.join(['%s']*len(columns))+') ON CONFLICT DO NOTHING RETURNING prediction_id',
                [Jsonb(value[k]) if k=='session_bias' else value[k] for k in columns]).fetchone()
        return row is not None

    def pending_big_move_predictions(self, now, limit=500):
        from psycopg.rows import dict_row
        with self._connection() as connection:
            return connection.cursor(row_factory=dict_row).execute('''SELECT p.*,m.manifest FROM big_move_predictions p
                JOIN big_move_model_definitions m ON m.model_checksum=p.move_model_checksum
                LEFT JOIN big_move_prediction_outcomes o USING(prediction_id)
                WHERE o.prediction_id IS NULL AND p.expires_at<=%s ORDER BY p.expires_at LIMIT %s''',(now,limit)).fetchall()

    def append_big_move_prediction_outcome(self, prediction_id, actual_label, quality_status, attached_at, realized_summary):
        from psycopg.types.json import Jsonb
        with self._connection() as connection:
            row=connection.execute('''INSERT INTO big_move_prediction_outcomes
                (prediction_id,actual_label,quality_status,attached_at,realized_summary) VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT(prediction_id) DO NOTHING RETURNING prediction_id''',
                (prediction_id,actual_label,quality_status,attached_at,Jsonb(realized_summary))).fetchone()
        return row is not None

    def __init__(self, database_url: str, migration_dir: Path | None = None) -> None:
        if not database_url.startswith(("postgresql://", "postgres://")):
            raise ValueError("PostgresRepository requires a PostgreSQL DATABASE_URL")
        self.database_url = database_url
        self.migration_dir = migration_dir or Path(__file__).resolve().parents[2] / "migrations"

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        import psycopg

        with psycopg.connect(self.database_url, **getattr(self,'connection_options',{})) as connection:
            yield connection

    def apply_migrations(self) -> list[str]:
        applied: list[str] = []
        with self._connection() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(8172401)")
            connection.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
            for path in sorted(self.migration_dir.glob("*.sql")):
                if connection.execute("SELECT 1 FROM schema_migrations WHERE version=%s", (path.stem,)).fetchone():
                    continue
                connection.execute(path.read_text(encoding="utf-8"))
                connection.execute("INSERT INTO schema_migrations(version) VALUES (%s) ON CONFLICT DO NOTHING", (path.stem,))
                applied.append(path.name)
        return applied

    def store_session(self, session: TradingSession) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO trading_sessions(session_date, market_open_utc, market_close_utc, status)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT(session_date) DO UPDATE SET
                  market_open_utc=EXCLUDED.market_open_utc,
                  market_close_utc=EXCLUDED.market_close_utc,
                  status=EXCLUDED.status
                """,
                (session.session_date, session.open_at, session.close_at, session.status),
            )

    def store_snapshot(
        self, raw: list[dict[str, Any]], instruments: list[Instrument], captured_at: datetime
    ) -> tuple[int, dict[int, int]]:
        raw_hash = digest(raw)
        with self._connection() as connection:
            row = connection.execute(
                """
                INSERT INTO instrument_snapshots(snapshot_date, captured_at, source_sha256, record_count, raw_payload, last_verified_at)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s)
                ON CONFLICT(source_sha256) DO UPDATE SET last_verified_at=GREATEST(instrument_snapshots.last_verified_at,EXCLUDED.last_verified_at)
                RETURNING id
                """,
                (captured_at.date(), captured_at, raw_hash, len(raw), canonical_json(raw),captured_at),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT id FROM instrument_snapshots WHERE source_sha256=%s",
                    (raw_hash,),
                ).fetchone()
            snapshot_id = int(row[0])
            mapping: dict[int, int] = {}
            for item in instruments:
                saved = connection.execute(
                    """
                    INSERT INTO canonical_instruments(
                      snapshot_id, instrument_token, exchange, tradingsymbol, name, segment,
                      instrument_type, expiry, tick_size, lot_size, strike
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(snapshot_id, instrument_token) DO UPDATE
                      SET tradingsymbol=EXCLUDED.tradingsymbol, strike=COALESCE(canonical_instruments.strike,EXCLUDED.strike)
                    RETURNING id
                    """,
                    (
                        snapshot_id,
                        item.instrument_token,
                        item.exchange,
                        item.tradingsymbol,
                        item.name,
                        item.segment,
                        item.instrument_type,
                        item.expiry,
                        item.tick_size,
                        item.lot_size,
                        item.strike,
                    ),
                ).fetchone()
                mapping[item.instrument_token] = int(saved[0])
        return snapshot_id, mapping

    @staticmethod
    def _job(row: Any) -> JobState:
        return JobState(
            id=str(row[0]),
            instrument_id=row[1],
            requested_from=row[2],
            requested_to=row[3],
            checkpoint_at=row[4],
            status=row[5],
            attempts=row[6],
            rows_received=row[7],
            rows_inserted=row[8],
            error_code=row[9],
            error_message=row[10],
        )

    def get_or_create_job(self, instrument_id: int, start: datetime, end: datetime) -> JobState:
        job_id = str(uuid4())
        with self._connection() as connection:
            row = connection.execute(
                """
                INSERT INTO historical_import_jobs(id, instrument_id, requested_from, requested_to, status)
                VALUES (%s,%s,%s,%s,'PENDING')
                ON CONFLICT(instrument_id, requested_from, requested_to)
                DO UPDATE SET updated_at=now()
                RETURNING id, instrument_id, requested_from, requested_to, checkpoint_at, status,
                          attempts, rows_received, rows_inserted, error_code, error_message
                """,
                (job_id, instrument_id, start, end),
            ).fetchone()
        return self._job(row)

    def mark_job_running(self, job_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE historical_import_jobs SET status='RUNNING', attempts=attempts+1,
                  started_at=COALESCE(started_at, now()), error_code=NULL, error_message=NULL, updated_at=now()
                WHERE id=%s
                """,
                (job_id,),
            )

    def update_checkpoint(self, job_id: str, checkpoint: datetime, received: int, inserted: int) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE historical_import_jobs SET checkpoint_at=%s, rows_received=rows_received+%s,
                  rows_inserted=rows_inserted+%s, updated_at=now() WHERE id=%s
                """,
                (checkpoint, received, inserted, job_id),
            )

    def complete_job(self, job_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE historical_import_jobs SET status='COMPLETED', completed_at=now(), updated_at=now() WHERE id=%s",
                (job_id,),
            )

    def fail_job(self, job_id: str, code: str, message: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE historical_import_jobs SET status='FAILED', error_code=%s, error_message=%s,
                  updated_at=now() WHERE id=%s
                """,
                (code, message[:2000], job_id),
            )

    def insert_candles(self, instrument_id: int, job_id: str, candles: list[Candle]) -> tuple[int, int]:
        if not candles:
            return 0, 0
        inserted = 0
        with self._connection() as connection:
            for candle in candles:
                row = connection.execute(
                    """
                    INSERT INTO one_minute_candles(
                      instrument_id, candle_at, open, high, low, close, volume, open_interest,
                      source_job_id, payload_sha256
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(instrument_id, candle_at) DO NOTHING
                    RETURNING 1
                    """,
                    (
                        instrument_id,
                        candle.timestamp,
                        candle.open,
                        candle.high,
                        candle.low,
                        candle.close,
                        candle.volume,
                        candle.oi,
                        job_id,
                        digest(candle),
                    ),
                ).fetchone()
                inserted += 1 if row else 0
        return inserted, len(candles) - inserted

    def store_issues(self, job_id: str, instrument_id: int, issues: list[QualityIssue]) -> None:
        if not issues:
            return
        with self._connection() as connection:
            for issue in issues:
                fingerprint = digest(
                    {
                        "job": job_id,
                        "instrument": instrument_id,
                        "code": issue.code,
                        "timestamp": issue.timestamp,
                        "message": issue.message,
                    }
                )
                connection.execute(
                    """
                    INSERT INTO quality_issues(
                      job_id, instrument_id, session_date, candle_at, code, severity, message, details, fingerprint
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
                    ON CONFLICT(fingerprint) DO NOTHING
                    """,
                    (
                        job_id,
                        instrument_id,
                        issue.timestamp.date() if issue.timestamp else None,
                        issue.timestamp,
                        issue.code,
                        issue.severity,
                        issue.message,
                        canonical_json(issue.details or {}),
                        fingerprint,
                    ),
                )

    def candle_times(self, instrument_id: int, start: datetime, end: datetime) -> set[datetime]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT candle_at FROM one_minute_candles
                WHERE instrument_id=%s AND candle_at >= %s AND candle_at < %s
                """,
                (instrument_id, start, end),
            ).fetchall()
        return {row[0] for row in rows}

    def issue_counts(self, instrument_id: int, session_date: date) -> dict[str, int]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT code, count(*) FROM quality_issues
                WHERE instrument_id=%s AND session_date=%s GROUP BY code ORDER BY code
                """,
                (instrument_id, session_date),
            ).fetchall()
        return {row[0]: row[1] for row in rows}

    def sessions_between(self, start: date, end: date) -> list[TradingSession]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT session_date, market_open_utc, market_close_utc, status
                FROM trading_sessions WHERE session_date BETWEEN %s AND %s
                ORDER BY session_date
                """,
                (start, end),
            ).fetchall()
        return [TradingSession(row[0], row[1], row[2], row[3]) for row in rows]

    def revision_source_rows(
        self, instrument_ids: tuple[int, ...], start: datetime, end: datetime
    ) -> list[RevisionCandle]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT instrument_id, candle_at, open, high, low, close, volume,
                       open_interest, source_job_id
                FROM one_minute_candles
                WHERE instrument_id = ANY(%s) AND candle_at >= %s AND candle_at < %s
                ORDER BY instrument_id, candle_at
                """,
                (list(instrument_ids), start, end),
            ).fetchall()
        return [
            RevisionCandle(
                row[0],
                Candle(row[1], row[2], row[3], row[4], row[5], row[6], row[7]),
                str(row[8]),
            )
            for row in rows
        ]

    def issues_between(
        self, instrument_ids: tuple[int, ...], start: datetime, end: datetime
    ) -> list[QualityIssue]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT code, severity, message, candle_at, details
                FROM quality_issues
                WHERE instrument_id = ANY(%s)
                  AND (
                    (candle_at >= %s AND candle_at < %s)
                    OR (
                      candle_at IS NULL AND EXISTS (
                        SELECT 1 FROM historical_import_jobs job
                        WHERE job.id=quality_issues.job_id
                          AND job.requested_from < %s AND job.requested_to >= %s
                      )
                    )
                  )
                ORDER BY id
                """,
                (list(instrument_ids), start, end, end, start),
            ).fetchall()
        return [QualityIssue(row[0], row[1], row[2], row[3], row[4]) for row in rows]

    def create_revision(self, revision: DatasetRevision, rows: list[RevisionCandle]) -> DatasetRevision:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO dataset_revisions(
                  id, created_at, session_start, session_end, row_count, quality_summary,
                  source_job_ids, checksum, status
                ) VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)
                """,
                (
                    revision.id,
                    revision.created_at,
                    revision.session_start,
                    revision.session_end,
                    revision.row_count,
                    canonical_json(revision.quality_summary),
                    list(revision.source_job_ids),
                    revision.checksum,
                    revision.status,
                ),
            )
            for instrument_id in revision.instrument_ids:
                connection.execute(
                    "INSERT INTO dataset_revision_instruments(revision_id, instrument_id) VALUES (%s,%s)",
                    (revision.id, instrument_id),
                )
            for item in rows:
                candle = item.candle
                connection.execute(
                    """
                    INSERT INTO dataset_revision_candles(
                      revision_id, instrument_id, candle_at, open, high, low, close,
                      volume, open_interest, source_job_id
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        revision.id,
                        item.instrument_id,
                        candle.timestamp,
                        candle.open,
                        candle.high,
                        candle.low,
                        candle.close,
                        candle.volume,
                        candle.oi,
                        item.source_job_id,
                    ),
                )
        return revision

    def get_revision(self, revision_id: str) -> DatasetRevision:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT id, created_at, session_start, session_end, row_count,
                       quality_summary, source_job_ids, checksum, status,
                       decided_at, decision_reason
                FROM dataset_revisions WHERE id=%s
                """,
                (revision_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown dataset revision: {revision_id}")
            instruments = connection.execute(
                "SELECT instrument_id FROM dataset_revision_instruments WHERE revision_id=%s ORDER BY instrument_id",
                (revision_id,),
            ).fetchall()
        return DatasetRevision(
            str(row[0]), row[1], tuple(item[0] for item in instruments), row[2], row[3],
            row[4], row[5], tuple(str(item) for item in row[6]), row[7], row[8], row[9], row[10]
        )

    def decide_revision(self, revision_id: str, decision: str, reason: str) -> DatasetRevision:
        if decision not in {"ACCEPTED", "REJECTED"}:
            raise ValueError("decision must be ACCEPTED or REJECTED")
        with self._connection() as connection:
            row = connection.execute(
                """
                UPDATE dataset_revisions
                SET status=%s, decided_at=now(), decision_reason=%s
                WHERE id=%s AND status='CANDIDATE'
                RETURNING id
                """,
                (decision, reason, revision_id),
            ).fetchone()
        if row is None:
            raise ValueError("only a CANDIDATE revision can be decided")
        return self.get_revision(revision_id)

    def revision_candles(self, revision_id: str, instrument_id: int) -> list[Candle]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT candle_at, open, high, low, close, volume, open_interest
                FROM dataset_revision_candles
                WHERE revision_id=%s AND instrument_id=%s ORDER BY candle_at
                """,
                (revision_id, instrument_id),
            ).fetchall()
        return [Candle(*row) for row in rows]

    def save_threshold_policy(self, version: str, thresholds: dict[int, Decimal]) -> None:
        with self._connection() as connection:
            row = connection.execute(
                """
                INSERT INTO threshold_policies(version, thresholds)
                VALUES (%s,%s::jsonb)
                ON CONFLICT(version) DO NOTHING
                RETURNING thresholds
                """,
                (version, canonical_json({str(key): str(value) for key, value in thresholds.items()})),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT thresholds FROM threshold_policies WHERE version=%s",
                    (version,),
                ).fetchone()
        expected = {str(key): str(value) for key, value in thresholds.items()}
        if row[0] != expected:
            raise ValueError("threshold policy versions are immutable")

    def get_instrument(self, instrument_id: int) -> Instrument:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT instrument_token, exchange, tradingsymbol, name, segment,
                       instrument_type, expiry, tick_size, lot_size, strike
                FROM canonical_instruments WHERE id=%s
                """,
                (instrument_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"Unknown instrument: {instrument_id}")
        return Instrument(*row)

    def save_labels(self, labels: list[TargetLabel]) -> int:
        inserted = 0
        with self._connection() as connection:
            for item in labels:
                row = connection.execute(
                    """
                    INSERT INTO target_labels(
                      dataset_revision_id, instrument_id, as_of, horizon_minutes,
                      reference_price, expiry_at, future_price, threshold_policy_version,
                      threshold_value, realised_return, label, quality_status
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT DO NOTHING RETURNING 1
                    """,
                    (
                        item.dataset_revision_id, item.instrument_id, item.as_of,
                        item.horizon_minutes, item.reference_price, item.expiry_at,
                        item.future_price, item.threshold_policy_version, item.threshold_value,
                        item.realised_return, item.label, item.quality_status,
                    ),
                ).fetchone()
                inserted += 1 if row else 0
        return inserted

    def target_labels(self, revision_id: str, instrument_id: int) -> list[TargetLabel]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT dataset_revision_id, instrument_id, as_of, horizon_minutes,
                       reference_price, expiry_at, future_price, threshold_policy_version,
                       threshold_value, realised_return, label, quality_status
                FROM target_labels WHERE dataset_revision_id=%s AND instrument_id=%s
                ORDER BY as_of, horizon_minutes
                """,
                (revision_id, instrument_id),
            ).fetchall()
        return [TargetLabel(str(row[0]), *row[1:]) for row in rows]

    def save_big_move_policy(self, policy: BigMoveThresholdPolicy) -> None:
        definition = {
            "minimum_returns": {str(key): str(value) for key, value in policy.minimum_returns.items()},
            "volatility_multipliers": {str(key): str(value) for key, value in policy.volatility_multipliers.items()},
            "volatility_lookback_minutes": policy.volatility_lookback_minutes,
        }
        checksum = digest({"version": policy.version, **definition})
        with self._connection() as connection:
            row = connection.execute("""
                INSERT INTO big_move_threshold_policies(version, definition, checksum)
                VALUES (%s,%s::jsonb,%s) ON CONFLICT(version) DO NOTHING RETURNING checksum
            """, (policy.version, canonical_json(definition), checksum)).fetchone()
            if row is None:
                row = connection.execute("SELECT checksum FROM big_move_threshold_policies WHERE version=%s", (policy.version,)).fetchone()
        if row[0] != checksum:
            raise ValueError("big-move threshold policy versions are immutable")

    def save_big_move_labels(self, labels: list[BigMoveLabel]) -> int:
        inserted = 0
        with self._connection() as connection:
            for item in labels:
                row = connection.execute("""
                    INSERT INTO big_move_labels(
                      dataset_revision_id, instrument_id, as_of, horizon_minutes, expiry_at,
                      reference_price, threshold_policy_version, threshold_value,
                      max_future_high, min_future_low, maximum_up_excursion,
                      maximum_down_excursion, maximum_favorable_excursion,
                      maximum_adverse_excursion, close_return_at_horizon, realized_range,
                      first_barrier, first_barrier_at, label, quality_status
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT DO NOTHING RETURNING 1
                """, (item.dataset_revision_id, item.instrument_id, item.as_of,
                    item.horizon_minutes, item.expiry_at, item.reference_price,
                    item.threshold_policy_version, item.threshold_value, item.max_future_high,
                    item.min_future_low, item.maximum_up_excursion, item.maximum_down_excursion,
                    item.maximum_favorable_excursion, item.maximum_adverse_excursion,
                    item.close_return_at_horizon, item.realized_range, item.first_barrier,
                    item.first_barrier_at, item.label, item.quality_status)).fetchone()
                inserted += int(row is not None)
        return inserted

    def big_move_labels(self, revision_id: str, instrument_id: int) -> list[BigMoveLabel]:
        with self._connection() as connection:
            rows = connection.execute("""
                SELECT dataset_revision_id, instrument_id, as_of, horizon_minutes, expiry_at,
                       reference_price, threshold_policy_version, threshold_value,
                       max_future_high, min_future_low, maximum_up_excursion,
                       maximum_down_excursion, maximum_favorable_excursion,
                       maximum_adverse_excursion, close_return_at_horizon, realized_range,
                       first_barrier, first_barrier_at, label, quality_status
                FROM big_move_labels WHERE dataset_revision_id=%s AND instrument_id=%s
                ORDER BY as_of, horizon_minutes
            """, (revision_id, instrument_id)).fetchall()
        return [BigMoveLabel(str(row[0]), *row[1:]) for row in rows]

    def save_feature_set(self, definition: FeatureSetDefinition) -> None:
        with self._connection() as connection:
            row = connection.execute(
                """
                INSERT INTO feature_set_definitions(version, created_at, definition, checksum)
                VALUES (%s,%s,%s::jsonb,%s)
                ON CONFLICT(version) DO NOTHING RETURNING checksum
                """,
                (
                    definition.version, definition.created_at,
                    canonical_json(definition.definition), definition.checksum,
                ),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT checksum FROM feature_set_definitions WHERE version=%s",
                    (definition.version,),
                ).fetchone()
        if row[0] != definition.checksum:
            raise ValueError("feature-set versions are immutable")

    def feature_sets_like(self, prefix: str, limit: int = 50) -> list[FeatureSetDefinition]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT version, created_at, definition, checksum
                FROM feature_set_definitions
                WHERE version LIKE %s
                ORDER BY created_at DESC, version DESC
                LIMIT %s
                """,
                (prefix + "%", int(limit)),
            ).fetchall()
        return [FeatureSetDefinition(str(row[0]), row[1], dict(row[2]), str(row[3]))
                for row in rows]

    def save_feature_rows(self, rows: list[FeatureRow]) -> int:
        inserted = 0
        with self._connection() as connection:
            for item in rows:
                row = connection.execute(
                    """
                    INSERT INTO feature_rows(
                      dataset_revision_id, instrument_id, as_of, feature_set_version,
                      source_max_timestamp, feature_values, quality_status
                    ) VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s)
                    ON CONFLICT DO NOTHING RETURNING 1
                    """,
                    (
                        item.dataset_revision_id, item.instrument_id, item.as_of,
                        item.feature_set_version, item.source_max_timestamp,
                        canonical_json(item.values), item.quality_status,
                    ),
                ).fetchone()
                if row is None and '_contextV3' in item.values:
                    stored=connection.execute('''SELECT source_max_timestamp,feature_values,quality_status FROM feature_rows
                        WHERE dataset_revision_id=%s AND instrument_id=%s AND as_of=%s AND feature_set_version=%s''',
                        (item.dataset_revision_id,item.instrument_id,item.as_of,item.feature_set_version)).fetchone()
                    if not stored or stored[0]!=item.source_max_timestamp or stored[1]!=item.values or stored[2]!=item.quality_status:
                        raise ValueError('IMMUTABLE_V3_ROW_CONFLICT')
                inserted += 1 if row else 0
        return inserted

    def feature_rows(self, revision_id: str, instrument_id: int, version: str) -> list[FeatureRow]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT dataset_revision_id, instrument_id, as_of, feature_set_version,
                       source_max_timestamp, feature_values, quality_status
                FROM feature_rows
                WHERE dataset_revision_id=%s AND instrument_id=%s AND feature_set_version=%s
                ORDER BY as_of
                """,
                (revision_id, instrument_id, version),
            ).fetchall()
        result = []
        for row in rows:
            values = dict(row[5])
            for key, value in list(values.items()):
                if '_contextV3' not in values and value is not None and (
                    key.startswith("return_") or "pct" in key or key.startswith("rolling_")
                ):
                    values[key] = Decimal(str(value))
            result.append(FeatureRow(str(row[0]), row[1], row[2], row[3], row[4], values, row[6]))
        return result

    def save_baseline_definition(self, definition: BaselineDefinition) -> None:
        with self._connection() as connection:
            row = connection.execute(
                """
                INSERT INTO baseline_definitions(version, kind, horizon_minutes, config, checksum)
                VALUES (%s,%s,%s,%s::jsonb,%s)
                ON CONFLICT(version) DO NOTHING RETURNING checksum
                """,
                (
                    definition.version, definition.kind, definition.horizon_minutes,
                    canonical_json(definition.config), definition.checksum,
                ),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT checksum FROM baseline_definitions WHERE version=%s",
                    (definition.version,),
                ).fetchone()
        if row[0] != definition.checksum:
            raise ValueError("baseline versions are immutable")

    def save_research_run(self, run_id: str, manifest: dict[str, Any], checksum: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO research_runs(id, manifest, manifest_checksum)
                VALUES (%s,%s::jsonb,%s) ON CONFLICT DO NOTHING
                """,
                (run_id, canonical_json(manifest), checksum),
            )

    def save_fold(self, run_id: str, fold_id: str, values: dict[str, Any]) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO walk_forward_folds(
                  id, run_id, test_date, train_start, train_end, purge_minutes, definition
                ) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT DO NOTHING
                """,
                (
                    fold_id, run_id, values["test_date"], values["train_start"],
                    values["train_end"], values["purge_minutes"], canonical_json(values),
                ),
            )

    def save_predictions(self, predictions: list[ResearchPrediction]) -> int:
        inserted = 0
        with self._connection() as connection:
            for item in predictions:
                row = connection.execute(
                    """
                    INSERT INTO research_predictions(
                      id, run_id, fold_id, baseline_version, feature_set_version,
                      dataset_revision_id, instrument_id, horizon_minutes, as_of,
                      expires_at, predicted_class, confidence, prediction_reason,
                      created_replay_clock
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT DO NOTHING RETURNING 1
                    """,
                    (
                        item.id, item.run_id, item.fold_id, item.baseline_version,
                        item.feature_set_version, item.dataset_revision_id,
                        item.instrument_id, item.horizon_minutes, item.as_of,
                        item.expires_at, item.predicted_class, item.confidence,
                        item.prediction_reason, item.created_replay_clock,
                    ),
                ).fetchone()
                inserted += 1 if row else 0
        return inserted

    def predictions_for_run(self, run_id: str) -> list[ResearchPrediction]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, run_id, fold_id, baseline_version, feature_set_version,
                       dataset_revision_id, instrument_id, horizon_minutes, as_of,
                       expires_at, predicted_class, confidence, prediction_reason,
                       created_replay_clock
                FROM research_predictions WHERE run_id=%s
                ORDER BY as_of, baseline_version, horizon_minutes
                """,
                (run_id,),
            ).fetchall()
        return [ResearchPrediction(*row) for row in rows]

    def append_outcome(self, outcome: PredictionOutcome) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                """
                INSERT INTO prediction_outcomes(
                  prediction_id, attached_replay_clock, actual_class, quality_status
                ) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING 1
                """,
                (
                    outcome.prediction_id, outcome.attached_replay_clock,
                    outcome.actual_class, outcome.quality_status,
                ),
            ).fetchone()
        return row is not None

    def outcomes_for_run(self, run_id: str) -> list[PredictionOutcome]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT outcome.prediction_id, outcome.attached_replay_clock,
                       outcome.actual_class, outcome.quality_status
                FROM prediction_outcomes outcome
                JOIN research_predictions prediction ON prediction.id=outcome.prediction_id
                WHERE prediction.run_id=%s ORDER BY outcome.prediction_id
                """,
                (run_id,),
            ).fetchall()
        return [PredictionOutcome(*row) for row in rows]

    def save_run_report(self, run_id: str, report: dict[str, Any], checksum: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO evaluation_report_manifests(run_id, report, checksum)
                VALUES (%s,%s::jsonb,%s) ON CONFLICT DO NOTHING
                """,
                (run_id, canonical_json(report), checksum),
            )

    def run_report(self, run_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT report FROM evaluation_report_manifests WHERE run_id=%s",
                (run_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"Unknown research run report: {run_id}")
        return row[0]

    def save_model_definition(self, manifest: dict[str, Any]) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                """
                INSERT INTO model_definitions(
                  model_checksum, algorithm, horizon_minutes, model_version,
                  dataset_revision_id, feature_set_version, artifact_path,
                  artifact_checksum, manifest
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                ON CONFLICT DO NOTHING RETURNING 1
                """,
                (
                    manifest["model_checksum"], manifest["algorithm"],
                    manifest["horizon_minutes"], manifest["model_version"],
                    manifest["dataset_revision_id"], manifest["feature_set_version"],
                    manifest["artifact_path"], manifest["artifact_checksum"],
                    canonical_json(manifest),
                ),
            ).fetchone()
        return row is not None

    def save_training_manifest(self, manifest: dict[str, Any]) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                """
                INSERT INTO training_runs(
                  training_manifest_checksum, dataset_revision_id, horizon_minutes,
                  selected_model_checksum, manifest
                ) VALUES (%s,%s,%s,%s,%s::jsonb)
                ON CONFLICT DO NOTHING RETURNING 1
                """,
                (
                    manifest["training_manifest_checksum"], manifest["dataset_revision_id"],
                    manifest["horizon_minutes"], manifest["selected_model_checksum"],
                    canonical_json(manifest),
                ),
            ).fetchone()
        return row is not None

    def save_model_bundle(self, bundle: dict[str, Any]) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                """
                INSERT INTO model_bundles(bundle_checksum, dataset_revision_id, status, definition)
                VALUES (%s,%s,%s,%s::jsonb) ON CONFLICT DO NOTHING RETURNING 1
                """,
                (
                    bundle["bundle_checksum"], bundle["dataset_revision_id"],
                    bundle["status"], canonical_json(bundle),
                ),
            ).fetchone()
            if row:
                for horizon, member in bundle["models"].items():
                    connection.execute(
                        """
                        INSERT INTO model_bundle_members(bundle_checksum, horizon_minutes, model_checksum)
                        VALUES (%s,%s,%s)
                        """,
                        (bundle["bundle_checksum"], int(horizon), member["model_checksum"]),
                    )
        return row is not None

    def save_calibration_run(self, calibration: dict[str, Any]) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                """
                INSERT INTO calibration_runs(
                  calibration_checksum, model_checksum, horizon_minutes, method,
                  version, parameters, metrics, rejection_reasons
                ) VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb)
                ON CONFLICT DO NOTHING RETURNING 1
                """,
                (
                    calibration["checksum"], calibration["model_checksum"],
                    calibration["horizon_minutes"], calibration["method"],
                    calibration["version"], canonical_json(calibration["parameters"]),
                    canonical_json({
                        "raw_selection": calibration["raw_selection_metrics"],
                        "calibrated_selection": calibration["calibrated_selection_metrics"],
                        "fit_range": calibration["fit_range"],
                        "selection_range": calibration["selection_range"],
                    }),
                    canonical_json(calibration["rejection_reasons"]),
                ),
            ).fetchone()
            if row:
                for bucket in calibration["reliability_buckets"]:
                    connection.execute(
                        """
                        INSERT INTO calibration_reliability_buckets(
                          calibration_checksum, bucket_index, lower_bound, upper_bound,
                          sample_count, mean_confidence, observed_accuracy,
                          directional_count, observed_directional_precision
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (
                            calibration["checksum"], bucket["bucket_index"],
                            bucket["lower"], bucket["upper"], bucket["count"],
                            bucket["mean_confidence"], bucket["observed_accuracy"],
                            bucket["directional_count"],
                            bucket["observed_directional_precision"],
                        ),
                    )
        return row is not None

    def save_abstention_policy(
        self, version: str, checksum: str, definition: dict[str, Any]
    ) -> bool:
        with self._connection() as connection:
            existing = connection.execute(
                "SELECT policy_checksum FROM abstention_policies WHERE version=%s",
                (version,),
            ).fetchone()
            if existing and existing[0] != checksum:
                raise ValueError("abstention policy version is immutable")
            row = connection.execute(
                """
                INSERT INTO abstention_policies(policy_checksum, version, definition)
                VALUES (%s,%s,%s::jsonb) ON CONFLICT DO NOTHING RETURNING 1
                """,
                (checksum, version, canonical_json(definition)),
            ).fetchone()
        return row is not None

    def save_ml_predictions(self, records: list[MLPrediction]) -> int:
        inserted = 0
        with self._connection() as connection:
            for item in records:
                validate_prediction(item)
                row = connection.execute(
                    """
                    INSERT INTO ml_predictions(
                      prediction_id, dataset_revision_id, feature_set_version,
                      model_checksum, calibration_checksum, policy_checksum,
                      training_manifest_checksum, fold_id, instrument_id,
                      horizon_minutes, as_of, expires_at, raw_probabilities,
                      calibrated_probabilities, raw_class, calibrated_class,
                      final_class, system_abstained, abstention_reasons,
                      reliability_bucket, created_replay_clock
                    ) VALUES (
                      %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,
                      %s,%s,%s,%s,%s::jsonb,%s::jsonb,%s
                    ) ON CONFLICT DO NOTHING RETURNING 1
                    """,
                    (
                        item.id, item.dataset_revision_id, item.feature_set_version,
                        item.model_checksum, item.calibration_checksum, item.policy_checksum,
                        item.training_manifest_checksum, item.fold_id, item.instrument_id,
                        item.horizon_minutes, item.as_of, item.expires_at,
                        canonical_json(item.raw_probabilities),
                        canonical_json(item.calibrated_probabilities),
                        item.raw_class, item.calibrated_class, item.final_class,
                        item.system_abstained,
                        canonical_json(item.abstention_reasons),
                        canonical_json(item.reliability_bucket),
                        item.created_replay_clock,
                    ),
                ).fetchone()
                inserted += 1 if row else 0
        return inserted

    def append_ml_prediction_outcome(self, outcome: MLPredictionOutcome) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                """
                INSERT INTO ml_prediction_outcomes(
                  prediction_id, actual_class, quality_status, attached_replay_clock
                ) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING 1
                """,
                (
                    outcome.prediction_id, outcome.actual_class,
                    outcome.quality_status, outcome.attached_replay_clock,
                ),
            ).fetchone()
        return row is not None

    def save_model_comparison(self, comparison: dict[str, Any]) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                """
                INSERT INTO model_comparisons(
                  comparison_checksum, training_manifest_checksum, results
                ) VALUES (%s,%s,%s::jsonb)
                ON CONFLICT DO NOTHING RETURNING 1
                """,
                (
                    comparison["comparison_checksum"],
                    comparison["training_manifest_checksum"],
                    canonical_json(comparison["results"]),
                ),
            ).fetchone()
        return row is not None
