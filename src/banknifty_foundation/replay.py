from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from .baselines import MajorityBaseline, PersistenceBaseline
from .calendar import IST, TradingCalendar
from .evaluation import classification_metrics, grouped_metrics
from .models import TargetLabel
from .repository import Repository, digest
from .research_models import (
    FeatureRow,
    PredictionOutcome,
    ReplayManifest,
    ResearchPrediction,
)


class AsOfDataView:
    """The only replay read boundary; future reads fail closed."""

    def __init__(
        self,
        clock: datetime,
        features: list[FeatureRow],
        training_labels: list[TargetLabel],
        outcome_labels: dict[tuple[datetime, int], TargetLabel],
    ) -> None:
        self.clock = clock
        self._features = features
        self._training_labels = training_labels
        self._outcome_labels = outcome_labels

    def feature_at(self, requested_at: datetime) -> FeatureRow | None:
        if requested_at > self.clock:
            raise ValueError("AsOfDataView rejects feature queries after the replay clock")
        return next((item for item in self._features if item.as_of == requested_at), None)

    def training_labels(self) -> tuple[TargetLabel, ...]:
        return tuple(self._training_labels)

    def outcome(self, as_of: datetime, horizon: int) -> TargetLabel | None:
        expiry = as_of + timedelta(minutes=horizon)
        if expiry > self.clock:
            raise ValueError("prediction outcome is not visible before expiry")
        return self._outcome_labels.get((as_of, horizon))


def _manifest_checksum(manifest: ReplayManifest) -> str:
    return digest(manifest.to_dict())


