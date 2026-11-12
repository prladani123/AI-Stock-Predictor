"""Stage D: chronological V2 evidence and immutable, evidence-gated promotion.

The last evaluation window is reserved for the exact persisted candidate. Earlier
folds compare learning procedures; they are not represented as predictions from
that candidate. Calibration fit, method selection and decision-threshold selection
use separate historical dates, all strictly before the evaluation window.
"""
from collections import Counter
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

import numpy as np
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (accuracy_score, average_precision_score, balanced_accuracy_score,
    brier_score_loss, confusion_matrix, f1_score, log_loss, precision_score, recall_score, roc_auc_score)

from .big_move_features import FEATURE_SET_VERSION_V2
from .big_move_labels import BIG_MOVE_HORIZONS
from .big_move_runtime import BigMoveTrainingService, numeric, policy_payload
from .calendar import IST
from .repository import digest

CLASSES = ('BIG_UP', 'BIG_DOWN', 'NO_BIG_MOVE')
ALGORITHMS = ('LOGISTIC_REGRESSION', 'LIGHTGBM')
IMPLEMENTATION = 'big-move-evaluation-v1'


@dataclass(frozen=True)
class EvaluationConfiguration:
    minimumTrainingDays: int = 5
    evaluationStepDays: int = 1
    calibrationMinimumSamples: int = 60
    calibrationMinimumClassSamples: int = 10
    minimumThresholdRecall: float = .20
    minimumThresholdCoverage: float = .05
    minimumThresholdPredictions: int = 10

    def __post_init__(self):
        if self.minimumTrainingDays < 4 or self.evaluationStepDays < 1:
            raise ValueError('At least four historical days and a positive evaluation step are required')
        if min(self.calibrationMinimumSamples, self.calibrationMinimumClassSamples,
               self.minimumThresholdPredictions) < 1:
            raise ValueError('Sample requirements must be positive')
        if not 0 < self.minimumThresholdRecall <= 1 or not 0 < self.minimumThresholdCoverage <= 1:
            raise ValueError('Threshold coverage/recall must be in (0,1]')


@dataclass
class BinaryCalibrator:
    method: str
    estimator: object

    def predict(self, probabilities):
        values = np.clip(np.asarray(probabilities, dtype=float), 1e-9, 1-1e-9)
        if self.method == 'SIGMOID':
            return self.estimator.predict_proba(np.log(values/(1-values)).reshape(-1, 1))[:, 1]
        return self.estimator.predict(values)


def _calibrate(fit_p, fit_y, selection_p, selection_y, config):
    def sufficient(y):
        return len(y) >= config.calibrationMinimumSamples and min(Counter(y).get(0, 0),
            Counter(y).get(1, 0)) >= config.calibrationMinimumClassSamples
    if not sufficient(fit_y) or not sufficient(selection_y):
        return None, {'status': 'UNAVAILABLE', 'reason': 'INSUFFICIENT_CALIBRATION_CLASSES',
                      'fitSamples': len(fit_y), 'selectionSamples': len(selection_y)}
    p = np.clip(np.asarray(fit_p), 1e-9, 1-1e-9)
    sigmoid = BinaryCalibrator('SIGMOID', LogisticRegression(C=1., max_iter=300, random_state=1729).fit(
        np.log(p/(1-p)).reshape(-1, 1), fit_y))
    isotonic = BinaryCalibrator('ISOTONIC', IsotonicRegression(out_of_bounds='clip', y_min=0., y_max=1.).fit(p, fit_y))
    candidates = {'RAW': None, 'SIGMOID': sigmoid, 'ISOTONIC': isotonic}
    scores = {}
    for name, candidate in candidates.items():
        predicted = np.asarray(selection_p) if candidate is None else candidate.predict(selection_p)
        scores[name] = {'brier': float(brier_score_loss(selection_y, predicted)),
                       'logLoss': float(log_loss(selection_y, np.column_stack((1-predicted, predicted)), labels=[0, 1]))}
    method = min(candidates, key=lambda name: (scores[name]['brier'], scores[name]['logLoss'],
                                             ('RAW', 'SIGMOID', 'ISOTONIC').index(name)))
    return candidates[method], {'status': 'RAW_RETAINED' if method == 'RAW' else 'CALIBRATED',
        'method': method, 'selectionScores': scores, 'fitSamples': len(fit_y), 'selectionSamples': len(selection_y)}


