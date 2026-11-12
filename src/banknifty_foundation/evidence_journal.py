"""Prospective point-in-time evidence. No probability adjustment or outcome writes."""
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import logging
import math
import os

from .market_memory import FileMarketMemoryRepository, SimilarityEngine
from .news.service import NewsIntelligenceService
from .product_queries import ProductDataReader
from .repository import canonical_json, digest

IMPLEMENTATION = 'prospective-evidence-v1'
HORIZONS = (10, 20, 30)
ALIGNMENTS = ('SUPPORTS_MODEL', 'CONTRADICTS_MODEL', 'NEUTRAL', 'UNAVAILABLE')
CONFIRMATIONS = ('CONFIRMED', 'PARTIALLY_CONFIRMED', 'CONTRADICTED', 'INSUFFICIENT_DATA')
COMBINED = ('MODEL_ONLY', 'MODEL_PLUS_MEMORY', 'MODEL_PLUS_NEWS', 'MODEL_PLUS_MEMORY_AND_NEWS',
            'MODEL_CONTRADICTED_BY_MEMORY', 'MODEL_CONTRADICTED_BY_NEWS')
log = logging.getLogger(__name__)


def _plain(value):
    if isinstance(value, Decimal): return float(value)
    if isinstance(value, datetime): return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, dict): return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)): return [_plain(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value): return None
    return value


def _bias(up, down, available):
    if not available or not isinstance(up, (int, float)) or not isinstance(down, (int, float)):
        return 'UNAVAILABLE'
    return 'UP' if up > down else 'DOWN' if down > up else 'NEUTRAL'


def _alignment(baseline, bias):
    if bias == 'UNAVAILABLE': return 'UNAVAILABLE'
    if baseline['state'] != 'BIG_MOVE' or bias == 'NEUTRAL': return 'NEUTRAL'
    return 'SUPPORTS_MODEL' if bias == baseline['direction'] else 'CONTRADICTS_MODEL'


class EvidenceJournalService:
    def __init__(self, memory=None, news=None, clock=None):
        self.memory = memory or FileMarketMemoryRepository()
        self.news = news
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def prepare(self, snapshot):
        """Once per market snapshot, before atomic inserts for each horizon.

        Read numerical files before stamping the decision: file evidence really
        existed at this runtime decision. Exclude episodes whose outcomes had not
        matured, in addition to the SimilarityEngine's strict as-of cutoff.
        """
        values = _plain(snapshot.values)
        try:
            episodes = [e for e in self.memory.episodes() if e.as_of < snapshot.as_of
                        and e.as_of + timedelta(minutes=31) <= snapshot.as_of]
            numerical = SimilarityEngine().search({**values, '_as_of': snapshot.as_of}, episodes)
            episode_checksum = digest([{'id': e.episode_id, 'asOf': e.as_of, 'state': e.state,
                                        'outcome': e.outcome} for e in episodes])
        except Exception:
            log.exception('prospective_numerical_memory_unavailable')
            numerical = {'available': False, 'reason': 'MEMORY_READ_FAILED', 'matchedEpisodes': 0,
                         'similarityStrength': 'UNAVAILABLE', 'historicalOutcome': {}, 'topMatches': []}
            episode_checksum = None
        decision_at = self.clock()
        if decision_at.tzinfo is None or decision_at < snapshot.as_of + timedelta(minutes=1):
            raise ValueError('Decision must follow the completed market candle')
        decision_at = decision_at.astimezone(timezone.utc)
        try:
            if self.news is None: self.news = NewsIntelligenceService()
            news = self.news.memory_as_of(decision_at, values)
        except Exception:
            log.exception('prospective_news_memory_unavailable')
            news = {'available': False, 'reason': 'AS_OF_NEWS_READ_FAILED'}
        return {'decision_at': decision_at, 'numerical': _plain(numerical), 'news': _plain(news),
                'values': values, 'raw_values': json.loads(canonical_json(snapshot.values)),
                'episode_checksum': episode_checksum}

    def capture(self, prediction, snapshot, shared):
        """Use the already-generated prediction and feature snapshot; never rerun inference."""
        lineage = prediction['session_bias']['runtime_lineage']
        horizon = prediction['horizon_minutes']
        if prediction['quality_status'] != 'PASS' or prediction['state'] not in ('BIG_MOVE', 'NO_BIG_MOVE'):
            raise ValueError('Only real ready V2 predictions may be journalled')
        if (snapshot.as_of != prediction['as_of'] or _plain(snapshot.values) != shared['values']
            or snapshot.source_max_timestamp > snapshot.as_of or snapshot.quality_status != 'PASS'):
            raise ValueError('Evidence snapshot does not match inference inputs')
        baseline = {'horizon': horizon, 'modelVersion': prediction['model_version'],
            'featureSetVersion': prediction['feature_set_version'], 'qualityStatus': prediction['quality_status'],
            'moveDecisionThreshold': lineage['move_decision_threshold'],
            'rawBigMoveProbability': lineage['raw_big_move_probability'],
            'rawUpGivenBigMoveProbability': lineage['raw_up_given_big_move_probability']}
        for key, column in {'state': 'state', 'bigMoveProbability': 'big_move_probability',
            'quietProbability': 'quiet_probability', 'direction': 'direction', 'directionProbability': 'direction_probability',
            'upGivenBigMoveProbability': 'up_given_big_move_probability', 'downGivenBigMoveProbability': 'down_given_big_move_probability',
            'expectedMovePoints': 'expected_move_points', 'expectedMovePercent': 'expected_move_percent',
            'referencePrice': 'reference_price', 'marketRegime': 'market_regime', 'sessionBias': 'session_bias'}.items():
            baseline[key] = prediction[column]
        numerical, news = shared['numerical'], shared['news']
        stats = numerical.get('historicalOutcome') or {}
        numerical_bias = _bias(stats.get(f'up{horizon}Rate'), stats.get(f'down{horizon}Rate'),
                               numerical.get('available') and numerical.get('matchedEpisodes', 0) > 0)
        historical = news.get('historicalMemory') or {}
        reaction = (historical.get('historicalReaction') or {}).get(str(horizon)) or {}
        news_bias = _bias(reaction.get('upRate'), reaction.get('downRate'),
                         historical.get('available') and reaction.get('sampleSize', 0) > 0)
        event = news.get('event') or {}
        confirmation = (news.get('marketConfirmation') or {}).get('state', 'INSUFFICIENT_DATA')
        event_bias = {'BULLISH': 'UP', 'BEARISH': 'DOWN'}.get(event.get('directional_context'), 'UNAVAILABLE')
        confirmation_bias = (event_bias if confirmation == 'CONFIRMED' else
            ('DOWN' if event_bias == 'UP' else 'UP') if confirmation == 'CONTRADICTED' and event_bias != 'UNAVAILABLE' else
            'NEUTRAL' if confirmation == 'PARTIALLY_CONFIRMED' else 'UNAVAILABLE')
        alignment = {'numericalMemory': _alignment(baseline, numerical_bias),
            'newsMemory': _alignment(baseline, news_bias), 'marketConfirmation': _alignment(baseline, confirmation_bias),
            'numericalDirectionBias': numerical_bias, 'newsDirectionBias': news_bias,
            'confirmationState': confirmation, 'numericalHorizonStatistics': {
                key: stats.get(key) for key in (f'bigMove{horizon}Rate', f'up{horizon}Rate', f'down{horizon}Rate')},
            'newsHorizonStatistics': reaction, 'ruleVersion': 'direction-comparison-v1'}
        values = shared['raw_values']  # Decimal strings preserve the exact feature snapshot.
        futures = {k: v for k, v in values.items() if k.startswith('futures_')}
        option_keys = ('pcr_oi', 'oi_change_imbalance', 'volume_pcr', 'mean_bid_ask_spread_pct',
                       'aggregate_depth_imbalance', 'call_oi_wall', 'put_oi_wall', 'distance_to_call_oi_wall',
                       'distance_to_put_oi_wall', 'oi_concentration_entropy', 'selected_universe_completeness')
        options = {k: v for k, v in values.items() if k in option_keys or k.startswith(('atm_', 'total_ce_', 'total_pe_', 'option_'))}
        price = {k: v for k, v in values.items() if k not in futures and k not in options}
        decision = shared['decision_at']
        journal = {'journal_id': digest({'prediction_id': prediction['prediction_id'], 'implementation': IMPLEMENTATION}),
            'prediction_id': prediction['prediction_id'], 'instrument_id': prediction['instrument_id'],
            'market_as_of': prediction['as_of'], 'decision_at': decision, 'horizon_minutes': horizon,
            'model_version': prediction['model_version'], 'move_model_checksum': prediction['move_model_checksum'],
            'direction_model_checksum': prediction['direction_model_checksum'],
            'evaluation_checksum': lineage['evaluation_checksum'], 'approval_checksum': lineage['approval_checksum'],
            'approval_policy_version': lineage['approval_policy_version'], 'baseline_prediction': _plain(baseline),
            'market_evidence': {'PRICE': price, 'FUTURES': futures, 'OPTIONS': options,
                'REGIME': {'marketRegime': snapshot.market_regime, 'sessionBias': _plain(snapshot.session_bias)},
                'fullFeatureValues': values, 'thresholdPolicy': lineage['policy'], 'thresholdUsed': lineage['threshold'],
                'referenceCandle': lineage['reference']},
            'numerical_memory': numerical, 'news_memory': news, 'evidence_alignment': alignment,
            'source_watermarks': {'marketDataThrough': snapshot.as_of.isoformat(), 'decisionAt': decision.isoformat(),
                'memoryEpisodeCutoff': snapshot.as_of.isoformat(), 'newsFirstSeenCutoff': decision.isoformat(),
                'selectedNewsEventFirstSeenAt': event.get('first_seen_at'), 'selectedNewsEventId': event.get('event_id'),
                'modelEvaluationChecksum': lineage['evaluation_checksum'], 'modelApprovalChecksum': lineage['approval_checksum'],
                'journalImplementationVersion': IMPLEMENTATION, 'memorySourceChecksum': shared['episode_checksum']}}
        # Canonical timestamps must survive timestamptz round-trips unchanged.
        journal['journal_checksum'] = digest(_plain(journal))
        return journal


def _groups(alignment):
    memory, news = alignment.get('numericalMemory', 'UNAVAILABLE'), alignment.get('newsMemory', 'UNAVAILABLE')
    result = []
    if memory == 'CONTRADICTS_MODEL': result.append('MODEL_CONTRADICTED_BY_MEMORY')
    if news == 'CONTRADICTS_MODEL': result.append('MODEL_CONTRADICTED_BY_NEWS')
    if memory == news == 'SUPPORTS_MODEL': result.append('MODEL_PLUS_MEMORY_AND_NEWS')
    elif memory == 'SUPPORTS_MODEL': result.append('MODEL_PLUS_MEMORY')
    elif news == 'SUPPORTS_MODEL': result.append('MODEL_PLUS_NEWS')
    if not result: result.append('MODEL_ONLY')
    return result  # Contradiction cohorts can overlap; never sum them as a partition.


def _accumulate(counts, baseline, actual, quality):
    counts['resolved'] += 1
    if actual not in ('BIG_UP', 'BIG_DOWN', 'NO_BIG_MOVE') or quality != 'VALID':
        counts['excluded'] += 1
        return
    counts['samples'] += 1
    predicted_move = baseline['state'] == 'BIG_MOVE'
    actual_move = actual != 'NO_BIG_MOVE'
    counts['predictedMove'] += int(predicted_move)
    counts['actualMove'] += int(actual_move)
    counts['trueMove'] += int(predicted_move and actual_move)
    if not actual_move:
        counts['actualQuiet'] += 1
        counts['correctQuiet'] += int(baseline['state'] == 'NO_BIG_MOVE')
    up = baseline.get('upGivenBigMoveProbability')
    if actual_move and isinstance(up, (int, float)):
        counts['directionSamples'] += 1
        counts['correctDirection'] += int(('BIG_UP' if up >= .5 else 'BIG_DOWN') == actual)


def _metrics(c, minimum, base=None):
    def ratio(n, d): return c[n]/c[d] if c[d] else None
    result = {'samples': c['samples'], 'resolvedSamples': c['resolved'], 'excludedOutcomes': c['excluded'],
        'predictedBigMoveCount': c['predictedMove'], 'actualBigMoveCount': c['actualMove'],
        'movePrecision': ratio('trueMove', 'predictedMove'), 'moveRecall': ratio('trueMove', 'actualMove'),
        'directionSamples': c['directionSamples'], 'directionAccuracy': ratio('correctDirection', 'directionSamples'),
        'noBigMoveAccuracy': ratio('correctQuiet', 'actualQuiet'), 'coverage': ratio('samples', 'resolved'),
        'actualBigMoveRate': ratio('actualMove', 'samples'),
        'status': 'DESCRIPTIVE_ONLY' if c['samples'] >= minimum else 'INSUFFICIENT_SAMPLE',
        'movePrecisionDifference': None, 'directionAccuracyDifference': None}
    if base and result['samples'] >= minimum and base['samples'] >= minimum:
        for metric, denominator in (('movePrecision', 'predictedBigMoveCount'), ('directionAccuracy', 'directionSamples')):
            if result[denominator] >= minimum and base[denominator] >= minimum:
                result[metric+'Difference'] = result[metric]-base[metric]
    return result


class EvidenceAnalysisService:
    def __init__(self, reader=None, minimum_sample=None):
        self.reader = reader or ProductDataReader()
        self.minimum = int(minimum_sample if minimum_sample is not None else os.getenv('EVIDENCE_MINIMUM_SAMPLE', '20'))
        if self.minimum < 1: raise ValueError('EVIDENCE_MINIMUM_SAMPLE must be positive')

    def summary(self):
        buckets = {h: {'baseline': Counter(), 'numericalMemory': {s: Counter() for s in ALIGNMENTS},
            'newsMemory': {s: Counter() for s in ALIGNMENTS}, 'confirmation': {s: Counter() for s in CONFIRMATIONS},
            'combined': {s: Counter() for s in COMBINED}, 'contradicted': Counter()} for h in HORIZONS}
        with self.reader._connection() as db:
            # Server-side cursor keeps long-running journals lightweight on the target laptop.
            with db.cursor(name='evidence_summary') as cursor:
                cursor.execute('''SELECT j.horizon_minutes,j.baseline_prediction,j.evidence_alignment,
                    o.actual_label,o.quality_status FROM big_move_evidence_journal j
                    JOIN big_move_predictions p USING(prediction_id)
                    JOIN big_move_prediction_outcomes o USING(prediction_id)''')
                for row in cursor:
                    b = buckets[row['horizon_minutes']]
                    baseline, alignment = row['baseline_prediction'], row['evidence_alignment']
                    selected = [b['baseline']]
                    for channel in ('numericalMemory', 'newsMemory'):
                        selected.append(b[channel][alignment.get(channel, 'UNAVAILABLE')])
                    selected.append(b['confirmation'][alignment.get('confirmationState', 'INSUFFICIENT_DATA')])
                    selected.extend(b['combined'][key] for key in _groups(alignment))
                    if any(alignment.get(c) == 'CONTRADICTS_MODEL' for c in ('numericalMemory', 'newsMemory')):
                        selected.append(b['contradicted'])
                    for counts in selected: _accumulate(counts, baseline, row['actual_label'], row['quality_status'])
        horizons = []
        for h, groups in buckets.items():
            baseline = _metrics(groups['baseline'], self.minimum)
            horizons.append({'minutes': h, 'baseline': baseline,
                **{channel: {key: _metrics(c, self.minimum, baseline) for key, c in groups[channel].items()}
                   for channel in ('numericalMemory', 'newsMemory', 'confirmation', 'combined')},
                'contradicted': _metrics(groups['contradicted'], self.minimum, baseline)})
        total = sum(b['baseline']['resolved'] for b in buckets.values())
        return {'available': total > 0, 'resolvedPredictions': total, 'horizons': horizons, 'minimumSample': self.minimum,
            'notice': 'Prospective observational evidence; no model probabilities are modified. DESCRIPTIVE_ONLY, not statistically significant.',
            'definitions': {'coverage': 'Valid scored outcomes / all resolved outcomes; UNKNOWN, AMBIGUOUS and invalid outcomes excluded.',
                'directionAccuracy': 'Conditional direction on actual BIG_MOVE outcomes, including quiet predictions.',
                'noBigMoveAccuracy': 'Correct quiet predictions / actual NO_BIG_MOVE outcomes.',
                'population': 'Horizon summaries pool model versions; overlapping minute targets are not independent trials. No causal or significance claim.',
                'lift': 'Differences require minimum samples in both metric denominators and both cohorts.',
                'combined': 'Contradiction groups may overlap support groups; cohort counts are not additive.'}}

    def journal(self, *, date_value=None, horizon=None, resolved=None, page=1, page_size=25, prediction_id=None):
        clauses, parameters = [], []
        if date_value:
            clauses.append("(j.market_as_of AT TIME ZONE 'Asia/Kolkata')::date=%s"); parameters.append(date_value)
        if horizon is not None:
            if horizon not in HORIZONS: raise ValueError('Unsupported horizon')
            clauses.append('j.horizon_minutes=%s'); parameters.append(horizon)
        if resolved is not None: clauses.append('o.prediction_id IS NOT NULL' if resolved else 'o.prediction_id IS NULL')
        if prediction_id:
            clauses.append('j.prediction_id=%s'); parameters.append(prediction_id)
        if page < 1 or not 1 <= page_size <= 100: raise ValueError('Invalid pagination')
        join = ''' FROM big_move_evidence_journal j JOIN big_move_predictions p USING(prediction_id)
            LEFT JOIN big_move_prediction_outcomes o USING(prediction_id)'''
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        with self.reader._connection() as db:
            total = db.execute('SELECT count(*) AS total'+join+where, parameters).fetchone()['total']
            rows = db.execute('''SELECT j.*,o.actual_label,o.quality_status AS outcome_quality,
                o.attached_at,o.realized_summary'''+join+where+' ORDER BY j.market_as_of DESC,j.prediction_id LIMIT %s OFFSET %s',
                [*parameters, page_size, (page-1)*page_size]).fetchall()
        items = [_plain({**row, 'resolved': row['actual_label'] is not None}) for row in rows]
        return {'available': bool(items), 'reason': None if items else 'NO_PROSPECTIVE_EVIDENCE',
                'items': items, 'total': total, 'page': page, 'pageSize': page_size}