class WalkForwardReplay:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def run(self, manifest: ReplayManifest) -> tuple[str, dict[str, Any]]:
        if manifest.training_mode not in {"EXPANDING", "ROLLING"}:
            raise ValueError("training mode must be EXPANDING or ROLLING")
        if manifest.training_mode == "ROLLING" and (
            manifest.rolling_days is None or manifest.rolling_days < 1
        ):
            raise ValueError("rolling mode requires a positive rolling_days")
        if manifest.purge_minutes < 20:
            raise ValueError("walk-forward purge must be at least 20 minutes")

        revision = self.repository.get_revision(manifest.dataset_revision_id)
        if revision.status != "ACCEPTED":
            raise ValueError("walk-forward replay requires an ACCEPTED revision")
        checksum = _manifest_checksum(manifest)
        run_id = f"run-{checksum[:32]}"
        fold_id = f"fold-{manifest.test_date.isoformat()}"
        self.repository.save_research_run(run_id, manifest.to_dict(), checksum)

        all_features = self.repository.feature_rows(
            manifest.dataset_revision_id,
            manifest.instrument_id,
            manifest.feature_set_version,
        )
        all_labels = self.repository.target_labels(
            manifest.dataset_revision_id, manifest.instrument_id
        )
        test_features = [
            item for item in all_features if item.as_of.astimezone(IST).date() == manifest.test_date
        ]
        if not test_features:
            raise ValueError("no feature rows exist for the unseen test date")

        test_open = min(item.as_of for item in test_features)
        purge_boundary = test_open - timedelta(minutes=manifest.purge_minutes)
        training_labels = [
            item
            for item in all_labels
            if item.as_of.astimezone(IST).date() < manifest.test_date
            and item.expiry_at <= purge_boundary
            and item.label in {"UP", "DOWN", "NO_TRADE"}
        ]
        training_dates = sorted({item.as_of.astimezone(IST).date() for item in training_labels})
        if manifest.training_mode == "ROLLING":
            allowed = set(training_dates[-int(manifest.rolling_days or 0):])
            training_labels = [
                item for item in training_labels if item.as_of.astimezone(IST).date() in allowed
            ]
            training_dates = sorted(allowed)
        if manifest.test_date in training_dates:
            raise AssertionError("test day entered training data")

        fold_values = {
            "training_dates": [item.isoformat() for item in training_dates],
            "test_date": manifest.test_date.isoformat(),
            "purge_minutes": manifest.purge_minutes,
            "training_mode": manifest.training_mode,
        }
        self.repository.save_fold(run_id, fold_id, fold_values)

        baselines: dict[int, list] = {}
        for horizon in (5, 10, 15, 20):
            labels = [item.label for item in training_labels if item.horizon_minutes == horizon]
            models = [
                MajorityBaseline(horizon, labels),
                PersistenceBaseline(
                    horizon,
                    manifest.persistence_return_minutes,
                    manifest.persistence_threshold,
                ),
            ]
            for model in models:
                self.repository.save_baseline_definition(model.definition)
            baselines[horizon] = models

        sessions = {
            item.session_date: item
            for item in self.repository.sessions_between(manifest.test_date, manifest.test_date)
        }
        session = sessions.get(manifest.test_date)
        if session is None or session.status not in {"OPEN", "SPECIAL"}:
            raise ValueError("test date lacks an explicit continuous session")
        outcome_labels = {
            (item.as_of, item.horizon_minutes): item
            for item in all_labels
            if item.as_of.astimezone(IST).date() == manifest.test_date
        }

        pending: dict[str, ResearchPrediction] = {}
        for clock in TradingCalendar.expected_minutes(session):
            view = AsOfDataView(clock, test_features, training_labels, outcome_labels)
            row = view.feature_at(clock)
            if row is not None:
                for horizon, models in baselines.items():
                    expiry = clock + timedelta(minutes=horizon)
                    for model in models:
                        predicted, confidence, reason = model.predict(row)
                        identity = {
                            "run": run_id,
                            "fold": fold_id,
                            "baseline": model.definition.version,
                            "as_of": clock,
                            "horizon": horizon,
                        }
                        prediction = ResearchPrediction(
                            id=f"pred-{digest(identity)[:32]}",
                            run_id=run_id,
                            fold_id=fold_id,
                            baseline_version=model.definition.version,
                            feature_set_version=manifest.feature_set_version,
                            dataset_revision_id=manifest.dataset_revision_id,
                            instrument_id=manifest.instrument_id,
                            horizon_minutes=horizon,
                            as_of=clock,
                            expires_at=expiry,
                            predicted_class=predicted,
                            confidence=confidence,
                            prediction_reason=reason,
                            created_replay_clock=clock,
                        )
                        self.repository.save_predictions([prediction])
                        pending[prediction.id] = prediction
            for prediction in list(pending.values()):
                if prediction.expires_at > clock:
                    continue
                label = view.outcome(prediction.as_of, prediction.horizon_minutes)
                if label is not None:
                    self.repository.append_outcome(
                        PredictionOutcome(
                            prediction.id,
                            clock,
                            label.label,
                            label.quality_status,
                        )
                    )
                del pending[prediction.id]

        predictions = self.repository.predictions_for_run(run_id)
        outcomes = self.repository.outcomes_for_run(run_id)
        by_baseline: dict[str, Any] = {}
        for kind in ("majority", "persistence"):
            selected = [
                item for item in predictions if item.baseline_version.startswith(kind)
            ]
            selected_ids = {item.id for item in selected}
            selected_outcomes = [item for item in outcomes if item.prediction_id in selected_ids]
            by_baseline[kind] = classification_metrics(selected, selected_outcomes)
        report = {
            "schema_version": "walk-forward-report-v1",
            "run_id": run_id,
            "manifest": manifest.to_dict(),
            "manifest_checksum": checksum,
            "fold": fold_values,
            "metrics": classification_metrics(predictions, outcomes),
            "by_horizon": grouped_metrics(predictions, outcomes, lambda item: item.horizon_minutes),
            "by_hour": grouped_metrics(
                predictions, outcomes, lambda item: item.as_of.astimezone(IST).hour
            ),
            "by_date": grouped_metrics(
                predictions, outcomes, lambda item: item.as_of.astimezone(IST).date().isoformat()
            ),
            "baseline_comparison": by_baseline,
        }
        self.repository.save_run_report(run_id, report, digest(report))
        return run_id, report