def _date(row):
    return row[1].as_of.astimezone(IST).date()


def _ready(feature, schema):
    return feature is not None and all(numeric(feature.values.get(name)) is not None for name in schema)


def _valid(row):
    return row[1].label in CLASSES and row[1].quality_status == 'VALID'


def _load(service, revision, instrument, algorithm):
    features, labels, policy, _, _ = service.training_data(revision, instrument, algorithm)
    if not labels:
        raise ValueError('No immutable V2 labels')
    dates = [r.as_of.astimezone(IST).date() for r in labels]
    sessions = {s.session_date: s for s in service.repository.sessions_between(min(dates), max(dates))}
    seen = set()
    rows = []
    for label in labels:
        key = (label.as_of, label.horizon_minutes)
        if key in seen:
            raise ValueError('Duplicate V2 horizon/as-of label')
        seen.add(key)
        session = sessions.get(label.as_of.astimezone(IST).date())
        if (not session or session.status not in ('OPEN', 'SPECIAL') or
            not session.open_at <= label.as_of < label.expiry_at < session.close_at or
            label.expiry_at != label.as_of + timedelta(minutes=label.horizon_minutes)):
            raise ValueError('V2 evaluation requires explicit continuous-session label lineage')
        rows.append((features.get(label.as_of), label))
    rows.sort(key=lambda row: (row[1].as_of, row[1].horizon_minutes))
    return rows, policy


def _fold_fit(service, rows, horizon, algorithm, config):
    days = sorted({_date(row) for row in rows})
    if len(days) < config.minimumTrainingDays:
        raise ValueError('INSUFFICIENT_TRAINING_DAYS')
    # Three independent historical tail days: calibrator fit, method choice,
    # threshold choice. Neither choice can observe final evaluation outcomes.
    estimator_days, fit_day, select_day, threshold_day = days[:-3], *days[-3:]
    estimator_rows = [row for row in rows if _date(row) in estimator_days and _valid(row) and row[0] is not None]
    model, metadata = service.fit_partition(estimator_rows, horizon, algorithm)

    def predictions(day):
        selected = [row for row in rows if _date(row) == day and _valid(row) and _ready(row[0], model.feature_schema)]
        return [(r, model.predict([numeric(f.values[n]) for n in model.feature_schema], float(r.reference_price))) for f, r in selected]

    fit, select = predictions(fit_day), predictions(select_day)
    calibration = {}
    for stage, raw_key, direction_only in (
        ('move', 'rawBigMoveProbability', False), ('direction', 'rawUpGivenBigMoveProbability', True)):
        def samples(source):
            usable = [(r, p) for r, p in source if not direction_only or r.label != 'NO_BIG_MOVE']
            return ([p[raw_key] for _, p in usable],
                    [int(r.label == 'BIG_UP' if direction_only else r.label != 'NO_BIG_MOVE') for r, _ in usable])
        fit_p, fit_y = samples(fit)
        sel_p, sel_y = samples(select)
        calibrator, calibration[stage] = _calibrate(fit_p, fit_y, sel_p, sel_y, config)
        setattr(model, stage + '_calibrator', calibrator)
    threshold_rows = predictions(threshold_day)
    p = np.asarray([v['bigMoveProbability'] for _, v in threshold_rows])
    y = np.asarray([r.label != 'NO_BIG_MOVE' for r, _ in threshold_rows], dtype=int)
    choices = []
    # Observed probability cut points, rather than a fixed 0.50 decision rule.
    if len(y) and set(y) == {0, 1}:
        for threshold in sorted(set(float(v) for v in p)):
            predicted = p >= threshold
            recall = float(recall_score(y, predicted, zero_division=0))
            coverage = float(predicted.mean())
            if (predicted.sum() >= config.minimumThresholdPredictions and
                recall >= config.minimumThresholdRecall and coverage >= config.minimumThresholdCoverage):
                choices.append((float(precision_score(y, predicted, zero_division=0)), recall, -coverage, threshold))
    if choices:
        precision, recall, minus_coverage, threshold = max(choices)
        model.move_decision_threshold = threshold
        threshold_evidence = {'status': 'SELECTED', 'value': threshold, 'precision': precision,
                              'recall': recall, 'coverage': -minus_coverage, 'samples': len(y)}
    else:
        threshold_evidence = {'status': 'UNAVAILABLE', 'value': None, 'samples': len(y),
                              'reason': 'INSUFFICIENT_THRESHOLD_EVIDENCE'}
    metadata.update({'calibration': calibration, 'moveDecisionThreshold': model.move_decision_threshold,
        'thresholdSelection': threshold_evidence, 'coverage_partition': 'ESTIMATOR_TRAINING_ONLY',
        'partitions': {'estimatorDates': [str(d) for d in estimator_days], 'calibrationFitDate': str(fit_day),
                       'calibrationSelectionDate': str(select_day), 'thresholdSelectionDate': str(threshold_day)},
        'last_training_date': str(days[-1])})
    return model, metadata


