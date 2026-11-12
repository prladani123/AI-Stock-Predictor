"""Strictly paired, chronological V2 versus V2 + Context V3 experiments.

This module is research-only. It never saves a production artifact, approves a
model, changes a runtime probability or writes to a production model registry.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from math import isfinite
from random import Random
import re
from statistics import mean

import numpy as np
from sklearn.metrics import (accuracy_score, average_precision_score, f1_score,
                             log_loss, precision_score, recall_score, roc_auc_score)

from .big_move_evaluation import EvaluationConfiguration, _calibrate
from .big_move_features import FEATURE_SET_VERSION_V2, big_move_feature_definition
from .big_move_labels import BIG_MOVE_HORIZONS
from .big_move_models import train_two_stage_model
from .big_move_runtime import policy_payload
from .calendar import IST
from .context_v3 import PROFILES, _hash, _stamp, common_support_indices, profile_definition
from .repository import digest
from .research_models import FeatureSetDefinition

IMPLEMENTATION_VERSION = 'context-v3-experiment-v2'
EXPERIMENT_PREFIX = 'context-v3-experiment-'
ALGORITHMS = ('LOGISTIC_REGRESSION', 'LIGHTGBM')
BLOCK_PREFIXES = tuple(f'{name.lower()}__' for name in (
    'PRICE_VOL', 'INDIA_MARKET', 'GLOBAL', 'DERIVATIVES', 'BANK_STRUCTURE', 'SESSION_STRUCTURE'))
MIN_COMMON_SUPPORT_FRACTION = .70
MIN_EVALUATION_ROWS = 100
MIN_EVALUATION_DAYS = 5
MIN_FOLD_ROWS = 20
MIN_CLASS_ROWS = 10
CALIBRATION_DAYS = 3
MIN_BOOTSTRAP_DAYS = 5
BOOTSTRAP_SAMPLES = 1000
BOOTSTRAP_SEED = 1729
MATERIAL_BRIER_REGRESSION = -.002
MATERIAL_LOG_LOSS_REGRESSION = -.01
MATERIAL_DIRECTION_REGRESSION = -.02
MATERIAL_JOINT_REGRESSION = -.02
CLASSES = ('BIG_UP', 'BIG_DOWN', 'NO_BIG_MOVE')


def _finite(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if isfinite(result) else None


def _day(stamp):
    return _stamp(stamp).astimezone(IST).date()


def _value(item, name):
    source = item['candidate'] if name.startswith(BLOCK_PREFIXES) else item['baseline']
    return _finite(source.values.get(name))


def _complete(item, schema):
    return item is not None and all(_value(item, name) is not None for name in schema)


def _schema(rows, names, *, minimum_coverage=.90):
    if not rows:
        return [], {}
    coverage = {name: sum(_value(item, name) is not None for item in rows) / len(rows)
                for name in names}
    schema = []
    for name in names:
        if coverage[name] < minimum_coverage:
            continue
        values = [_value(item, name) for item in rows]
        values = [value for value in values if value is not None]
        if len(values) > 1 and max(values) != min(values):
            schema.append(name)
    return schema, coverage


def _context_names(rows):
    names = set()
    for row in rows:
        for key, value in row.values.items():
            if key.startswith(BLOCK_PREFIXES) and _finite(value) is not None:
                names.add(key)
    return sorted(names)


def _binary_probability_metrics(actual, probabilities):
    actual = np.asarray(actual, dtype=int)
    probabilities = np.asarray(probabilities, dtype=float)
    both = len(set(actual.tolist())) == 2
    result = {
        'rows': len(actual),
        'classCounts': {'negative': int((actual == 0).sum()), 'positive': int(actual.sum())},
        'brier': float(np.mean((probabilities - actual) ** 2)) if len(actual) else None,
        'logLoss': None,
        'rocAuc': None,
        'prAuc': None,
        'actualRate': float(actual.mean()) if len(actual) else None,
    }
    if len(actual):
        try:
            result['logLoss'] = float(log_loss(
                actual, np.clip(probabilities, 1e-9, 1 - 1e-9), labels=[0, 1]))
        except ValueError:
            result['logLoss'] = None
    if both:
        result['rocAuc'] = float(roc_auc_score(actual, probabilities))
        result['prAuc'] = float(average_precision_score(actual, probabilities))
    return result


def _direction_metrics(actual_labels, up_probabilities):
    rows = [(label, probability) for label, probability in zip(actual_labels, up_probabilities)
            if label in ('BIG_UP', 'BIG_DOWN')]
    if not rows:
        return {'rows': 0, 'accuracy': None, 'macroF1': None,
                'UP': {'precision': None, 'recall': None, 'f1': None},
                'DOWN': {'precision': None, 'recall': None, 'f1': None}}
    actual = np.asarray([label == 'BIG_UP' for label, _ in rows], dtype=int)
    predicted = np.asarray([probability >= .5 for _, probability in rows], dtype=int)
    return {
        'rows': len(rows),
        'accuracy': float(accuracy_score(actual, predicted)),
        'macroF1': float(f1_score(actual, predicted, average='macro', zero_division=0)),
        'UP': {
            'precision': float(precision_score(actual, predicted, zero_division=0)) if predicted.sum() else None,
            'recall': float(recall_score(actual, predicted, zero_division=0)) if actual.sum() else None,
            'f1': float(f1_score(actual, predicted, zero_division=0)),
        },
        'DOWN': {
            'precision': float(precision_score(1 - actual, 1 - predicted, zero_division=0))
            if (1 - predicted).sum() else None,
            'recall': float(recall_score(1 - actual, 1 - predicted, zero_division=0))
            if (1 - actual).sum() else None,
            'f1': float(f1_score(1 - actual, 1 - predicted, zero_division=0)),
        },
    }


def _metrics(records, *, side, thresholds=None):
    actual_labels = [row['actual'] for row in records]
    actual_move = [label != 'NO_BIG_MOVE' for label in actual_labels]
    probabilities = [row[f'{side}Probability'] for row in records]
    up_probabilities = [row[f'{side}UpProbability'] for row in records]
    if thresholds is None:
        thresholds = [row.get(f'{side}Threshold') for row in records]
    threshold_values = [value for value in thresholds if value is not None]
    usable_thresholds = len(threshold_values) == len(records) and bool(records)
    predicted_move = ([row[f'{side}Probability'] >= threshold for row, threshold in zip(records, thresholds)]
                      if usable_thresholds else None)
    stage_a = _binary_probability_metrics(actual_move, probabilities)
    stage_a.update({
        'accuracy': None, 'precision': None, 'recall': None, 'f1': None,
        'predictedRate': None,
        'threshold': float(mean(threshold_values)) if usable_thresholds and threshold_values else None,
        'thresholdPolicy': 'PER_RECORD_LEARNED_MOVE_THRESHOLD' if usable_thresholds else 'UNAVAILABLE',
    })
    if usable_thresholds:
        predicted = np.asarray(predicted_move, dtype=int)
        actual = np.asarray(actual_move, dtype=int)
        stage_a.update({
            'accuracy': float(accuracy_score(actual, predicted)),
            'precision': float(precision_score(actual, predicted, zero_division=0)) if predicted.sum() else None,
            'recall': float(recall_score(actual, predicted, zero_division=0)) if actual.sum() else None,
            'f1': float(f1_score(actual, predicted, zero_division=0)),
            'predictedRate': float(predicted.mean()),
        })
    stage_b = _direction_metrics(actual_labels, up_probabilities)
    if usable_thresholds:
        predicted_labels = [
            ('BIG_UP' if up >= .5 else 'BIG_DOWN') if is_move else 'NO_BIG_MOVE'
            for is_move, up in zip(predicted_move, up_probabilities)]
        joint = {
            'accuracy': float(accuracy_score(actual_labels, predicted_labels)),
            'macroF1': float(f1_score(actual_labels, predicted_labels, labels=CLASSES,
                                      average='macro', zero_division=0)),
            'perClass': {},
        }
        for label in CLASSES:
            joint['perClass'][label] = {
                'precision': float(precision_score(actual_labels, predicted_labels, labels=[label],
                                                    average=None, zero_division=0)[0]),
                'recall': float(recall_score(actual_labels, predicted_labels, labels=[label],
                                              average=None, zero_division=0)[0]),
                'f1': float(f1_score(actual_labels, predicted_labels, labels=[label],
                                      average=None, zero_division=0)[0]),
            }
    else:
        joint = {'accuracy': None, 'macroF1': None, 'perClass': {
            label: {'precision': None, 'recall': None, 'f1': None} for label in CLASSES}}
    errors = []
    for row in records:
        if row['actual'] not in ('BIG_UP', 'BIG_DOWN'):
            continue
        expected = _finite(row.get(f'{side}ExpectedMagnitude'))
        actual_magnitude = _finite(row.get('actualMagnitude'))
        if expected is not None and actual_magnitude is not None:
            errors.append(abs(expected - actual_magnitude))
    return {
        'stageA': stage_a,
        'stageB': stage_b,
        'joint': joint,
        'magnitude': {
            'rows': len(errors),
            'mae': float(mean(errors)) if errors else None,
            'medianAbsoluteError': float(np.median(errors)) if errors else None,
            'unit': 'return_fraction',
        },
        'rows': len(records),
    }


def _threshold(model, rows):
    if not rows:
        return None, {'status': 'UNAVAILABLE', 'value': None, 'samples': 0,
                      'reason': 'NO_THRESHOLD_SELECTION_ROWS'}
    predictions = []
    for row in rows:
        prediction = model.predict(
            [_value(row, name) for name in model.feature_schema],
            float(row['label'].reference_price))
        predictions.append((row['label'].label, prediction['bigMoveProbability']))
    y = np.asarray([label != 'NO_BIG_MOVE' for label, _ in predictions], dtype=int)
    p = np.asarray([probability for _, probability in predictions], dtype=float)
    choices = []
    if len(y) and set(y.tolist()) == {0, 1}:
        for value in sorted(set(float(v) for v in p)):
            predicted = p >= value
            recall = float(recall_score(y, predicted, zero_division=0))
            coverage = float(predicted.mean())
            if predicted.sum() >= 10 and recall >= .20 and coverage >= .05:
                choices.append((float(precision_score(y, predicted, zero_division=0)),
                                recall, -coverage, value))
    if choices:
        precision, recall, minus_coverage, value = max(choices)
        return value, {'status': 'SELECTED', 'value': value, 'precision': precision,
                       'recall': recall, 'coverage': -minus_coverage, 'samples': len(y)}
    return None, {'status': 'UNAVAILABLE', 'value': None, 'samples': len(y),
                  'reason': 'INSUFFICIENT_THRESHOLD_EVIDENCE'}


def _predict(model, row, side):
    prediction = model.predict(
        [_value(row, name) for name in model.feature_schema],
        float(row['label'].reference_price))
    return {
        f'{side}Probability': prediction['bigMoveProbability'],
        f'{side}UpProbability': prediction['upGivenBigMoveProbability'],
        f'{side}RawProbability': prediction['rawBigMoveProbability'],
        f'{side}RawUpProbability': prediction['rawUpGivenBigMoveProbability'],
        f'{side}ExpectedMagnitude': prediction['expectedMovePercent'],
    }


def _average_precision(actual, probabilities):
    positives = int(actual.sum())
    if not positives or positives == len(actual):
        return None
    order = np.argsort(-probabilities, kind='mergesort')
    ordered = actual[order]
    scores = probabilities[order]
    distinct = np.where(np.diff(scores))[0]
    chosen = np.r_[distinct, len(ordered) - 1]
    true_positive = np.cumsum(ordered)[chosen]
    false_positive = 1 + chosen - true_positive
    if len(false_positive) > 2:
        keep = np.where(np.concatenate((
            [True], np.logical_or(np.diff(true_positive[:-1]), np.diff(true_positive[1:])),
            [True])))[0]
        true_positive = true_positive[keep]
        false_positive = false_positive[keep]
    predicted = true_positive + false_positive
    precision = np.divide(true_positive, predicted, out=np.zeros_like(true_positive, dtype=float),
                          where=predicted != 0)
    recall = (true_positive / true_positive[-1] if true_positive[-1]
              else np.ones_like(true_positive, dtype=float))
    precision = np.hstack((precision[::-1], 1.))
    recall = np.hstack((recall[::-1], 0.))
    return float(max(0., -np.sum(np.diff(recall) * precision[:-1])))


def _bootstrap_arrays(records, side):
    return {
        'move': np.asarray([row['actual'] != 'NO_BIG_MOVE' for row in records], dtype=int),
        'labels': np.asarray([row['actual'] for row in records]),
        'probability': np.asarray([row[f'{side}Probability'] for row in records], dtype=float),
        'upProbability': np.asarray([row[f'{side}UpProbability'] for row in records], dtype=float),
        'threshold': np.asarray([np.nan if row.get(f'{side}Threshold') is None
                                 else row[f'{side}Threshold'] for row in records], dtype=float),
        'expectedMagnitude': np.asarray([np.nan if _finite(row.get(f'{side}ExpectedMagnitude')) is None
                                         else _finite(row.get(f'{side}ExpectedMagnitude'))
                                         for row in records], dtype=float),
        'actualMagnitude': np.asarray([np.nan if _finite(row.get('actualMagnitude')) is None
                                       else _finite(row.get('actualMagnitude'))
                                       for row in records], dtype=float),
    }


def _bootstrap_metrics(arrays, index):
    actual = arrays['move'][index]
    labels = arrays['labels'][index]
    probabilities = arrays['probability'][index]
    clipped = np.clip(probabilities, 1e-9, 1 - 1e-9)
    up = arrays['upProbability'][index]
    threshold = arrays['threshold'][index]
    usable = not np.isnan(threshold).any()
    brier = float(np.mean((probabilities - actual) ** 2))
    loss = float(-np.mean(actual * np.log(clipped) + (1 - actual) * np.log(1 - clipped)))
    pr_auc = _average_precision(actual, probabilities)
    macro_f1 = None
    if usable:
        is_move = probabilities >= threshold
        predicted = np.where(is_move, np.where(up >= .5, 'BIG_UP', 'BIG_DOWN'), 'NO_BIG_MOVE')
        scores = []
        for label in CLASSES:
            true_positive = int(((predicted == label) & (labels == label)).sum())
            false_positive = int(((predicted == label) & (labels != label)).sum())
            false_negative = int(((predicted != label) & (labels == label)).sum())
            denominator = 2 * true_positive + false_positive + false_negative
            scores.append(2 * true_positive / denominator if denominator else 0.)
        macro_f1 = float(mean(scores))
    direction = (labels == 'BIG_UP') | (labels == 'BIG_DOWN')
    accuracy = (float(np.mean((up[direction] >= .5) == (labels[direction] == 'BIG_UP')))
                if direction.any() else None)
    expected = arrays['expectedMagnitude'][index][direction]
    realized = arrays['actualMagnitude'][index][direction]
    valid = ~np.isnan(expected) & ~np.isnan(realized)
    mae = float(np.mean(np.abs(expected[valid] - realized[valid]))) if valid.any() else None
    return {'brier': brier, 'logLoss': loss, 'prAuc': pr_auc, 'jointMacroF1': macro_f1,
            'directionAccuracy': accuracy, 'magnitudeMae': mae}


def _day_bootstrap(records):
    days = sorted({row['day'] for row in records})
    if len(days) < MIN_BOOTSTRAP_DAYS:
        return {'status': 'INSUFFICIENT_EVIDENCE', 'reason': 'INSUFFICIENT_INDEPENDENT_DAYS',
                'days': len(days), 'samples': 0, 'seed': BOOTSTRAP_SEED}
    grouped = {day: np.asarray([index for index, row in enumerate(records) if row['day'] == day])
               for day in days}
    arrays = {side: _bootstrap_arrays(records, side) for side in ('baseline', 'candidate')}
    random = Random(BOOTSTRAP_SEED)
    values = defaultdict(list)
    comparisons = (
        ('brierImprovement', 'brier'), ('logLossImprovement', 'logLoss'),
        ('prAucDelta', 'prAuc'), ('jointMacroF1Delta', 'jointMacroF1'),
        ('directionAccuracyDelta', 'directionAccuracy'), ('magnitudeMaeImprovement', 'magnitudeMae'),
    )
    for _ in range(BOOTSTRAP_SAMPLES):
        index = np.concatenate([grouped[days[random.randrange(len(days))]] for _ in days])
        if not len(index):
            continue
        baseline = _bootstrap_metrics(arrays['baseline'], index)
        candidate = _bootstrap_metrics(arrays['candidate'], index)
        for key, name in comparisons:
            left, right = baseline[name], candidate[name]
            if left is not None and right is not None:
                values[key].append(left - right)

    def interval(items):
        items = sorted(items)
        return {'lower': items[int(.025 * (len(items) - 1))],
                'upper': items[int(.975 * (len(items) - 1))],
                'mean': mean(items)}

    return {
        'status': 'PASS',
        'unit': 'TRADING_DAY_BLOCK',
        'days': len(days),
        'samples': BOOTSTRAP_SAMPLES,
        'seed': BOOTSTRAP_SEED,
        **{key: interval(items) for key, items in values.items() if items},
    }


def _materially_worse(metrics):
    baseline, candidate = metrics['baseline'], metrics['candidate']
    brier = (baseline['stageA']['brier'] - candidate['stageA']['brier']
             if baseline['stageA']['brier'] is not None and candidate['stageA']['brier'] is not None else None)
    loss = (baseline['stageA']['logLoss'] - candidate['stageA']['logLoss']
            if baseline['stageA']['logLoss'] is not None and candidate['stageA']['logLoss'] is not None else None)
    direction = (candidate['stageB']['accuracy'] - baseline['stageB']['accuracy']
                 if baseline['stageB']['accuracy'] is not None and candidate['stageB']['accuracy'] is not None else None)
    joint = (candidate['joint']['macroF1'] - baseline['joint']['macroF1']
             if candidate['joint']['macroF1'] is not None and baseline['joint']['macroF1'] is not None else None)
    return {
        'brierDelta': brier,
        'logLossDelta': loss,
        'directionDelta': direction,
        'jointMacroF1Delta': joint,
        'brierWorse': brier is not None and brier < MATERIAL_BRIER_REGRESSION,
        'logLossWorse': loss is not None and loss < MATERIAL_LOG_LOSS_REGRESSION,
        'directionWorse': direction is not None and direction < MATERIAL_DIRECTION_REGRESSION,
        'jointWorse': joint is not None and joint < MATERIAL_JOINT_REGRESSION,
    }


def _classification(aggregate, final, bootstrap, blockers, coverage_ok, days_ok):
    if not coverage_ok:
        return 'INSUFFICIENT_COVERAGE'
    if blockers or not days_ok or final is None or aggregate is None:
        return 'INSUFFICIENT_EVIDENCE'
    if bootstrap.get('status') != 'PASS':
        return 'INSUFFICIENT_EVIDENCE'
    delta = _materially_worse(aggregate)
    final_delta = _materially_worse(final)
    if delta['brierWorse'] or delta['logLossWorse'] or delta['directionWorse'] or delta['jointWorse']:
        return 'V3_WORSE'
    if final_delta['brierWorse'] or final_delta['jointWorse'] or final_delta['directionWorse']:
        return 'NO_CLEAR_IMPROVEMENT'
    primary_positive = any(
        bootstrap.get(key, {}).get('lower', 0) > 0 for key in ('brierImprovement', 'logLossImprovement'))
    return 'V3_BETTER' if primary_positive else 'NO_CLEAR_IMPROVEMENT'


def _overall_classification(horizons):
    values = [row['classification'] for row in horizons.values()]
    if not values:
        return 'INSUFFICIENT_EVIDENCE'
    if any(value == 'INSUFFICIENT_COVERAGE' for value in values):
        return 'INSUFFICIENT_COVERAGE'
    if 'V3_WORSE' in values:
        return 'V3_WORSE'
    if 'INSUFFICIENT_EVIDENCE' in values:
        return 'INSUFFICIENT_EVIDENCE'
    if all(value == 'V3_BETTER' for value in values):
        return 'V3_BETTER'
    return 'NO_CLEAR_IMPROVEMENT'


class ContextV3ExperimentService:
    """Run only a matched cohort; every fit/calibration/threshold decision uses prior dates."""

    def __init__(self, repository):
        self.repository = repository

    def _load(self, revision_id, instrument_id, profile):
        revision = self.repository.get_revision(revision_id)
        if revision.status != 'ACCEPTED':
            raise ValueError('CONTEXT_V3_EXPERIMENT_REQUIRES_ACCEPTED_REVISION')
        if instrument_id not in revision.instrument_ids:
            raise ValueError('INSTRUMENT_NOT_IN_REVISION')
        baseline = [row for row in self.repository.feature_rows(
            revision_id, instrument_id, FEATURE_SET_VERSION_V2)
            if row.quality_status == 'PASS' and row.source_max_timestamp <= row.as_of]
        candidates = [row for row in self.repository.feature_rows(
            revision_id, instrument_id, profile)
            if row.quality_status == 'PASS' and row.source_max_timestamp <= row.as_of]
        labels = self.repository.big_move_labels(revision_id, instrument_id)
        return revision, baseline, candidates, labels

    def _generation_manifest(self, candidates):
        identities = {}
        evidence_by_identity = {}
        for row in candidates:
            pins = (row.values.get('_contextV3') or {}).get('pinnedLineage') or {}
            version, checksum = pins.get('inputManifestVersion'), pins.get('inputManifestChecksum')
            if not version or not checksum:
                raise ValueError('CONTEXT_V3_GENERATION_MANIFEST_MISSING')
            identity = (row.dataset_revision_id, row.instrument_id, row.feature_set_version, row.as_of)
            fingerprint = digest(row.to_dict())
            if identity in evidence_by_identity and evidence_by_identity[identity] != fingerprint:
                raise ValueError('AMBIGUOUS_CONTEXT_V3_CANDIDATE_EVIDENCE')
            evidence_by_identity[identity] = fingerprint
            identities.setdefault((str(version), str(checksum)), []).append(row)
        if len({version for version, _ in identities}) != len(identities):
            raise ValueError('AMBIGUOUS_CONTEXT_V3_GENERATION_MANIFEST')
        reader = getattr(self.repository, 'feature_sets_like', None)
        if reader is None:
            raise ValueError('CONTEXT_V3_GENERATION_MANIFEST_STORE_UNAVAILABLE')
        mapping = []
        for (version, checksum), references in sorted(identities.items()):
            stored = [item for item in reader(version, 5) if item.version == version]
            if len(stored) != 1 or str(stored[0].checksum) != checksum or digest(stored[0].definition) != checksum:
                raise ValueError('CONTEXT_V3_GENERATION_MANIFEST_MISMATCH')
            definition = stored[0].definition
            dates = set()
            for row in references:
                meta = row.values['_contextV3']
                pins = meta['pinnedLineage']
                day = str(_day(_stamp(meta['referenceMinute'])))
                dates.add(day)
                if (definition.get('kind') != 'CONTEXT_V3_GENERATION_PINS'
                        or str(definition.get('datasetRevisionId')) != str(row.dataset_revision_id)
                        or definition.get('instrumentId') != row.instrument_id
                        or definition.get('profileVersion') != row.feature_set_version):
                    raise ValueError('CONTEXT_V3_GENERATION_MANIFEST_IDENTITY_MISMATCH')
                manifest_pins = definition.get('pins', {})
                for key in ('acceptedDatasetChecksum', 'scheduledConfigurationChecksum', 'marketStructureRulesChecksum'):
                    if manifest_pins.get(key) != pins.get(key):
                        raise ValueError('CONTEXT_V3_GENERATION_LINEAGE_MISMATCH')
                for key in ('researchContextRunIds', 'marketStructureContextRunIds', 'membershipSnapshotChecksums'):
                    if any(manifest_pins.get(key, {}).get(k) != v for k, v in pins.get(key, {}).items()):
                        raise ValueError('CONTEXT_V3_GENERATION_LINEAGE_MISMATCH')
                if definition.get('manifestSchemaVersion') == 2:
                    if (day not in definition.get('generatedDates', [])
                            or definition.get('profileChecksum') != meta.get('profileChecksum')
                            or manifest_pins.get('acceptedDatasetChecksum') != self.repository.get_revision(row.dataset_revision_id).checksum
                            or digest(definition.get('scheduledConfiguration')) != manifest_pins.get('scheduledConfigurationChecksum')
                            or digest(definition.get('marketStructureRules')) != manifest_pins.get('marketStructureRulesChecksum')):
                        raise ValueError('CONTEXT_V3_GENERATION_LINEAGE_MISMATCH')
                    for key in ('researchContextRunIds', 'marketStructureContextRunIds'):
                        expected = {day: manifest_pins[key][day]} if day in manifest_pins.get(key, {}) else {}
                        if pins.get(key, {}) != expected:
                            raise ValueError('CONTEXT_V3_GENERATION_LINEAGE_MISMATCH')
            mapping.append({'version': version, 'checksum': checksum,
                            'referencedRowCount': len(references), 'referencedDateCount': len(dates),
                            'dates': sorted(dates)})
        return {'manifests': mapping, 'checksum': digest(mapping)}

    def _label_policy(self, labels):
        versions = {label.threshold_policy_version for label in labels}
        if len(versions) != 1:
            raise ValueError('AMBIGUOUS_LABEL_POLICY')
        version = next(iter(versions))
        policy = self.repository.big_move_policy(version)
        payload = policy_payload(policy)
        return {'version': version, 'checksum': digest(payload), 'payload': payload}

    @staticmethod
    def _exact_cohort(baseline, candidates, labels, horizon, start=None, end=None):
        support = common_support_indices(baseline, candidates, horizon, labels)
        baseline_by_reference = defaultdict(list)
        for row in baseline:
            baseline_by_reference[(row.dataset_revision_id, row.instrument_id, row.as_of)].append(row)
        candidate_by_reference = defaultdict(list)
        for row in candidates:
            meta = row.values.get('_contextV3') or {}
            if not meta:
                continue
            reference = _stamp(meta['referenceMinute'])
            candidate_by_reference[(row.dataset_revision_id, row.instrument_id, reference)].append(row)
        label_by_reference = defaultdict(list)
        for label in labels:
            if (label.quality_status == 'VALID' and label.label in CLASSES
                    and label.horizon_minutes == horizon):
                label_by_reference[(label.dataset_revision_id, label.instrument_id, label.as_of)].append(label)
        rows, exclusions = [], Counter()
        for item in support:
            reference = _stamp(item['referenceMinute'])
            day = reference.astimezone(IST).date()
            if start and day < start or end and day > end:
                continue
            left_index, right_index = item.get('leftIndex'), item.get('rightIndex')
            if left_index is None or right_index is None:
                exclusions['MISSING_COMMON_SUPPORT_INDEX'] += 1
                continue
            if left_index < 0 or left_index >= len(baseline) or right_index < 0 or right_index >= len(candidates):
                exclusions['INVALID_COMMON_SUPPORT_INDEX'] += 1
                continue
            base, candidate = baseline[left_index], candidates[right_index]
            meta = candidate.values.get('_contextV3') or {}
            candidate_reference = _stamp(meta.get('referenceMinute'))
            identity = (item['datasetRevisionId'], item['instrumentId'], reference)
            if len(baseline_by_reference.get(identity, ())) != 1:
                exclusions['AMBIGUOUS_V2_REFERENCE'] += 1
                continue
            if len(candidate_by_reference.get(identity, ())) != 1:
                exclusions['AMBIGUOUS_V3_REFERENCE'] += 1
                continue
            labels_here = label_by_reference.get(identity, ())
            if len(labels_here) != 1:
                exclusions['AMBIGUOUS_LABEL_REFERENCE'] += 1
                continue
            label = labels_here[0]
            checks = (
                base.dataset_revision_id == item['datasetRevisionId'] == candidate.dataset_revision_id,
                base.instrument_id == item['instrumentId'] == candidate.instrument_id,
                base.as_of == reference,
                base.quality_status == 'PASS',
                base.source_max_timestamp <= base.as_of,
                candidate.quality_status == 'PASS',
                meta.get('status') == 'PASS',
                meta.get('horizonEligibility', {}).get(str(horizon)) is True,
                candidate.source_max_timestamp <= candidate.as_of,
                candidate.as_of > reference,
                candidate.as_of == reference + timedelta(minutes=1),
                candidate_reference == reference,
                label.as_of == reference,
                label.expiry_at == reference + timedelta(minutes=horizon),
            )
            if not all(checks):
                exclusions['EXACT_REFERENCE_VALIDATION_FAILED'] += 1
                continue
            rows.append({'day': day, 'reference': reference, 'baseline': base,
                         'candidate': candidate, 'label': label})
        rows.sort(key=lambda item: item['reference'])
        return rows, exclusions

    def _fit(self, rows, schema, horizon, algorithm):
        if not rows:
            raise ValueError('INSUFFICIENT_ESTIMATOR_ROWS')
        labels = [item['label'].label for item in rows]
        excursions = [_finite(item['label'].maximum_favorable_excursion) for item in rows]
        if any(value is None or value < 0 for value in excursions):
            raise ValueError('GENUINE_TRAINING_EXCURSIONS_REQUIRED')
        if len(rows) < 100 or any(labels.count(name) < MIN_CLASS_ROWS for name in CLASSES):
            raise ValueError('INSUFFICIENT_TRAINING_CLASSES_OR_ROWS')
        matrix = np.asarray([[_value(item, name) for name in schema] for item in rows], dtype=float)
        return train_two_stage_model(horizon=horizon, train_x=matrix, labels=labels,
                                     realized_excursions=excursions, feature_schema=schema,
                                     algorithm=algorithm)

    def _fold(self, rows, window, horizon, algorithm, base_names, context_names,
              minimum_training_days):
        history_days = sorted({row['day'] for row in rows if row['day'] < window[0]})
        required = int(minimum_training_days) + CALIBRATION_DAYS
        if len(history_days) < required:
            return {'status': 'BLOCKED', 'blockers': ['INSUFFICIENT_TRAINING_DAYS'],
                    'evaluationDates': [str(day) for day in window]}
        estimator_days = history_days[-required:-CALIBRATION_DAYS]
        fit_day, select_day, threshold_day = history_days[-CALIBRATION_DAYS:]
        estimator_source = [row for row in rows if row['day'] in estimator_days]
        baseline_schema, baseline_coverage = _schema(estimator_source, base_names)
        candidate_context, context_coverage = _schema(estimator_source, context_names)
        candidate_schema = [*baseline_schema, *candidate_context]
        blockers = []
        if not baseline_schema:
            blockers.append('NO_V2_TRAINING_SCHEMA')
        if not candidate_context:
            blockers.append('NO_CONTEXT_V3_FEATURES_SELECTED_ON_ESTIMATOR_PARTITION')
        if blockers:
            return {'status': 'BLOCKED', 'blockers': blockers,
                    'schemas': {'baseline': baseline_schema, 'candidate': candidate_schema,
                                'candidateContext': candidate_context},
                    'evaluationDates': [str(day) for day in window]}

        def paired(source):
            return [row for row in source if _complete(row, baseline_schema)
                    and _complete(row, candidate_schema)]

        estimator = paired([row for row in rows if row['day'] in estimator_days])
        fit_rows = paired([row for row in rows if row['day'] == fit_day])
        select_rows = paired([row for row in rows if row['day'] == select_day])
        threshold_rows = paired([row for row in rows if row['day'] == threshold_day])
        evaluation = paired([row for row in rows if row['day'] in window])
        counts = {
            'estimator': len(estimator), 'calibrationFit': len(fit_rows),
            'calibrationSelection': len(select_rows), 'thresholdSelection': len(threshold_rows),
            'evaluation': len(evaluation),
        }
        if not estimator or len(evaluation) < MIN_FOLD_ROWS:
            return {'status': 'BLOCKED', 'blockers': ['INSUFFICIENT_PAIRED_PARTITION_ROWS'],
                    'rowCounts': counts, 'schemas': {
                        'baseline': baseline_schema, 'candidate': candidate_schema,
                        'candidateContext': candidate_context},
                    'evaluationDates': [str(day) for day in window]}
        try:
            baseline_model = self._fit(estimator, baseline_schema, horizon, algorithm)
            candidate_model = self._fit(estimator, candidate_schema, horizon, algorithm)
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            return {'status': 'BLOCKED', 'blockers': ['MODEL_FIT_UNAVAILABLE'],
                    'reason': str(exc), 'rowCounts': counts, 'schemas': {
                        'baseline': baseline_schema, 'candidate': candidate_schema,
                        'candidateContext': candidate_context},
                    'evaluationDates': [str(day) for day in window]}
        calibration = {}
        config = EvaluationConfiguration(minimumTrainingDays=4, evaluationStepDays=1,
                                         calibrationMinimumSamples=20,
                                         calibrationMinimumClassSamples=3)
        for stage, direction_only in (('move', False), ('direction', True)):
            for side, model in (('baseline', baseline_model), ('candidate', candidate_model)):
                fit_p, fit_y, select_p, select_y = [], [], [], []
                for source, probabilities, outcomes in (
                        (fit_rows, fit_p, fit_y), (select_rows, select_p, select_y)):
                    for row in source:
                        if direction_only and row['label'].label == 'NO_BIG_MOVE':
                            continue
                        prediction = model.predict(
                            [_value(row, name) for name in model.feature_schema],
                            float(row['label'].reference_price))
                        probabilities.append(
                            prediction['rawBigMoveProbability' if stage == 'move'
                                        else 'rawUpGivenBigMoveProbability'])
                        outcomes.append(int(row['label'].label == 'BIG_UP' if direction_only
                                            else row['label'].label != 'NO_BIG_MOVE'))
                calibrator, evidence = _calibrate(fit_p, fit_y, select_p, select_y, config)
                setattr(model, f'{stage}_calibrator', calibrator)
                calibration[f'{side}_{stage}'] = {
                    **evidence, 'fitSamples': len(fit_y), 'selectionSamples': len(select_y)}
        thresholds = {}
        for side, model in (('baseline', baseline_model), ('candidate', candidate_model)):
            value, evidence = _threshold(model, threshold_rows)
            model.move_decision_threshold = value
            thresholds[side] = {'value': value, 'evidence': evidence}
        records = []
        for row in evaluation:
            record = {'day': row['day'], 'referenceMinute': row['reference'].isoformat(),
                      'actual': row['label'].label,
                      'actualMagnitude': _finite(row['label'].maximum_favorable_excursion)}
            record.update(_predict(baseline_model, row, 'baseline'))
            record.update(_predict(candidate_model, row, 'candidate'))
            record['baselineThreshold'] = thresholds['baseline']['value']
            record['candidateThreshold'] = thresholds['candidate']['value']
            records.append(record)
        return {
            'status': 'EVALUATED',
            'blockers': [],
            'schemas': {'baseline': baseline_schema, 'candidate': candidate_schema,
                        'candidateContext': candidate_context},
            'coverage': {'baseline': baseline_coverage, 'candidate': context_coverage},
            'rowCounts': counts,
            'partitions': {
                'estimatorDates': [str(day) for day in estimator_days],
                'calibrationFitDate': str(fit_day),
                'calibrationSelectionDate': str(select_day),
                'thresholdSelectionDate': str(threshold_day),
                'evaluationDates': [str(day) for day in window],
            },
            'models': {
                'baseline': {'version': baseline_model.model_version,
                             'schemaChecksum': digest(list(baseline_schema)),
                             'threshold': thresholds['baseline']},
                'candidate': {'version': candidate_model.model_version,
                              'schemaChecksum': digest(list(candidate_schema)),
                              'threshold': thresholds['candidate']},
            },
            'calibration': calibration,
            'thresholds': thresholds,
            'metrics': {'baseline': _metrics(records, side='baseline'),
                        'candidate': _metrics(records, side='candidate')},
            'bootstrap': _day_bootstrap(records),
            'predictionChecksum': digest(records),
            'records': records,
        }

    def run(self, revision_id, instrument_id, profile, *, date_from=None, date_to=None,
            minimum_training_days=5, holdout_days=5, evaluation_step_days=1,
            horizons=BIG_MOVE_HORIZONS, algorithms=ALGORITHMS, persist=True):
        revision_id = str(revision_id)
        instrument_id = int(instrument_id)
        if profile not in PROFILES:
            raise ValueError('UNKNOWN_CONTEXT_V3_PROFILE')
        horizons = tuple(dict.fromkeys(int(value) for value in horizons))
        if not horizons or any(value not in BIG_MOVE_HORIZONS for value in horizons):
            raise ValueError('UNSUPPORTED_EXPERIMENT_HORIZON')
        algorithms = tuple(dict.fromkeys(algorithms))
        if not algorithms or any(value not in ALGORITHMS for value in algorithms):
            raise ValueError('UNSUPPORTED_EXPERIMENT_ALGORITHM')
        if int(minimum_training_days) < 4 or int(holdout_days) < 1 or int(evaluation_step_days) < 1:
            raise ValueError('INVALID_EXPERIMENT_PARTITION_CONFIGURATION')
        start = date.fromisoformat(str(date_from)) if date_from else None
        end = date.fromisoformat(str(date_to)) if date_to else None
        if start and end and start > end:
            raise ValueError('INVALID_EXPERIMENT_DATE_RANGE')
        revision, baseline, candidates, labels = self._load(revision_id, instrument_id, profile)
        matched={}
        for horizon in horizons:
            cohort,_=self._exact_cohort(baseline,candidates,labels,horizon,start,end)
            for item in cohort:
                row=item['candidate']
                matched[(row.dataset_revision_id,row.instrument_id,row.as_of)]=row
        generation = self._generation_manifest(list(matched.values()))
        policy = self._label_policy(labels)
        context_names = _context_names(candidates)
        base_names = [name for group in ('price_features', 'futures_features', 'option_features')
                      for name in big_move_feature_definition()[group] if name != 'futures_buildup']
        horizons_result, cohort_summary = {}, {}
        all_dates = set()
        for horizon in horizons:
            rows, exclusions = self._exact_cohort(
                baseline, candidates, labels, horizon, start, end)
            days = sorted({row['day'] for row in rows})
            all_dates.update(days)
            eligible = 0
            for row in baseline:
                day = _day(row.as_of)
                if start and day < start or end and day > end:
                    continue
                if any(label.dataset_revision_id == row.dataset_revision_id
                       and label.instrument_id == row.instrument_id
                       and label.as_of == row.as_of
                       and label.horizon_minutes == horizon
                       and label.quality_status == 'VALID' and label.label in CLASSES for label in labels):
                    eligible += 1
            fraction = len(rows) / eligible if eligible else 0.
            cohort_summary[str(horizon)] = {
                'commonSupportRows': len(rows), 'baselineEligibleRows': eligible,
                'commonSupportFraction': fraction, 'tradingDates': [str(day) for day in days],
                'exclusions': dict(exclusions),
                'cohortChecksum': digest([(row['reference'].isoformat(), row['label'].label,
                                           _hash(row['label'].to_dict())) for row in rows]),
            }
            blockers = []
            if fraction < MIN_COMMON_SUPPORT_FRACTION:
                blockers.append('COMMON_SUPPORT_BELOW_70_PERCENT')
            if len(days) < max(MIN_EVALUATION_DAYS,
                               int(minimum_training_days) + CALIBRATION_DAYS + int(holdout_days)):
                blockers.append('INSUFFICIENT_TRADING_DAYS')
            horizon_result = {
                'status': 'BLOCKED',
                'classification': 'INSUFFICIENT_COVERAGE'
                if 'COMMON_SUPPORT_BELOW_70_PERCENT' in blockers else 'INSUFFICIENT_EVIDENCE',
                'blockers': blockers, 'algorithms': {}, 'cohort': cohort_summary[str(horizon)],
            }
            # Requested combinations remain explicit even when data gates prevent fitting.
            horizon_result['algorithms']={algorithm:{
                'status':'BLOCKED','classification':horizon_result['classification'],
                'blockers':list(blockers),'foldCount':0,'evaluatedFoldCount':0,
                'pairedRows':len(rows),'independentDays':len(days),'aggregate':None,
                'exactFinalHoldout':None,'bootstrap':{'status':'INSUFFICIENT_EVIDENCE'},
                'predictionChecksum':digest([])} for algorithm in algorithms}
            if not blockers:
                windows = [days[index:index + int(evaluation_step_days)]
                           for index in range(int(minimum_training_days) + CALIBRATION_DAYS,
                                              len(days) - int(holdout_days), int(evaluation_step_days))]
                final_window = days[-int(holdout_days):]
                if not windows:
                    horizon_result['blockers'].append('INSUFFICIENT_COMPARISON_WINDOWS')
                else:
                    for algorithm in algorithms:
                        folds = []
                        for window in windows:
                            fold = self._fold(rows, window, horizon, algorithm, base_names,
                                              context_names, minimum_training_days)
                            fold['finalHoldout'] = False
                            folds.append(fold)
                        final_fold = self._fold(rows, final_window, horizon, algorithm,
                                                base_names, context_names, minimum_training_days)
                        final_fold['finalHoldout'] = True
                        folds.append(final_fold)
                        evaluated = [fold for fold in folds if fold['status'] == 'EVALUATED']
                        comparison_records = [record for fold in evaluated if not fold['finalHoldout']
                                              for record in fold['records']]
                        final_records = [record for fold in evaluated if fold['finalHoldout']
                                         for record in fold['records']]
                        all_records = comparison_records + final_records
                        comparison_metrics = (
                            {'baseline': _metrics(comparison_records, side='baseline'),
                             'candidate': _metrics(comparison_records, side='candidate')}
                            if comparison_records else None)
                        final_metrics = (
                            {'baseline': _metrics(final_records, side='baseline'),
                             'candidate': _metrics(final_records, side='candidate')}
                            if final_records else None)
                        bootstrap = _day_bootstrap(comparison_records)
                        classification = _classification(
                            comparison_metrics, final_metrics, bootstrap,
                            horizon_result['blockers'], True,
                            len(days) >= (int(minimum_training_days) + CALIBRATION_DAYS
                                          + int(holdout_days)))
                        public_folds = [{key: value for key, value in fold.items()
                                         if key not in ('records',)} for fold in folds]
                        horizon_result['algorithms'][algorithm] = {
                            'classification': classification,
                            'status': 'EVALUATED' if evaluated else 'BLOCKED',
                            'foldCount': len(folds),
                            'evaluatedFoldCount': len(evaluated),
                            'aggregate': comparison_metrics,
                            'exactFinalHoldout': final_metrics,
                            'bootstrap': bootstrap,
                            'folds': public_folds,
                            'predictionChecksum': digest(all_records),
                            'pairedRows': len(all_records),
                            'independentDays': len({record['day'] for record in all_records}),
                        }
                    values = [row['classification'] for row in horizon_result['algorithms'].values()]
                    if 'V3_WORSE' in values:
                        horizon_result['classification'] = 'V3_WORSE'
                    elif values and all(value == 'V3_BETTER' for value in values):
                        horizon_result['classification'] = 'V3_BETTER'
                    elif any(value == 'INSUFFICIENT_COVERAGE' for value in values):
                        horizon_result['classification'] = 'INSUFFICIENT_COVERAGE'
                    elif any(value == 'INSUFFICIENT_EVIDENCE' for value in values) or not values:
                        horizon_result['classification'] = 'INSUFFICIENT_EVIDENCE'
                    else:
                        horizon_result['classification'] = 'NO_CLEAR_IMPROVEMENT'
                    horizon_result['status'] = 'EVALUATED' if values else 'BLOCKED'
            horizons_result[str(horizon)] = horizon_result
        manifest = {
            'kind': 'CONTEXT_V3_PAIRED_EXPERIMENT',
            'implementationVersion': IMPLEMENTATION_VERSION,
            'experimentId': None,
            'datasetRevisionId': revision_id,
            'revisionChecksum': revision.checksum,
            'instrumentId': instrument_id,
            'profileVersion': profile,
            'profileChecksum': _hash(profile_definition(profile)),
            'v2FeatureDefinitionChecksum': _hash(big_move_feature_definition()),
            'labelPolicy': policy,
            'contextGeneration': {key: value for key, value in generation.items() if key != 'definition'},
            'contextGenerationManifests': generation['manifests'],
            'contextGenerationManifestsChecksum': generation['checksum'],
            'configuration': {
                'dateFrom': str(start) if start else None,
                'dateTo': str(end) if end else None,
                'horizons': list(horizons), 'algorithms': list(algorithms),
                'minimumTrainingDays': int(minimum_training_days),
                'calibrationDays': CALIBRATION_DAYS,
                'holdoutDays': int(holdout_days),
                'evaluationStepDays': int(evaluation_step_days),
                'minimumCommonSupportFraction': MIN_COMMON_SUPPORT_FRACTION,
                'bootstrapSamples': BOOTSTRAP_SAMPLES, 'bootstrapSeed': BOOTSTRAP_SEED,
                'missingPolicy': 'NULL_NO_IMPUTATION',
                'decisionThreshold': 'LEARNED_FROM_THRESHOLD_SELECTION_DATE_ONLY',
            },
            'cohortPolicy': 'SAME_REVISION_INSTRUMENT_REFERENCE_MINUTE_HORIZON_LABEL_POLICY',
            'cohort': cohort_summary,
            'tradingDates': [str(day) for day in sorted(all_dates)],
            'horizons': horizons_result,
            'classification': _overall_classification(horizons_result),
            'runtimeImpact': 'NONE',
            'productionArtifactsModified': False,
            'blockers': sorted({code for row in horizons_result.values() for code in row['blockers']}),
        }
        identity = {key: value for key, value in manifest.items() if key != 'experimentId'}
        experiment_id = digest(identity)[:32]
        manifest['experimentId'] = experiment_id
        checksum = digest(manifest)
        manifest['checksum'] = checksum
        stored = False
        if persist:
            version = EXPERIMENT_PREFIX + experiment_id
            definition = {key: value for key, value in manifest.items() if key != 'checksum'}
            try:
                self.repository.save_feature_set(FeatureSetDefinition(
                    version, datetime.now(IST), definition, digest(definition)))
                stored = True
            except ValueError:
                raise ValueError('IMMUTABLE_CONTEXT_V3_EXPERIMENT_CONFLICT') from None
        return {'available': True, 'status': manifest['classification'], 'manifest': manifest,
                'stored': stored, 'version': EXPERIMENT_PREFIX + experiment_id if persist else None}

    def stored(self, limit=20):
        reader = getattr(self.repository, 'feature_sets_like', None)
        if reader is None:
            return {'available': False, 'reason': 'EXPERIMENT_STORE_UNAVAILABLE',
                    'count': 0, 'experiments': []}
        experiments = []
        for row in reader(EXPERIMENT_PREFIX, limit):
            definition = dict(row.definition)
            horizons = {}
            for horizon, value in definition.get('horizons', {}).items():
                horizons[horizon] = {
                    'classification': value.get('classification'),
                    'status': value.get('status'),
                    'algorithms': {
                        name: {
                            'classification': run.get('classification'),
                            'foldCount': run.get('foldCount'),
                            'evaluatedFoldCount': run.get('evaluatedFoldCount'),
                            'predictionChecksum': run.get('predictionChecksum'),
                            'pairedRows': run.get('pairedRows'),
                            'independentDays': run.get('independentDays'),
                            'aggregate': run.get('aggregate'),
                            'exactFinalHoldout': run.get('exactFinalHoldout'),
                            'bootstrap': run.get('bootstrap'),
                        } for name, run in value.get('algorithms', {}).items()},
                }
            experiments.append({
                'version': row.version,
                'createdAt': row.created_at.isoformat() if row.created_at else None,
                'experimentId': definition.get('experimentId'),
                'datasetRevisionId': definition.get('datasetRevisionId'),
                'instrumentId': definition.get('instrumentId'),
                'profileVersion': definition.get('profileVersion'),
                'dateFrom': definition.get('configuration', {}).get('dateFrom'),
                'dateTo': definition.get('configuration', {}).get('dateTo'),
                'classification': definition.get('classification'),
                'blockers': definition.get('blockers', []),
                'runtimeImpact': definition.get('runtimeImpact'),
                'productionArtifactsModified': definition.get('productionArtifactsModified'),
                'manifestChecksum': row.checksum,
                'contextGenerationChecksum': (definition.get('contextGeneration') or {}).get('checksum'),
                'contextGenerationManifests': definition.get('contextGenerationManifests',[]),
                'tradingDates': definition.get('tradingDates', []),
                'horizons': horizons,
            })
        return {'available': True, 'count': len(experiments), 'experiments': experiments,
                'notice': 'RESEARCH ONLY - NOT ACTIVE MODEL. Stored evidence only; no model or runtime state is changed.'}

    def get(self, experiment_id):
        reader = getattr(self.repository, 'feature_sets_like', None)
        if reader is None:
            return {'available': False, 'reason': 'EXPERIMENT_STORE_UNAVAILABLE'}
        if not re.fullmatch(r'[0-9a-f]{32}', str(experiment_id)):
            return {'available': False, 'reason': 'EXPERIMENT_NOT_FOUND'}
        rows = [row for row in reader(EXPERIMENT_PREFIX + str(experiment_id), 5)
                if row.version == EXPERIMENT_PREFIX + str(experiment_id)]
        if not rows:
            return {'available': False, 'reason': 'EXPERIMENT_NOT_FOUND'}
        definition = dict(rows[0].definition)
        definition['checksum'] = rows[0].checksum
        return {'available': True, 'version': rows[0].version,
                'createdAt': rows[0].created_at.isoformat() if rows[0].created_at else None,
                'manifest': definition, 'notice': 'RESEARCH ONLY - NOT ACTIVE MODEL'}
