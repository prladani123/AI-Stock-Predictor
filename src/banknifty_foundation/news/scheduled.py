"""Configured, source-backed scheduled events only.

The configuration may be a legacy list or a versioned object containing
``events`` plus metadata. Event visibility is point-in-time: an event is
visible only when its verified historical knowledge time has passed. Calendar
configuration status is based on the number of valid configured events, not on
how many events happen to be upcoming at the requested decision time.
"""
from datetime import date, datetime, timezone
import json
from pathlib import Path

from ..calendar import IST
from ..paths import memory_root


EVENT_TYPES = ('RBI_MPC', 'INDIA_CPI', 'INDIA_WPI', 'INDIA_GDP', 'INDIA_IIP', 'FED_DECISION')
TIMING_PRECISIONS = ('EXACT_TIME', 'DATE_ONLY')
_REQUIRED = {
    'eventType', 'title', 'scheduledDate', 'source', 'sourceUrl',
    'sourceObservedAt', 'bankniftyRelevance', 'country',
}


def _stamp(value):
    from ..research_context import _stamp as parse
    return parse(value)


def _configured_events(values):
    if isinstance(values, dict):
        events = values.get('events')
        if not isinstance(events, list):
            raise ValueError('Scheduled configuration requires an events list')
        return events
    if isinstance(values, list):
        return values
    raise ValueError('Scheduled configuration must be a list or an object with events')


def _event_knowledge_at(value, observed):
    """Return the verified historical knowledge time, never a fabricated one."""
    publication = value.get('sourcePublicationDate')
    historical = value.get('historicalKnowledgeAvailableFrom')
    basis = value.get('historicalKnowledgeBasis')
    reference = value.get('sourceReference')
    provenance = (publication, historical, basis, reference)
    if any(item is not None for item in provenance) and not all(
            isinstance(item, str) and item.strip() for item in provenance):
        raise ValueError('Incomplete historical scheduled-event provenance')
    if historical is None:
        return observed
    known = _stamp(historical)
    if known > observed:
        raise ValueError('Historical knowledge cannot follow source observation')
    try:
        published = date.fromisoformat(publication)
    except (TypeError, ValueError):
        raise ValueError('Invalid scheduled-event publication date') from None
    if known.astimezone(IST).date() < published:
        raise ValueError('Historical knowledge precedes publication')
    return known


def _normalize(value):
    if not isinstance(value, dict):
        raise ValueError('Scheduled event must be an object')
    missing = _REQUIRED - value.keys()
    if missing:
        raise ValueError('Incomplete scheduled event configuration')
    event_type = value['eventType']
    if event_type not in EVENT_TYPES:
        raise ValueError('Unsupported scheduled event type')
    url = value['sourceUrl']
    if not isinstance(url, str) or not url.startswith(('https://', 'http://')):
        raise ValueError('Scheduled event needs a source URL')
    day = date.fromisoformat(value['scheduledDate'])
    calendar_day = date.fromisoformat(value['calendarDate']) if value.get('calendarDate') else day
    precision = value.get('timingPrecision')
    if precision is None:
        precision = 'EXACT_TIME' if value.get('scheduledAt') else 'DATE_ONLY'
    if precision not in TIMING_PRECISIONS:
        raise ValueError('Unsupported scheduled-event timing precision')
    at = _stamp(value['scheduledAt']) if value.get('scheduledAt') else None
    if 'expectedReleaseTimeKnown' in value:
        if type(value['expectedReleaseTimeKnown']) is not bool:
            raise ValueError('Release-time certainty must be boolean')
        if value['expectedReleaseTimeKnown'] != (precision == 'EXACT_TIME'):
            raise ValueError('Release-time certainty conflicts with timing precision')
    if precision == 'EXACT_TIME' and at is None:
        raise ValueError('Exact scheduled event requires a source-backed time')
    if precision == 'DATE_ONLY' and at is not None:
        raise ValueError('Date-only scheduled event must not invent a time')
    if at is not None and at.astimezone(IST).date() != day:
        raise ValueError('scheduledDate must be the IST event date')
    meeting_start = date.fromisoformat(value['meetingStart']) if value.get('meetingStart') else None
    meeting_end = date.fromisoformat(value['meetingEnd']) if value.get('meetingEnd') else None
    if (meeting_start is None) != (meeting_end is None) or (
            meeting_start is not None and meeting_start > meeting_end):
        raise ValueError('Scheduled meeting window is invalid')
    observed = _stamp(value['sourceObservedAt'])
    known = _event_knowledge_at(value, observed)
    result = {
        **value,
        'scheduledDate': str(day),
        'calendarDate': str(calendar_day),
        'scheduledAt': at.isoformat() if at else None,
        'timingPrecision': precision,
        'expectedReleaseTimeKnown': precision == 'EXACT_TIME',
        'marketRelevance': value['bankniftyRelevance'],
        'knowledgeAvailableAt': known.isoformat(),
        'sourceObservedAt': observed.isoformat(),
        'meetingStart': str(meeting_start) if meeting_start else None,
        'meetingEnd': str(meeting_end) if meeting_end else None,
    }
    return result


def scheduled_events(now=None, *, root=None, configuration=None):
    """Return normalized visible events and independent configuration state."""
    now = _stamp(now or datetime.now(timezone.utc))
    if configuration is None:
        path = Path(root or memory_root()) / 'config' / 'scheduled_events.json'
        values = json.loads(path.read_text(encoding='utf-8')) if path.exists() else []
    else:
        values = configuration
    configured = [_normalize(value) for value in _configured_events(values)]
    today = now.astimezone(IST).date()
    visible = []
    for event in configured:
        known = _stamp(event['knowledgeAvailableAt'])
        if known > now:
            continue
        event_day = date.fromisoformat(event['scheduledDate'])
        if event['timingPrecision'] == 'EXACT_TIME':
            is_visible = _stamp(event['scheduledAt']) >= now
        else:
            is_visible = event_day >= today
        if is_visible:
            visible.append(event)
    visible.sort(key=lambda item: (
        item['scheduledDate'], item.get('scheduledAt') or '', item['eventType']))
    configured_dates = sorted({item['scheduledDate'] for item in configured})
    return {
        'status': 'CONFIGURED' if configured else 'NOT_CONFIGURED',
        'configuredEventCount': len(configured),
        'configuredDateRange': [configured_dates[0], configured_dates[-1]] if configured_dates else [],
        'configuredEventTypes': sorted({item['eventType'] for item in configured}),
        'visibleEventCount': len(visible),
        'visibleEvents': visible,
        # Legacy consumers use events as the visible upcoming list.
        'events': visible,
    }