def _windows(rows, config):
    days = sorted({_date(row) for row in rows})
    windows = [days[i:i+config.evaluationStepDays] for i in
               range(config.minimumTrainingDays, len(days), config.evaluationStepDays)]
    if not windows:
        raise ValueError('INSUFFICIENT_EVIDENCE: no future evaluation dates')
    return windows


def train_candidates(service, revision, instrument, algorithm, config):
    rows, policy = _load(service, revision, instrument, algorithm)
    trained = []
    for horizon in BIG_MOVE_HORIZONS:
        horizon_rows = [row for row in rows if row[1].horizon_minutes == horizon]
        holdout = _windows(horizon_rows, config)[-1]
        historical = [row for row in horizon_rows if _date(row) < holdout[0]]
        model, metadata = _fold_fit(service, historical, horizon, algorithm, config)
        identity = digest({'implementation': IMPLEMENTATION, 'revision': str(revision), 'instrument': instrument,
            'algorithm': algorithm, 'horizon': horizon, 'configuration': asdict(config), 'metadata': metadata,
            'historicalRows': [(f.to_dict() if f else None, r.to_dict()) for f, r in historical],
            'thresholdPolicy': policy_payload(policy), 'reservedDates': [str(d) for d in holdout]})
        model.model_version = f'big-move-v2-{algorithm.lower()}-h{horizon}-{identity[:24]}'
        manifest = {**metadata, 'model_family': 'BIG_MOVE_V2', 'model_version': model.model_version,
            'training_revision': str(revision), 'instrument_id': instrument, 'horizon_minutes': horizon,
            'algorithm': algorithm, 'status': 'CANDIDATE', 'feature_set_version': FEATURE_SET_VERSION_V2,
            'training_identity': identity, 'threshold_policy': policy_payload(policy),
            'evaluation_configuration': asdict(config), 'implementation': IMPLEMENTATION,
            'reserved_evaluation_dates': [str(d) for d in holdout],
            'created_at': datetime.now(timezone.utc).isoformat()}
        trained.append((model, manifest))
    return service.save_candidate_models(trained, revision, algorithm)


