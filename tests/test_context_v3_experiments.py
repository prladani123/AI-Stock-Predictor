from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from banknifty_foundation.big_move_features import FEATURE_SET_VERSION_V2, big_move_feature_definition
from banknifty_foundation.big_move_labels import BigMoveLabel, BigMoveThresholdPolicy
from banknifty_foundation.calendar import IST
from banknifty_foundation.context_v3_experiments import ContextV3ExperimentService
from banknifty_foundation.repository import digest
from banknifty_foundation.research_models import FeatureRow, FeatureSetDefinition

BASE = datetime(2026, 1, 5, 4, 0, tzinfo=timezone.utc)
PROFILE = 'banknifty-price-vol-v3'
PIN_VERSION = 'context-v3-inputs-test'
BASE_NAMES = [name for group in ('price_features', 'futures_features', 'option_features')
              for name in big_move_feature_definition()[group] if name != 'futures_buildup'][:8]


class FakeRepository:
    def __init__(self):
        self.revision = SimpleNamespace(status='ACCEPTED', checksum='revision-checksum',
                                        instrument_ids=[7])
        self.rows = {}
        self.labels = []
        self.feature_sets = {}

    def get_revision(self, revision_id):
        return self.revision

    def feature_rows(self, revision_id, instrument_id, version):
        return self.rows.get(version, [])

    def big_move_labels(self, revision_id, instrument_id):
        return self.labels

    def big_move_policy(self, version):
        return BigMoveThresholdPolicy(version, {h: Decimal('0.001') for h in (10, 20, 30)},
                                      {h: Decimal('0') for h in (10, 20, 30)})

    def save_feature_set(self, definition):
        current = self.feature_sets.get(definition.version)
        if current and current.checksum != definition.checksum:
            raise ValueError('feature-set versions are immutable')
        self.feature_sets.setdefault(definition.version, definition)

    def feature_sets_like(self, prefix, limit=50):
        return [value for key, value in sorted(self.feature_sets.items(), reverse=True)
                if key.startswith(prefix)][:limit]


