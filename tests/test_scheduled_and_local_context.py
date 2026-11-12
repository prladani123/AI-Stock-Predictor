from __future__ import annotations

from datetime import date, datetime, timezone
import json

import pytest

from banknifty_foundation.context_v3 import ContextV3ActivationService
from banknifty_foundation.news.scheduled import scheduled_events
import banknifty_foundation.research_context as research_context
from banknifty_foundation.research_context import (
    LocalResearchContextImportService,
    ResearchContextBackfillService,
    _local_observations,
    _stamp,
)


def _event(**overrides):
    value = {
        'eventType': 'INDIA_CPI',
        'title': 'CPI release',
        'scheduledDate': '2026-07-13',
        'calendarDate': '2026-07-13',
        'scheduledAt': '2026-07-13T17:30:00+05:30',
        'timingPrecision': 'EXACT_TIME',
        'source': 'official source',
        'sourceUrl': 'https://example.test/releases',
        'sourceObservedAt': '2026-07-01T00:00:00+05:30',
        'country': 'IN',
        'bankniftyRelevance': 'MACRO_CALENDAR',
        'meetingStart': None,
        'meetingEnd': None,
    }
    value.update(overrides)
    return value


def _configuration():
    return {'events': [
        _event(scheduledDate='2026-07-09', scheduledAt='2026-07-09T17:30:00+05:30'),
        _event(),
        _event(eventType='INDIA_WPI', title='WPI release', scheduledDate='2026-07-14',
            calendarDate='2026-07-14', scheduledAt=None, timingPrecision='DATE_ONLY'),
    ]}


def test_scheduled_configuration_is_independent_of_visible_upcoming_events():
    schedule = scheduled_events(now='2026-07-10T09:00:00+05:30', configuration=_configuration())
    assert schedule['status'] == 'CONFIGURED'
    assert schedule['configuredEventCount'] == 3
    assert schedule['visibleEventCount'] == 2
    assert [event['eventType'] for event in schedule['visibleEvents']] == ['INDIA_CPI', 'INDIA_WPI']

    after_release = scheduled_events(now='2026-07-13T18:00:00+05:30', configuration=_configuration())
    assert after_release['status'] == 'CONFIGURED'
    assert after_release['configuredEventCount'] == 3
    assert [event['eventType'] for event in after_release['visibleEvents']] == ['INDIA_WPI']
    assert after_release['visibleEvents'][0]['scheduledAt'] is None


def _series(**overrides):
    value = {
        'provider': 'LOCAL_VENDOR',
        'instrument': 'NIFTY50',
        'observed_at': '2026-07-13T09:15:00+05:30',
        'source_latency_seconds': 5,
        'values': {'close': 100},
    }
    value.update(overrides)
    return value


def test_local_observations_deduplicate_and_reject_ambiguous_input():
    source_at = _stamp('2026-10-24T12:00:00+05:30')
    observations = _local_observations([_series(), _series()], source_observed_at=source_at,
        source_name='verified export')
    assert len(observations) == 1
    assert observations[0].provider == 'LOCAL_VENDOR'
    assert observations[0].effective_at == _stamp('2026-07-13T09:16:05+05:30')

    conflict = _series(values={'close': 101})
    with pytest.raises(ValueError, match='LOCAL_IMPORT_DUPLICATE_CONFLICT'):
        _local_observations([_series(), conflict], source_observed_at=source_at,
            source_name='verified export')
    with pytest.raises(ValueError, match='LOCAL_IMPORT_OPTION_PROVIDER_UNSUPPORTED'):
        _local_observations([_series(instrument='BANKNIFTY_OPTIONS', values={'side': 'CE', 'offset': 0})],
            source_observed_at=source_at, source_name='verified export')
    with pytest.raises(ValueError, match='LOCAL_IMPORT_EFFECTIVE_BEFORE_OBSERVED'):
        _local_observations([_series(effective_at='2026-07-13T09:14:00+05:30')],
            source_observed_at=source_at, source_name='verified export')


class _FakeRepository:
    url = ''
    def __init__(self):
        self.saved = None
    def session(self, day):
        return {'status': 'OPEN'}
    def save(self, run, rows):
        self.saved = (run, rows)
    def connection(self):
        raise ValueError('RESEARCH_CONTEXT_DATABASE_NOT_CONFIGURED')


class _FakeEnricher:
    def derive_day(self, day, bounds, series, option_rows, information):
        return [{'session_date': day, 'as_of': bounds[0], 'quality_status': 'PASS'}]


def test_local_import_reports_skipped_dates_as_partial(tmp_path, monkeypatch):
    input_path = tmp_path / 'observations.json'
    input_path.write_text(json.dumps([
        _series(),
        _series(observed_at='2026-07-15T09:15:00+05:30'),
    ]), encoding='utf-8')
    repository = _FakeRepository()
    service = LocalResearchContextImportService(repository=repository, root=tmp_path)
    service.enricher = _FakeEnricher()
    monkeypatch.setattr(research_context, 'canonical_dates', lambda root: [date(2026, 7, 13)])
    monkeypatch.setattr(research_context, '_index_bounds', lambda session, day: (
        datetime(2026, 7, 13, 3, 45, tzinfo=timezone.utc),
        datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)))

    result = service.import_file(input_path, instrument_id=1, source_name='verified export',
        source_observed_at=datetime.now(timezone.utc).isoformat())
    assert result['status'] == 'PARTIAL'
    assert result['rows'] == 1
    assert result['skippedDates'] == [{'date': '2026-07-15',
        'status': 'LOCAL_IMPORT_REQUIRES_COMPLETED_CANONICAL_DATE'}]
    assert repository.saved[0]['status'] == 'PARTIAL'


def test_backfill_rejects_missing_input_directory_before_date_queries(tmp_path):
    service = ResearchContextBackfillService(repository=_FakeRepository(), root=tmp_path)
    with pytest.raises(ValueError, match='LOCAL_INPUT_DIRECTORY_NOT_FOUND'):
        service.backfill(instrument_id=1, input_dir=tmp_path / 'missing',
            source_name='verified export', source_observed_at='2026-10-24T12:00:00+05:30')


def test_activation_profile_selection_rejects_unknown_and_mixed_all():
    assert ContextV3ActivationService._profiles('all')
    assert ContextV3ActivationService._profiles('banknifty-price-vol-v3,banknifty-price-vol-v3') == [
        'banknifty-price-vol-v3']
    with pytest.raises(ValueError, match='UNKNOWN_PROFILE'):
        ContextV3ActivationService._profiles('unknown')
    with pytest.raises(ValueError, match='ALL_PROFILES_MUST_BE_EXCLUSIVE'):
        ContextV3ActivationService._profiles('all,unknown')