def _binary_metrics(actual, probabilities, predicted):
    if not len(actual):
        return {'sampleCount': 0, 'classCounts': {'negative': 0, 'positive': 0},
                **{k: None for k in ('accuracy', 'balancedAccuracy', 'precision', 'recall', 'f1',
                                    'rocAuc', 'prAuc', 'brier', 'logLoss', 'predictedRate', 'actualRate')}}
    y, p, pred = np.asarray(actual, dtype=int), np.asarray(probabilities), np.asarray(predicted, dtype=int)
    both = len(set(y)) == 2
    return {'sampleCount': len(y), 'classCounts': {'negative': int((y == 0).sum()), 'positive': int(y.sum())},
        'accuracy': float(accuracy_score(y, pred)),
        'balancedAccuracy': float(balanced_accuracy_score(y, pred)) if both else None,
        'precision': float(precision_score(y, pred, zero_division=0)) if pred.sum() else None,
        'recall': float(recall_score(y, pred, zero_division=0)) if y.sum() else None,
        'f1': float(f1_score(y, pred, zero_division=0)) if y.sum() or pred.sum() else None,
        'rocAuc': float(roc_auc_score(y, p)) if both else None,
        'prAuc': float(average_precision_score(y, p)) if both else None,
        'brier': float(brier_score_loss(y, p)),
        'logLoss': float(log_loss(y, np.column_stack((1-p, p)), labels=[0, 1])),
        'predictedRate': float(pred.mean()), 'actualRate': float(y.mean())}


def metrics(records, total, exclusions):
    move = _binary_metrics([r['actual'] != 'NO_BIG_MOVE' for r in records],
        [r['move'] for r in records], [r['predicted'] != 'NO_BIG_MOVE' for r in records])
    direction_rows = [r for r in records if r['actual'] != 'NO_BIG_MOVE']
    y = [r['actual'] == 'BIG_UP' for r in direction_rows]
    p = [r['up'] for r in direction_rows]
    direction = _binary_metrics(y, p, [v >= .5 for v in p])
    direction['upCount'] = sum(y)
    direction['downCount'] = len(y)-sum(y)
    direction['UP'] = {k: direction[k] for k in ('precision', 'recall', 'f1')}
    down = _binary_metrics([not v for v in y], [1-v for v in p], [v < .5 for v in p])
    direction['DOWN'] = {k: down[k] for k in ('precision', 'recall', 'f1')}
    actual, predicted = [r['actual'] for r in records], [r['predicted'] for r in records]
    joint = {'accuracy': float(accuracy_score(actual, predicted)) if records else None,
        'macroF1': float(f1_score(actual, predicted, labels=CLASSES, average='macro', zero_division=0)) if records else None,
        'weightedF1': float(f1_score(actual, predicted, labels=CLASSES, average='weighted', zero_division=0)) if records else None,
        'classes': list(CLASSES), 'confusionMatrix': confusion_matrix(actual, predicted, labels=CLASSES).tolist() if records else None}
    errors = [abs(r['expected']-r['excursion']) for r in direction_rows if r['excursion'] is not None]
    return {'stageA': move, 'stageB': direction, 'joint': joint,
        'expectedMove': {'sampleCount': len(errors), 'mae': float(np.mean(errors)) if errors else None,
                         'medianAbsoluteError': float(np.median(errors)) if errors else None, 'unit': 'return_fraction'},
        'coverage': len(records)/total if total else 0., 'eligibleRows': total,
        'scoredRows': len(records), 'exclusions': dict(exclusions)}