def _fixture(shift_candidate_seconds=0, days=20, minutes=24):
    repository = FakeRepository()
    manifest = {'kind': 'CONTEXT_V3_GENERATION_PINS', 'profileVersion': PROFILE,
                'datasetRevisionId': 'revision', 'instrumentId': 7, 'pins': {}}
    checksum = digest(manifest)
    repository.feature_sets[PIN_VERSION] = FeatureSetDefinition(
        PIN_VERSION, datetime.now(IST), manifest, checksum)
    baseline, candidates = [], []
    for day in range(days):
        for minute in range(minutes):
            reference = BASE + timedelta(days=day, minutes=minute)
            signal = ((day * minutes + minute) % 17) / 8 - 1
            values = {name: signal + index / 100 for index, name in enumerate(BASE_NAMES)}
            baseline.append(FeatureRow('revision', 7, reference, FEATURE_SET_VERSION_V2,
                                       reference, values, 'PASS'))
            label = 'BIG_UP' if signal > .35 else 'BIG_DOWN' if signal < -.35 else 'NO_BIG_MOVE'
            excursion = Decimal('0.006') if label != 'NO_BIG_MOVE' else Decimal('0')
            for horizon in (10, 20, 30):
                repository.labels.append(BigMoveLabel(
                    'revision', 7, reference, horizon, reference + timedelta(minutes=horizon),
                    Decimal('100'), 'policy', Decimal('0.001'), None, None,
                    excursion, excursion, excursion, excursion, None, excursion, None, None,
                    label, 'VALID'))
            identity = int((reference - BASE).total_seconds() // 60)
            context = {
                'price_vol__signal': signal,
                'price_vol__signal_cubed': signal ** 3,
                'price_vol__identity': identity,
                'session_structure__crosses_cas_boundary_10m': 0,
                'session_structure__crosses_cas_boundary_20m': 0,
                'session_structure__crosses_cas_boundary_30m': 0,
                'session_structure__crosses_index_close_10m': 0,
                'session_structure__crosses_index_close_20m': 0,
                'session_structure__crosses_index_close_30m': 0,
            }
            context['_contextV3'] = {
                'status': 'PASS', 'mode': 'RETROSPECTIVE_PROVIDER_TIMESTAMP',
                'profileChecksum': 'profile-checksum',
                'referenceMinute': (reference + timedelta(seconds=shift_candidate_seconds)).isoformat(),
                'horizonEligibility': {'10': True, '20': True, '30': True},
                'blocks': {},
                'pinnedLineage': {'inputManifestVersion': PIN_VERSION,
                                  'inputManifestChecksum': checksum},
            }
            candidates.append(FeatureRow('revision', 7, reference + timedelta(minutes=1),
                                         PROFILE, reference + timedelta(minutes=1),
                                         context, 'PASS'))
    repository.rows[FEATURE_SET_VERSION_V2] = baseline
    repository.rows[PROFILE] = candidates
    return repository


def test_exact_reference_minute_never_uses_previous_candidate_as_of():
    repository = _fixture()
    service = ContextV3ExperimentService(repository)
    revision, baseline, candidates, labels = service._load('revision', 7, PROFILE)
    rows, exclusions = service._exact_cohort(baseline, candidates, labels, 10)
    assert rows
    target = BASE + timedelta(days=2, minutes=13)
    row = next(item for item in rows if item['reference'] == target)
    assert row['candidate'].values['price_vol__identity'] == int((target - BASE).total_seconds() // 60)
    assert row['candidate'].values['_contextV3']['referenceMinute'] == target.isoformat()
    assert row['candidate'].as_of == target + timedelta(minutes=1)
    assert all(item['candidate'].values['_contextV3']['referenceMinute'] ==
               item['reference'].isoformat() for item in rows)
    assert not exclusions.get('EXACT_REFERENCE_VALIDATION_FAILED')


def test_reference_mismatch_is_not_imputed_or_joined():
    repository = _fixture(shift_candidate_seconds=30)
    result = ContextV3ExperimentService(repository).run(
        'revision', 7, PROFILE, horizons=(10,), algorithms=('LOGISTIC_REGRESSION',),
        minimum_training_days=5, holdout_days=5, persist=False)
    manifest = result['manifest']
    assert manifest['classification'] == 'INSUFFICIENT_COVERAGE'
    assert manifest['horizons']['10']['cohort']['commonSupportRows'] == 0
    assert result['stored'] is False


def test_walk_forward_separates_calibration_threshold_and_final_holdout():
    repository = _fixture()
    result = ContextV3ExperimentService(repository).run(
        'revision', 7, PROFILE, horizons=(10,), algorithms=('LOGISTIC_REGRESSION',),
        minimum_training_days=5, holdout_days=5, evaluation_step_days=1, persist=False)
    horizon = result['manifest']['horizons']['10']
    run = horizon['algorithms']['LOGISTIC_REGRESSION']
    assert run['status'] == 'EVALUATED'
    assert run['aggregate'] is not None
    assert run['exactFinalHoldout'] is not None
    assert run['bootstrap']['status'] == 'PASS'
    assert run['classification'] in {'V3_BETTER', 'V3_WORSE', 'NO_CLEAR_IMPROVEMENT',
                                     'INSUFFICIENT_EVIDENCE', 'INSUFFICIENT_COVERAGE'}
    assert run['folds'][-1]['finalHoldout'] is True
    assert sum(1 for fold in run['folds'] if fold['finalHoldout']) == 1
    for fold in run['folds']:
        assert fold['status'] == 'EVALUATED'
        partitions = fold['partitions']
        assert partitions['calibrationFitDate'] < partitions['calibrationSelectionDate']
        assert partitions['calibrationSelectionDate'] < partitions['thresholdSelectionDate']
        assert max(partitions['estimatorDates']) < partitions['calibrationFitDate']
        assert partitions['thresholdSelectionDate'] < min(partitions['evaluationDates'])
        assert fold['schemas']['candidate'][:len(fold['schemas']['baseline'])] == fold['schemas']['baseline']
        assert 'price_vol__signal' in fold['schemas']['candidateContext']
        assert '_contextV3' not in fold['schemas']['candidate']
        assert fold['rowCounts']['estimator'] == fold['rowCounts']['calibrationFit'] * 5
        assert fold['thresholds']['baseline']['value'] is not None
        assert fold['thresholds']['candidate']['value'] is not None


def test_experiment_identity_is_deterministic_and_idempotent():
    repository = _fixture()
    service = ContextV3ExperimentService(repository)
    first = service.run('revision', 7, PROFILE, horizons=(10,),
                        algorithms=('LOGISTIC_REGRESSION',), persist=True)
    second = service.run('revision', 7, PROFILE, horizons=(10,),
                         algorithms=('LOGISTIC_REGRESSION',), persist=True)
    assert first['manifest']['checksum'] == second['manifest']['checksum']
    assert first['manifest']['experimentId'] == second['manifest']['experimentId']
    assert len(repository.feature_sets) == 2
    stored = service.stored()
    assert stored['count'] == 1
    assert stored['experiments'][0]['runtimeImpact'] == 'NONE'
    assert stored['experiments'][0]['productionArtifactsModified'] is False
    fetched = service.get(first['manifest']['experimentId'])
    assert fetched['available'] is True
    assert fetched['manifest']['contextGeneration']['checksum']


def test_ambiguous_generation_manifest_fails_closed():
    repository = _fixture()
    candidate = repository.rows[PROFILE][0]
    candidate.values['_contextV3']['pinnedLineage']['inputManifestChecksum'] = 'other-checksum'
    with pytest.raises(ValueError, match='AMBIGUOUS_CONTEXT_V3_GENERATION_MANIFEST'):
        ContextV3ExperimentService(repository).run(
            'revision', 7, PROFILE, horizons=(10,), algorithms=('LOGISTIC_REGRESSION',),
            persist=False)