def _score(model, rows):
    records, excluded = [], Counter()
    eligible = 0
    for feature, label in rows:
        if not _valid((feature, label)):
            excluded[label.label if label.label in ('UNKNOWN', 'AMBIGUOUS') else 'INVALID_LABEL'] += 1
            continue
        eligible += 1
        if model is None or model.move_decision_threshold is None or not _ready(feature, model.feature_schema):
            excluded['DATA_NOT_READY'] += 1
            continue
        prediction = model.predict([numeric(feature.values[n]) for n in model.feature_schema], float(label.reference_price))
        records.append({'asOf': label.as_of.isoformat(), 'day': str(label.as_of.astimezone(IST).date()),
            'regime': str(feature.values.get('market_regime') or 'UNAVAILABLE'), 'actual': label.label,
            'predicted': 'BIG_'+prediction['direction'] if prediction['state'] == 'BIG_MOVE' else 'NO_BIG_MOVE',
            'move': prediction['bigMoveProbability'], 'up': prediction['upGivenBigMoveProbability'],
            'rawMove': prediction['rawBigMoveProbability'], 'rawUp': prediction['rawUpGivenBigMoveProbability'],
            'threshold': model.move_decision_threshold, 'expected': prediction['expectedMovePercent'],
            'excursion': numeric(label.maximum_favorable_excursion)})
    return records, eligible, excluded


def _stability(records, field, minimum=1, expected_days=None):
    result = []
    for key in sorted({r[field] for r in records} | set(expected_days or ())):
        group = [r for r in records if r[field] == key]
        if (len(group) < minimum and field != 'day') or (field == 'regime' and key == 'UNAVAILABLE'): continue
        m = metrics(group, len(group), {})
        result.append({field: key, 'sampleCount': len(group), 'movePrecision': m['stageA']['precision'],
                       'moveRecall': m['stageA']['recall'], 'directionAccuracy': m['stageB']['accuracy']})
    return result


class BigMoveEvaluationService:
    def __init__(self, repository, store=None, configuration=None):
        self.repository = repository
        self.training = BigMoveTrainingService(repository, store)
        self.config = configuration or EvaluationConfiguration()

    def evaluate(self, revision, instrument):
        results = []
        for algorithm in ALGORITHMS:
            rows, policy = _load(self.training, revision, instrument, algorithm)
            # Final artifacts are created once; fold artifacts stay in memory.
            candidates = self.training.train(revision, instrument, algorithm, self.config)
            for candidate in candidates:
                horizon = candidate['horizon_minutes']
                selected = [row for row in rows if row[1].horizon_minutes == horizon]
                windows = _windows(selected, self.config)
                all_records, all_excluded, total, folds = [], Counter(), 0, []
                exact_metrics = None
                comparison_records, comparison_excluded, comparison_total = [], Counter(), 0
                for window in windows:
                    history = [row for row in selected if _date(row) < window[0]]
                    future = [row for row in selected if _date(row) in window]
                    failure = None
                    if window == windows[-1]:
                        model, metadata = self.training.store.load(candidate)
                        if metadata['reserved_evaluation_dates'] != [str(d) for d in window]:
                            raise ValueError('Candidate holdout mismatch')
                    else:
                        try:
                            model, metadata = _fold_fit(self.training, history, horizon, algorithm, self.config)
                        except ValueError as error:
                            model, metadata, failure = None, {}, str(error)
                    records, eligible, excluded = _score(model, future)
                    fold_metrics = metrics(records, eligible, excluded)
                    folds.append({'dates': [str(d) for d in window], 'trainingEnd': str(max(_date(r) for r in history)),
                        'featureSchema': list(model.feature_schema) if model else [],
                        'calibration': metadata.get('calibration'), 'moveDecisionThreshold': metadata.get('moveDecisionThreshold'),
                        'metrics': fold_metrics, 'failure': failure})
                    if window == windows[-1]:
                        exact_metrics = fold_metrics
                    else:
                        comparison_records.extend(records)
                        comparison_excluded.update(excluded)
                        comparison_total += eligible
                    all_records.extend(records); all_excluded.update(excluded); total += eligible
                summary = metrics(all_records, total, all_excluded)
                summary['exactCandidateHoldout'] = exact_metrics
                summary['comparisonOnly'] = metrics(comparison_records, comparison_total, comparison_excluded)
                evidence = {'dataset_revision_id': str(revision), 'instrument_id': instrument,
                    'algorithm': algorithm, 'horizon_minutes': horizon, 'feature_set_version': FEATURE_SET_VERSION_V2,
                    'candidate_model_checksum': candidate['model_checksum'], 'model_version': candidate['model_version'],
                    'artifact_checksum': candidate['artifact_checksum'], 'started_data_date': str(windows[0][0]),
                    'ended_data_date': str(windows[-1][-1]), 'fold_count': len(folds), 'sample_count': len(all_records),
                    'metrics': summary, 'stability': {'daily': _stability(all_records, 'day',
                        expected_days=[str(d) for window in windows for d in window]),
                        'comparisonDaily': _stability(comparison_records, 'day'),
                        'regime': _stability(all_records, 'regime', 30)},
                    'configuration': {**asdict(self.config), 'implementation': IMPLEMENTATION,
                        'labelPolicy': policy_payload(policy), 'folds': folds,
                        'exactCandidateDates': [str(d) for d in windows[-1]],
                        'predictionChecksum': digest(all_records)}}
                checksum = digest(evidence)
                evidence.update(evaluation_id=checksum, evaluation_checksum=checksum)
                self.repository.save_big_move_evaluation(evidence)
                results.append(evidence)
        comparisons = [self._compare([r for r in results if r['horizon_minutes'] == h]) for h in BIG_MOVE_HORIZONS]
        return {'evaluations': results, 'comparisons': comparisons}

    def _compare(self, runs):
        def score(run):
            m = run['metrics']['comparisonOnly']; a, b = m['stageA'], m['stageB']
            daily = [d['movePrecision'] for d in run['stability']['comparisonDaily'] if d['movePrecision'] is not None]
            ready = all(v is not None for v in (a['precision'], a['recall'], a['brier'], b['accuracy'], m['joint']['macroF1']))
            # Fixed research utility, not tuned on evaluation data. Complexity wins no bonus.
            utility = (.25*(a['precision'] or 0)+.15*(a['recall'] or 0)+.15*(1-(a['brier'] if a['brier'] is not None else 1))
                +.15*(b['accuracy'] or 0)+.15*(m['joint']['macroF1'] or 0)+.15*m['coverage']
                -.10*(float(np.std(daily)) if daily else 1))
            return ready, utility, run['algorithm'] == 'LOGISTIC_REGRESSION'
        ranked = sorted(runs, key=score, reverse=True)
        selected, rejected = ranked
        usable = score(selected)[0]
        def numbers(run):
            m=run['metrics']['comparisonOnly']
            return {'movePrecision': m['stageA']['precision'], 'moveRecall': m['stageA']['recall'],
                'brier': m['stageA']['brier'], 'directionAccuracy': m['stageB']['accuracy'],
                'macroF1': m['joint']['macroF1'], 'coverage': m['coverage']}
        left, right = numbers(selected), numbers(rejected)
        comparison = {'dataset_revision_id': selected['dataset_revision_id'], 'instrument_id': selected['instrument_id'],
            'horizon_minutes': selected['horizon_minutes'], 'selected_algorithm': selected['algorithm'] if usable else None,
            'candidate_results': {r['algorithm']: {'evaluationChecksum': r['evaluation_checksum'],
                'modelChecksum': r['candidate_model_checksum'], 'modelVersion': r['model_version'],
                'utility': score(r)[1], 'metrics': numbers(r)} for r in runs},
            'selection_reason': {'rule': 'precision25-recall15-brier15-direction15-macroF115-coverage15-stabilityPenalty10-v1',
                'reason': 'PRE_HOLDOUT_FIXED_RESEARCH_UTILITY; LOGISTIC_WINS_TIES; FINAL_HOLDOUT_UNUSED' if usable else 'INSUFFICIENT_EVIDENCE',
                'rejectedAlgorithm': rejected['algorithm'] if usable else None,
                'metricDifferences': {k: left[k]-right[k] if left[k] is not None and right[k] is not None else None for k in left}}}
        checksum = digest(comparison)
        comparison.update(comparison_id=checksum, comparison_checksum=checksum)
        self.repository.save_big_move_comparison(comparison)
        return comparison


@dataclass(frozen=True)
class BigMoveApprovalPolicy:
    version: str = 'big-move-approval-research-v1'
    minimumPredictions: int = 200
    minimumActualEvents: int = 40
    minimumDirectionSamples: int = 40
    minimumEvaluationDays: int = 3
    minimumMovePrecision: float = .55
    minimumMoveRecall: float = .20
    minimumDirectionAccuracy: float = .52
    maximumBrier: float = .25
    minimumCoverage: float = .75
    minimumExactCandidatePredictions: int = 100
    minimumExactCandidateEvents: int = 20

    def __post_init__(self):
        if not self.version or min(self.minimumPredictions, self.minimumActualEvents, self.minimumDirectionSamples,
            self.minimumEvaluationDays, self.minimumExactCandidatePredictions, self.minimumExactCandidateEvents) < 1:
            raise ValueError('Approval requires a version and positive evidence counts')
        if any(not 0 < v <= 1 for v in (self.minimumMovePrecision, self.minimumMoveRecall,
            self.minimumDirectionAccuracy, self.maximumBrier, self.minimumCoverage)):
            raise ValueError('Approval probability requirements must be in (0,1]')

    def assess(self, evidence):
        reasons, insufficient = [], []
        for name, m, count, events in (
            ('walkForward', evidence['metrics'], self.minimumPredictions, self.minimumActualEvents),
            ('exactCandidate', evidence['metrics']['exactCandidateHoldout'], self.minimumExactCandidatePredictions,
             self.minimumExactCandidateEvents)):
            a, b = m['stageA'], m['stageB']
            for key, value, minimum in (('predictions', a['sampleCount'], count),
                ('actualEvents', a['classCounts']['positive'], events),
                ('directionSamples', b['sampleCount'], self.minimumDirectionSamples if name == 'walkForward' else events)):
                if value < minimum: insufficient.append(f'{name}.{key}: {value} < {minimum}')
            for key, value, threshold, maximum in (
                ('movePrecision', a['precision'], self.minimumMovePrecision, False),
                ('moveRecall', a['recall'], self.minimumMoveRecall, False),
                ('directionAccuracy', b['accuracy'], self.minimumDirectionAccuracy, False),
                ('brier', a['brier'], self.maximumBrier, True),
                ('coverage', m['coverage'], self.minimumCoverage, False)):
                if value is None: insufficient.append(f'{name}.{key}: unavailable')
                elif (value > threshold if maximum else value < threshold):
                    reasons.append(f'{name}.{key}: {value:.6f} violates {"maximum" if maximum else "minimum"} {threshold}')
        days = sum(day['sampleCount'] > 0 for day in evidence['stability']['daily'])
        if days < self.minimumEvaluationDays: insufficient.append(f'evaluationDays: {days} < {self.minimumEvaluationDays}')
        return ('INSUFFICIENT_EVIDENCE' if insufficient else 'FAIL' if reasons else 'PASS',
                insufficient + reasons or ['All versioned research evidence gates passed'])


class BigMoveApprovalService:
    def __init__(self, repository, store=None, policy=None):
        self.repository = repository
        self.training = BigMoveTrainingService(repository, store)
        self.policy = policy or BigMoveApprovalPolicy()

    def approve(self, revision, instrument):
        comparisons = self.repository.big_move_comparisons(revision, instrument)
        results = []
        for horizon in BIG_MOVE_HORIZONS:
            comparison = next((c for c in comparisons if c['horizon_minutes'] == horizon), None)
            if not comparison or not comparison['selected_algorithm']:
                results.append({'horizon': horizon, 'decision': 'INSUFFICIENT_EVIDENCE', 'reasons': ['No completed candidate comparison']})
                continue
            _verify_evidence(comparison, 'comparison_checksum', 'comparison_id')
            chosen = comparison['candidate_results'][comparison['selected_algorithm']]
            evidence = self.repository.big_move_evaluation(chosen['evaluationChecksum'])
            candidate = self.repository.big_move_model_definition(chosen['modelChecksum'])
            _verify_evidence(evidence, 'evaluation_checksum', 'evaluation_id')
            model, manifest = self.training.store.load(candidate)
            if (candidate['status'] != 'CANDIDATE' or candidate['stage'] != 'MOVE_DETECTOR' or
                str(candidate['dataset_revision_id']) != str(revision) or manifest['instrument_id'] != instrument or
                evidence['candidate_model_checksum'] != candidate['model_checksum'] or
                evidence['artifact_checksum'] != candidate['artifact_checksum'] or
                evidence['model_version'] != model.model_version or evidence['horizon_minutes'] != horizon or
                evidence['algorithm'] != comparison['selected_algorithm'] or
                str(evidence['dataset_revision_id']) != str(revision) or evidence['instrument_id'] != instrument or
                evidence['feature_set_version'] != FEATURE_SET_VERSION_V2 or
                evidence['configuration']['labelPolicy'] != manifest['threshold_policy'] or
                evidence['configuration']['exactCandidateDates'] != manifest['reserved_evaluation_dates'] or
                any(evidence['configuration'][k] != v for k, v in manifest['evaluation_configuration'].items()) or
                manifest['last_training_date'] >= min(manifest['reserved_evaluation_dates'])):
                raise ValueError('Evaluation does not belong to this exact candidate/holdout')
            decision, reasons = self.policy.assess(evidence)
            if model.move_decision_threshold is None:
                decision, reasons = 'INSUFFICIENT_EVIDENCE', reasons + ['No historical decision threshold']
            approval = {'candidate_model_checksum': candidate['model_checksum'],
                'evaluation_checksum': evidence['evaluation_checksum'], 'policy_version': self.policy.version,
                'policy_definition': asdict(self.policy), 'decision': decision, 'reasons': reasons,
                'metrics_snapshot': evidence['metrics'], 'comparison_checksum': comparison['comparison_checksum']}
            checksum = digest(approval)
            approval.update(approval_id=checksum, approval_checksum=checksum)
            self.repository.save_big_move_approval(approval)
            if decision == 'PASS':
                # Copy only: no estimator, scaler or calibrator is fitted during promotion.
                approved = deepcopy(model)
                lineage = {'approved_from_model_checksum': candidate['model_checksum'],
                    'approved_from_artifact_checksum': candidate['artifact_checksum'],
                    'evaluation_checksum': evidence['evaluation_checksum'], 'approval_policy_version': self.policy.version,
                    'approval_decision_checksum': checksum}
                approved.model_version = 'big-move-v2-approved-' + digest(lineage)[:32]
                promoted = {**manifest, **lineage, 'status': 'APPROVED', 'model_version': approved.model_version,
                    'training_identity': digest({'candidateTrainingIdentity': manifest['training_identity'], **lineage})}
                artifact = self.training.store.save(approved, promoted)
                definitions = [{**candidate, **artifact, 'stage': stage, 'model_version': approved.model_version,
                    'status': 'APPROVED', 'model_checksum': digest({'artifact': artifact['artifact_checksum'],
                        'stage': stage, 'version': approved.model_version})} for stage in ('MOVE_DETECTOR', 'DIRECTION')]
                self.repository.save_big_move_model_definitions(definitions)
                approval['approvedModelVersion'] = approved.model_version
            results.append(approval)
        return results


def _verify_evidence(record, checksum_key, id_key):
    content = {k: v for k, v in record.items() if k not in (checksum_key, id_key, 'created_at')}
    if digest(content) != record[checksum_key] or record[id_key] != record[checksum_key]:
        raise ValueError('Immutable V2 evidence checksum mismatch')
