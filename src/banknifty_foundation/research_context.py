"""Research-only historical context. Network access occurs only in explicit enrichment.

Retrospective availability is candle close plus documented source latency, not a
claim of prospective receipt. V2 definitions, live collection and daily memories
are deliberately outside this module's write surface.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
from io import StringIO
from pathlib import Path
from typing import Protocol
from urllib.parse import quote
import base64
import csv
import gzip
import json
import math
import os
import re
import time as clock

import httpx

from .calendar import IST
from .config import ResearchContextSettings
from .paths import memory_root

UTC = timezone.utc
RESEARCH_CONTEXT_VERSION = 'banknifty-research-context-v1'
SOURCE_PRIORITY = ('DHAN_ROLLING_OPTIONS', 'UPSTOX_EXPIRED_OPTIONS', 'UPSTOX_MARKET_INFORMATION')
CONFLICT_RELATIVE_TOLERANCE = .05


def _plain(value):
    if isinstance(value, datetime): return _stamp(value).isoformat()
    if isinstance(value, date): return value.isoformat()
    # PostgreSQL JSONB normalizes numeric representation, including negative zero.
    if isinstance(value, float) and math.isfinite(value) and value.is_integer(): return int(value)
    if isinstance(value, dict): return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)): return [_plain(v) for v in value]
    return value


def _hash(value):
    return sha256(json.dumps(_plain(value), sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _stamp(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000 if value > 100000000000 else value, UTC)
    result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if result.tzinfo is None or result.utcoffset() is None: raise ValueError('Naive provider timestamp')
    return result.astimezone(UTC)


def _number(value, *, positive=False):
    if value is None or isinstance(value, bool): return None
    try: value = float(value)
    except (TypeError, ValueError, OverflowError): return None
    return value if math.isfinite(value) and (value > 0 if positive else value >= 0) else None


def _expired(token):
    # Expiry inspection only; this does not authenticate or trust a JWT signature.
    try:
        part = token.split('.')[1]
        expiry = json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4))).get('exp')
        return expiry is not None and float(expiry) <= datetime.now(UTC).timestamp()
    except (ValueError, IndexError, TypeError): return False


class ResearchProviderError(Exception):
    def __init__(self, state, reason):
        self.state, self.reason = state, reason
        super().__init__(f'{state}: {reason}')  # Fixed codes only, never tokens/provider bodies.


@dataclass(frozen=True)
class ResearchContextObservation:
    provider: str
    instrument: str
    observed_at: datetime
    effective_at: datetime | None
    source_latency: float | None
    granularity_seconds: int | None
    values: dict
    raw_checksum: str
    metadata: dict

    def __post_init__(self):
        _stamp(self.observed_at)
        if self.effective_at is not None and _stamp(self.effective_at) < self.observed_at:
            raise ValueError('Effective timestamp precedes observation')


@dataclass(frozen=True)
class ResearchContextCoverage:
    canonicalSessions: int
    sessionsWithOptionContext: int
    sessionsWithVix: int
    sessionsWithGiftNifty: int
    sessionsWithBrent: int
    sessionsWithUsdInr: int
    sessionsWithHistoricalPCR: int
    contextMinuteRows: int
    sourceConflicts: int
    missingDatesBySource: dict
    status: str = 'PARTIAL'
    sessionsWithHistoricalOI: int = 0
    sessionsWithHistoricalMaxPain: int = 0
    sessionsWithNifty50: int = 0


class ResearchContextProvider(Protocol):
    def configuration_state(self) -> str: ...


class _RawCache:
    def __init__(self, root, provider):
        self.root = Path(root) / 'raw/research_context' / provider
        self.provider = provider

    def _directory(self, request):
        period = str(request.get('fromDate', request.get('date', 'metadata')))
        # Values enter a digest, not an externally supplied filesystem path.
        period = period if len(period) == 10 and period.replace('-', '').isdigit() else 'metadata'
        strike, side = request.get('strike', ''), request.get('drvOptionType', '')
        group = f'{strike}_{side}' if re.fullmatch(r'ATM(?:[+-](?:[1-9]|10))?', strike) and side in ('CALL', 'PUT') else 'metadata' if period == 'metadata' else 'market_data'
        return self.root / period / group / _hash(request)

    def read(self, request):
        records = []
        for path in sorted(self._directory(request).glob('*.json')):
            item = json.loads(path.read_text(encoding='utf-8'))
            if item['provider'] != self.provider or path.stem != item['content_checksum'] or item['request'] != _plain(request) or _hash(item['content']) != item['content_checksum']:
                raise ResearchProviderError('PROVIDER_ERROR', 'RAW_CACHE_INTEGRITY_FAILED')
            records.append(item)
        if len({item['content_checksum'] for item in records}) > 1:
            raise ResearchProviderError('PROVIDER_ERROR', 'SOURCE_CONFLICT_RAW_REVISIONS')
        return records[0] if records else None

    def save(self, request, content, source_url):
        checksum = _hash(content)
        item = {'provider': self.provider, 'request': _plain(request), 'retrieved_at': datetime.now(UTC).isoformat(),
                'content_checksum': checksum, 'date_range': [request.get('fromDate'), request.get('toDate')],
                'source_status': 'SUCCESS', 'source_url': source_url, 'content': content}
        directory = self._directory(request)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f'{checksum}.json'
        # Publish a fully written file with an atomic, non-overwriting hard link.
        from tempfile import NamedTemporaryFile
        with NamedTemporaryFile(mode='w', encoding='utf-8', dir=directory, suffix='.tmp', delete=False) as temporary:
            temporary.write(json.dumps(item, sort_keys=True, allow_nan=False))
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        try:
            try: os.link(temporary_path, target)
            except FileExistsError:
                existing = json.loads(target.read_text(encoding='utf-8'))
                if existing['content_checksum'] != checksum or existing['content'] != content:
                    raise ResearchProviderError('PROVIDER_ERROR', 'IMMUTABLE_RAW_CONFLICT') from None
                return existing
        finally:
            temporary_path.unlink(missing_ok=True)  # Only the exact temp file created above.
        return item


def _http(method, url, headers, *, params=None, payload=None, csv_response=False):
    """Private transport: reject URLs outside the explicit implemented data surface."""
    from urllib.parse import urlsplit
    parsed = urlsplit(url)
    dhan = url == 'https://api.dhan.co/v2/charts/rollingoption' and method == 'POST'
    master = url == 'https://images.dhan.co/api-data/api-scrip-master.csv' and method == 'GET'
    exact = {'/v2/market/pcr', '/v2/market/oi', '/v2/market/change-oi', '/v2/market/max-pain',
             '/v2/option/chain', '/v2/expired-instruments/expiries', '/v2/expired-instruments/option/contract',
             '/v2/expired-instruments/future/contract', '/v2/news/instruments'}
    upstox = (method == 'GET' and parsed.scheme == 'https' and parsed.netloc == 'api.upstox.com'
              and (parsed.path in exact or re.fullmatch(r'/v3/historical-candle/[^/]+/minutes/1/\d{4}-\d{2}-\d{2}/\d{4}-\d{2}-\d{2}', parsed.path)
                   or re.fullmatch(r'/v2/expired-instruments/historical-candle/[^/]+/1minute/\d{4}-\d{2}-\d{2}/\d{4}-\d{2}-\d{2}', parsed.path))
              and not parsed.query and not parsed.fragment)
    if not (dhan or master or upstox): raise ValueError('Research endpoint is not allowlisted')
    for attempt in range(3):
        try:
            with httpx.Client(timeout=30, follow_redirects=False) as client:
                response = client.request(method, url, headers=headers, params=params, json=payload)
        except httpx.HTTPError:
            if attempt == 2: raise ResearchProviderError('PROVIDER_ERROR', 'TRANSPORT_UNAVAILABLE') from None
            clock.sleep(2 ** attempt)
            continue
        if response.status_code == 401: raise ResearchProviderError('TOKEN_EXPIRED', 'AUTHENTICATION_REJECTED')
        if response.status_code in (402, 403): raise ResearchProviderError('SUBSCRIPTION_REQUIRED', 'DATA_PLAN_INACTIVE_OR_PERMISSION_DENIED')
        if response.status_code == 429 or response.status_code >= 500:
            if attempt == 2: raise ResearchProviderError('PROVIDER_ERROR', 'RATE_LIMIT_OR_PROVIDER_UNAVAILABLE')
            clock.sleep(min(30, 2 ** (attempt + 1)))
            continue
        if response.status_code != 200: raise ResearchProviderError('PROVIDER_ERROR', 'DATA_REQUEST_REJECTED')
        if csv_response: return response.text
        try: content = response.json()
        except ValueError: raise ResearchProviderError('PROVIDER_ERROR', 'INVALID_PROVIDER_JSON') from None
        if not isinstance(content, dict): raise ResearchProviderError('PROVIDER_ERROR', 'INVALID_PROVIDER_ENVELOPE')
        if content.get('status') in ('failure', 'error') or content.get('errorCode'):
            message = json.dumps(content).lower()
            if 'token' in message and ('expir' in message or 'invalid' in message):
                raise ResearchProviderError('TOKEN_EXPIRED', 'AUTHENTICATION_REJECTED')
            if 'subscri' in message or 'data plan' in message:
                raise ResearchProviderError('SUBSCRIPTION_REQUIRED', 'DATA_PLAN_INACTIVE')
            raise ResearchProviderError('PROVIDER_ERROR', 'PROVIDER_REJECTED_REQUEST')
        return content
    raise ResearchProviderError('PROVIDER_ERROR', 'RETRIES_EXHAUSTED')


class DhanHistoricalOptionProvider:
    MASTER_URL = 'https://images.dhan.co/api-data/api-scrip-master.csv'
    OPTIONS_URL = 'https://api.dhan.co/v2/charts/rollingoption'

    def __init__(self, settings=None, root=None):
        self._settings = settings or ResearchContextSettings.from_env()
        self._cache = _RawCache(root or memory_root(), 'dhan')
        self._last_request = 0.0
        self.source_manifest = []
        self._resolved_identity = None
        self._runtime_state = None

    def configuration_state(self):
        s = self._settings
        if not s.dhan_enabled: return 'DISABLED'
        if not s.dhan_token or not s.dhan_client_id: return 'NOT_CONFIGURED'
        if self._runtime_state: return self._runtime_state
        if _expired(s.dhan_token): return 'TOKEN_EXPIRED'
        return 'READY_FOR_REQUEST' if s.dhan_data_enabled else 'SUBSCRIPTION_REQUIRED'

    def _retrieve(self, request, *, metadata=False, missing_only=True):
        cached = self._cache.read(request) if missing_only else None
        if cached is None:
            state = self.configuration_state()
            if state != 'READY_FOR_REQUEST': raise ResearchProviderError(state, 'DHAN_RESEARCH_NOT_READY')
            clock.sleep(max(0, 1.05 - (clock.monotonic() - self._last_request)))
            self._last_request = clock.monotonic()
            url = self.MASTER_URL if metadata else self.OPTIONS_URL
            headers = {} if metadata else {'access-token': self._settings.dhan_token, 'client-id': self._settings.dhan_client_id}
            try:
                content = _http('GET' if metadata else 'POST', url, headers,
                                payload=None if metadata else request, csv_response=metadata)
            except ResearchProviderError as error:
                if error.state in ('TOKEN_EXPIRED', 'SUBSCRIPTION_REQUIRED'): self._runtime_state = error.state
                raise
            cached = self._cache.save(request, content, url)
        self.source_manifest.append({key: cached[key] for key in ('provider', 'request', 'content_checksum', 'retrieved_at', 'source_url')})
        return cached

    def resolve_banknifty(self):
        if self._resolved_identity:
            self.source_manifest.append(self._resolved_identity)
            return self._resolved_identity['resolvedIdentity']
        cached = self._retrieve({'operation': 'instrument_master'}, metadata=True)
        matches = {}
        for row in csv.DictReader(StringIO(cached['content'].lstrip('\ufeff'))):
            normalized = {key.strip().upper(): str(value).strip() for key, value in row.items() if key}
            exchange = normalized.get('SEM_EXM_EXCH_ID', normalized.get('EXCH_ID'))
            segment = normalized.get('SEM_SEGMENT', normalized.get('SEGMENT'))
            instrument = normalized.get('SEM_INSTRUMENT_NAME', normalized.get('INSTRUMENT'))
            symbols = [normalized.get(key, '').upper().replace(' ', '') for key in ('SEM_TRADING_SYMBOL', 'SEM_CUSTOM_SYMBOL', 'SYMBOL_NAME', 'DISPLAY_NAME')]
            if exchange == 'NSE' and segment in ('I', 'IDX_I', 'INDEX') and instrument in ('INDEX', 'IDX') and any(symbol in ('BANKNIFTY', 'NIFTYBANK') for symbol in symbols):
                identity = normalized.get('SEM_SMST_SECURITY_ID', normalized.get('SECURITY_ID'))
                if identity and identity.isdigit(): matches[identity] = normalized
        if len(matches) != 1: raise ResearchProviderError('PROVIDER_ERROR', 'AMBIGUOUS_OR_MISSING_BANKNIFTY_INDEX_IDENTITY')
        security_id = next(iter(matches))
        self._resolved_identity = {'resolvedIdentity': security_id, 'underlying': 'BANKNIFTY', 'indexSegment': 'IDX_I',
            'metadataChecksum': cached['content_checksum'], 'sourceUrl': self.MASTER_URL}
        self.source_manifest.append(self._resolved_identity)
        return security_id

    def option_history(self, day, *, missing_only=True):
        if not isinstance(day, date) or isinstance(day, datetime):
            raise ValueError('DHAN_OPTION_HISTORY_REQUIRES_A_DATE')
        security_id = self.resolve_banknifty()
        observations = []
        for offset in range(-self._settings.option_width, self._settings.option_width + 1):
            strike = 'ATM' if offset == 0 else f'ATM{offset:+d}'
            for side, api_side in (('CE', 'CALL'), ('PE', 'PUT')):
                request = {'exchangeSegment': 'NSE_FNO', 'instrument': 'OPTIDX', 'securityId': security_id,
                    'expiryFlag': 'MONTH', 'expiryCode': 0, 'interval': '1', 'strike': strike,
                    'drvOptionType': api_side, 'requiredData': ['open', 'high', 'low', 'close', 'iv', 'volume', 'strike', 'oi', 'spot'],
                    'fromDate': day.isoformat(), 'toDate': (day + timedelta(days=1)).isoformat()}
                raw = self._retrieve(request, missing_only=missing_only)
                data = raw['content'].get('data', {})
                data = data.get(side.lower(), data)
                stamps = data.get('timestamp', [])
                if not isinstance(stamps, list): raise ResearchProviderError('PROVIDER_ERROR', 'OPTION_TIMESTAMPS_INVALID')
                for key in request['requiredData']:
                    if not isinstance(data.get(key), list) or len(data[key]) != len(stamps):
                        raise ResearchProviderError('PROVIDER_ERROR', 'OPTION_ARRAY_LENGTH_MISMATCH')
                seen = set()
                for index, stamp in enumerate(stamps):
                    at = _stamp(stamp)
                    if at.astimezone(IST).date() != day: continue
                    if at in seen or at.second or at.microsecond:
                        raise ResearchProviderError('PROVIDER_ERROR', 'DUPLICATE_OR_NON_MINUTE_OPTION_TIMESTAMP')
                    seen.add(at)
                    values = {key: _number(data[key][index], positive=key in ('open', 'high', 'low', 'close', 'spot', 'strike')) for key in request['requiredData']}
                    expiry = data.get('expiry')
                    expiry = expiry[index] if isinstance(expiry, list) else expiry
                    if expiry: expiry = date.fromisoformat(str(expiry)[:10]).isoformat()
                    expiry_type = data.get('expiry_type', data.get('expiryType'))
                    expiry_type = expiry_type[index] if isinstance(expiry_type, list) else expiry_type
                    expiry_type = {'MONTH': 'MONTHLY', 'QUARTER': 'QUARTERLY'}.get(str(expiry_type).upper(), str(expiry_type).upper())
                    if expiry_type not in ('MONTHLY', 'QUARTERLY'): expiry_type = 'UNKNOWN'
                    values.update(side=side, offset=offset, expiry=expiry, expiry_type=expiry_type,
                                  contract_identity=f'{expiry or str(day) + ":ROLLING_MONTH_0"}:{values["strike"]}:{side}')
                    observations.append(ResearchContextObservation('DHAN_ROLLING_OPTIONS', 'BANKNIFTY_OPTIONS', at,
                        at + timedelta(minutes=1), 0, 60, values, raw['content_checksum'],
                        {'availabilityBasis': 'ONE_MINUTE_CANDLE_CLOSE', 'underlyingSecurityId': security_id, 'requestedExpiryFlag': 'MONTH'}))
        return observations


class _UpstoxAnalyticsBase:
    INSTRUMENTS = {'BANKNIFTY': 'NSE_INDEX|Nifty Bank', 'NIFTY50': 'NSE_INDEX|Nifty 50', 'INDIA_VIX': 'NSE_INDEX|India VIX',
                   'GIFT_NIFTY': 'GLOBAL_INDEX|SGX NIFTY', 'BRENT': 'GLOBAL_INDICATOR|BZUSD'}
    MARKET_ENDPOINTS = {'PCR': '/v2/market/pcr', 'OI': '/v2/market/oi',
                        'CHANGE_OI': '/v2/market/change-oi', 'MAX_PAIN': '/v2/market/max-pain'}

    def __init__(self, settings=None, root=None):
        self._settings = settings or ResearchContextSettings.from_env()
        self._root = Path(root or memory_root())
        self._cache = _RawCache(self._root, 'upstox')
        self._last_request = 0.0
        self.source_manifest = []
        self._runtime_state = None
        self._plus_unavailable = None

    def configuration_state(self):
        if not self._settings.upstox_enabled: return 'DISABLED'
        if not self._settings.upstox_token: return 'NOT_CONFIGURED'
        if self._runtime_state: return self._runtime_state
        return 'TOKEN_EXPIRED' if _expired(self._settings.upstox_token) else 'READY_FOR_REQUEST'

    def plus_state(self):
        return (self._plus_unavailable or self.configuration_state()) if self._settings.upstox_plus_enabled else 'DISABLED'

    def _get(self, path, params, *, missing_only=True):
        request = {'path': path, **params}
        cached = self._cache.read(request) if missing_only else None
        if cached is None:
            state = self.configuration_state()
            if state != 'READY_FOR_REQUEST': raise ResearchProviderError(state, 'UPSTOX_ANALYTICS_NOT_READY')
            clock.sleep(max(0, 1.05 - (clock.monotonic() - self._last_request)))
            self._last_request = clock.monotonic()
            # Local bookkeeping dates must not become unsupported HTTP parameters.
            query = {key: value for key, value in params.items() if key not in ('fromDate', 'toDate')}
            url = 'https://api.upstox.com' + path
            try:
                content = _http('GET', url, {'Authorization': 'Bearer ' + self._settings.upstox_token, 'Accept': 'application/json'}, params=query)
            except ResearchProviderError as error:
                if error.state == 'TOKEN_EXPIRED': self._runtime_state = error.state
                if error.state == 'SUBSCRIPTION_REQUIRED' and path.startswith('/v2/expired-instruments/'):
                    self._plus_unavailable = error.state
                raise
            cached = self._cache.save(request, content, url)
        self.source_manifest.append({key: cached[key] for key in ('provider', 'request', 'content_checksum', 'retrieved_at', 'source_url')})
        return cached

    def global_metadata(self):
        """Read a supplied official Global Instruments file; never guess USDINR keys/latency."""
        path = Path(self._settings.global_instruments_file) if self._settings.global_instruments_file else self._root / 'config/upstox_global_instruments.json'
        if not path.is_file(): return {}
        raw = path.read_bytes()
        content = json.loads(gzip.decompress(raw) if path.suffix == '.gz' else raw)
        rows = content.get('instruments', content.get('data', [])) if isinstance(content, dict) else content
        if not isinstance(rows, list): raise ResearchProviderError('PROVIDER_ERROR', 'GLOBAL_INSTRUMENT_METADATA_INVALID')
        result = {}
        for name in ('GIFT_NIFTY', 'BRENT', 'USDINR'):
            matches = [row for row in rows if row.get('instrument_key') == self.INSTRUMENTS.get(name)] if name != 'USDINR' else [
                row for row in rows if str(row.get('trading_symbol', row.get('name', ''))).upper().replace('/', '').replace(' ', '') in ('USDINR', 'USDINRSPOT')
                and str(row.get('instrument_key', '')).startswith('GLOBAL_')]
            keys = {row['instrument_key'] for row in matches}
            if len(keys) == 1 and len(matches) == 1:
                row = matches[0]
                result[name] = {'instrument_key': row['instrument_key'],
                    'source_latency_seconds': _number(row.get('source_latency_seconds', row.get('latency_seconds'))),
                    'metadata_checksum': sha256(raw).hexdigest(), 'provider_metadata': row}
            elif matches:
                raise ResearchProviderError('PROVIDER_ERROR', 'AMBIGUOUS_GLOBAL_INSTRUMENT')
        self.source_manifest.append({'source': 'UPSTOX_GLOBAL_INSTRUMENTS_FILE',
            'metadataChecksum': sha256(raw).hexdigest(), 'resolvedInstruments': result})
        return result

    @staticmethod
    def _key(value):
        if not re.fullmatch(r'[A-Za-z0-9_| .-]+', value):
            raise ValueError('Invalid market-data instrument key')
        return quote(value, safe='')

    def historical_candles(self, instrument, day, *, missing_only=True):
        if not isinstance(day, date) or isinstance(day, datetime):
            raise ValueError('UPSTOX_HISTORICAL_CANDLES_REQUIRE_A_DATE')
        if instrument not in (*self.INSTRUMENTS, 'USDINR'): raise ValueError('Unsupported research instrument')
        metadata = self.global_metadata() if instrument in ('GIFT_NIFTY', 'BRENT', 'USDINR') else {}
        item = metadata.get(instrument, {})
        key = self.INSTRUMENTS.get(instrument, item.get('instrument_key'))
        if not key: raise ResearchProviderError('NOT_CONFIGURED', 'USDINR_GLOBAL_METADATA_REQUIRED')
        latency = 0 if instrument in ('BANKNIFTY', 'NIFTY50', 'INDIA_VIX') else item.get('source_latency_seconds')
        path = f'/v3/historical-candle/{self._key(key)}/minutes/1/{day}/{day}'
        raw = self._get(path, {'fromDate': day.isoformat(), 'toDate': day.isoformat()}, missing_only=missing_only)
        # Response latency, when explicitly provided, overrides metadata-file latency.
        returned_latency = raw['content'].get('data', {}).get('source_latency_seconds')
        if returned_latency is not None: latency = _number(returned_latency)
        return self._parse_candles(raw, instrument, day, latency, {'instrumentKey': key, **item})

    def _parse_candles(self, raw, instrument, day, latency, metadata):
        result, seen = [], set()
        for row in raw['content'].get('data', {}).get('candles', []):
            if not isinstance(row, list) or len(row) < 6: raise ResearchProviderError('PROVIDER_ERROR', 'CANDLE_SCHEMA_INVALID')
            at = _stamp(row[0])
            if at.astimezone(IST).date() != day: continue
            if at in seen or at.second or at.microsecond: raise ResearchProviderError('PROVIDER_ERROR', 'DUPLICATE_OR_NON_MINUTE_CANDLE')
            seen.add(at)
            values = {key: _number(value, positive=True) for key, value in zip(('open', 'high', 'low', 'close'), row[1:5])}
            if any(value is None for value in values.values()) or not values['low'] <= min(values['open'], values['close']) <= max(values['open'], values['close']) <= values['high']:
                continue
            values.update(volume=_number(row[5]), oi=_number(row[6]) if len(row) > 6 else None)
            result.append(ResearchContextObservation('UPSTOX_ANALYTICS', instrument, at,
                at + timedelta(seconds=60 + latency) if latency is not None else None, latency, 60,
                values, raw['content_checksum'], {**metadata, 'availabilityBasis': 'CANDLE_CLOSE_PLUS_SOURCE_LATENCY' if latency is not None else 'SOURCE_LATENCY_UNKNOWN'}))
        return sorted(result, key=lambda observation: observation.observed_at)

    def market_information(self, kind, day, *, expiry=None, missing_only=True):
        if kind not in self.MARKET_ENDPOINTS: raise ValueError('Unsupported market-information kind')
        params = {'instrument_key': self.INSTRUMENTS['BANKNIFTY'], 'date': day.isoformat(),
                  'fromDate': day.isoformat(), 'toDate': day.isoformat()}
        if expiry: params['expiry_date'] = expiry.isoformat()
        raw = self._get(self.MARKET_ENDPOINTS[kind], params, missing_only=missing_only)
        data = raw['content'].get('data', [])
        rows = ([data] if data.get('timestamp') else data.get('buckets', data.get('values', data.get('data', [])))) if isinstance(data, dict) else data
        if not isinstance(rows, list): raise ResearchProviderError('PROVIDER_ERROR', 'HISTORICAL_BUCKET_SCHEMA_UNSUPPORTED')
        output = []
        for row in rows:
            if not isinstance(row, dict) or not row.get('timestamp'): continue
            at = _stamp(row['timestamp'])
            if at.astimezone(IST).date() != day: continue  # Reject a current snapshot returned for a historical request.
            seconds = _number(row.get('granularity_seconds'))
            published = row.get('published_at') or row.get('effective_at')
            latency = _number(row.get('source_latency_seconds'))
            effective = _stamp(published) if published else at + timedelta(seconds=seconds + latency) if seconds is not None and latency is not None else None
            output.append(ResearchContextObservation('UPSTOX_MARKET_INFORMATION', kind, at, effective,
                latency, int(seconds) if seconds is not None else None,
                {key: value for key, value in row.items() if key not in ('timestamp', 'published_at', 'effective_at')},
                raw['content_checksum'], {'expiry': expiry.isoformat() if expiry else None,
                    'availabilityBasis': 'PROVIDER_PUBLICATION' if published else 'BUCKET_END_PLUS_LATENCY' if effective else 'PUBLICATION_OR_GRANULARITY_UNKNOWN'}))
        if not output:
            self.source_manifest.append({'source': 'UPSTOX_MARKET_INFORMATION', 'kind': kind,
                'date': str(day), 'status': 'HISTORICAL_BUCKETS_UNAVAILABLE', 'rawChecksum': raw['content_checksum']})
        return output


def _total(rows, key):
    values = [row.get(key) for row in rows]
    return sum(values) if values and all(value is not None for value in values) else None


def _ratio(top, bottom):
    return top / bottom if top is not None and bottom is not None and bottom > 0 else None


def _delta(current, previous):
    return current - previous if current is not None and previous is not None else None


def derive_option_context(observations, width):
    """One snapshot per original provider minute; rolling universe changes invalidate deltas."""
    grouped = defaultdict(list)
    for observation in observations:
        grouped[(observation.provider, observation.observed_at)].append(observation)
    output, previous = [], {}
    for (provider, at), observations_at in sorted(grouped.items()):
        rows = [observation.values for observation in observations_at]
        legs = {(row['side'], row['offset']): row for row in rows}
        if len(legs) != len(rows): continue  # Ambiguous duplicate legs, never last-write-wins.
        expected = {(side, offset) for side in ('CE', 'PE') for offset in range(-width, width + 1)}
        valid = {key: row for key, row in legs.items() if all(row.get(field) is not None for field in ('open', 'high', 'low', 'close', 'oi', 'volume', 'spot', 'strike'))
                 and row['low'] <= min(row['open'], row['close']) <= max(row['open'], row['close']) <= row['high']}
        atm_ce, atm_pe = valid.get(('CE', 0)), valid.get(('PE', 0))
        pair = bool(atm_ce and atm_pe and atm_ce['strike'] == atm_pe['strike'] and atm_ce['expiry'] == atm_pe['expiry'])
        spot = atm_ce['spot'] if pair else None
        aligned = bool(spot and all(abs(row['spot'] - spot) <= max(.05, spot * .00001) for row in valid.values()))
        complete = (set(valid) == expected and aligned
            and all(valid[('CE', offset)]['strike'] == valid[('PE', offset)]['strike'] for offset in range(-width, width + 1))
            and len({(row['side'], row['strike'], row['expiry']) for row in valid.values()}) == len(expected)
            and len({row['expiry'] for row in valid.values()}) == 1
            and all(not row['expiry'] or date.fromisoformat(row['expiry']) >= at.astimezone(IST).date() for row in valid.values())
            and all(valid[('CE', offset)]['strike'] < valid[('CE', offset + 1)]['strike'] for offset in range(-width, width)))
        ce = [row for (side, _), row in valid.items() if side == 'CE']
        pe = [row for (side, _), row in valid.items() if side == 'PE']
        ce_oi, pe_oi = (_total(ce, 'oi'), _total(pe, 'oi')) if complete else (None, None)
        ce_volume, pe_volume = (_total(ce, 'volume'), _total(pe, 'volume')) if complete else (None, None)
        identities = sorted(row['contract_identity'] for row in valid.values())
        prior = previous.get(provider)
        adjacent = bool(prior and prior['at'] + timedelta(minutes=1) == at)
        comparable = adjacent and complete and prior['complete'] and prior['identities'] == identities
        ce_change = _delta(ce_oi, prior['values']['ce_total_oi']) if comparable else None
        pe_change = _delta(pe_oi, prior['values']['pe_total_oi']) if comparable else None
        atm_ids = [atm_ce['contract_identity'], atm_pe['contract_identity']] if pair else []
        atm_comparable = adjacent and pair and prior['atm_ids'] == atm_ids
        straddle = atm_ce['close'] + atm_pe['close'] if pair else None
        ce_iv, pe_iv = (atm_ce.get('iv'), atm_pe.get('iv')) if pair else (None, None)
        ce_wall = max((row for row in ce if spot is not None and row['strike'] > spot), key=lambda row: (row['oi'], -row['strike']), default=None) if complete else None
        pe_wall = max((row for row in pe if spot is not None and row['strike'] < spot), key=lambda row: (row['oi'], row['strike']), default=None) if complete else None
        weights = [row['oi'] for row in ce + pe] if complete else []
        total_oi = sum(weights)
        entropy = -sum((value / total_oi) * math.log(value / total_oi) for value in weights if value > 0) if total_oi > 0 else None
        expiries = {row['expiry'] for row in valid.values() if row.get('expiry')}
        expiry = next(iter(expiries)) if len(expiries) == 1 else None
        expiry_types = {row.get('expiry_type') for row in valid.values()}
        context = 'UNKNOWN'
        if expiry and date.fromisoformat(expiry) > at.astimezone(IST).date(): context = 'NON_EXPIRY'
        elif expiry and date.fromisoformat(expiry) == at.astimezone(IST).date() and len(expiry_types) == 1:
            kind = next(iter(expiry_types))
            if kind in ('MONTHLY', 'QUARTERLY'): context = kind + '_EXPIRY'
        values = {'spot': spot if aligned else None, 'atm_strike': atm_ce['strike'] if pair else None,
            'ce_total_oi': ce_oi, 'pe_total_oi': pe_oi, 'pcr_oi': _ratio(pe_oi, ce_oi),
            'ce_oi_change': ce_change, 'pe_oi_change': pe_change,
            'normalized_oi_imbalance': ((pe_change-ce_change)/(abs(pe_change)+abs(ce_change)) if abs(pe_change)+abs(ce_change) else 0) if ce_change is not None and pe_change is not None else None,
            'ce_volume': ce_volume, 'pe_volume': pe_volume, 'volume_pcr': _ratio(pe_volume, ce_volume),
            'atm_ce_oi': atm_ce['oi'] if pair else None, 'atm_pe_oi': atm_pe['oi'] if pair else None,
            'atm_ce_oi_change': _delta(atm_ce['oi'], prior['values']['atm_ce_oi']) if atm_comparable else None,
            'atm_pe_oi_change': _delta(atm_pe['oi'], prior['values']['atm_pe_oi']) if atm_comparable else None,
            'atm_ce_iv': ce_iv, 'atm_pe_iv': pe_iv, 'atm_iv_mean': (ce_iv+pe_iv)/2 if ce_iv is not None and pe_iv is not None else None,
            'put_call_iv_skew': _delta(pe_iv, ce_iv), 'atm_straddle_premium': straddle,
            'atm_straddle_premium_change': _delta(straddle, prior['values']['atm_straddle_premium']) if atm_comparable else None,
            'strongest_ce_oi_strike_above_spot': ce_wall['strike'] if ce_wall else None,
            'strongest_pe_oi_strike_below_spot': pe_wall['strike'] if pe_wall else None,
            'distance_to_ce_wall': ce_wall['strike']-spot if ce_wall else None,
            'distance_to_pe_wall': spot-pe_wall['strike'] if pe_wall else None,
            'ce_oi_concentration': _ratio(max((row['oi'] for row in ce), default=None), ce_oi),
            'pe_oi_concentration': _ratio(max((row['oi'] for row in pe), default=None), pe_oi),
            'oi_concentration': _ratio(max(weights, default=None), total_oi), 'oi_entropy': entropy,
            'selected_strike_completeness': len(set(valid) & expected) / len(expected),
            'complete': bool(complete), 'expiry': expiry, 'expiry_context': context,
            'strikes': sorted({row['strike'] for row in valid.values()}),
            'scope': 'SELECTED_ATM_RELATIVE_STRIKES_NOT_ENTIRE_CHAIN'}
        effective = max(observation.effective_at for observation in observations_at) if all(observation.effective_at for observation in observations_at) else None
        checksums = sorted({observation.raw_checksum for observation in observations_at})
        iv_proofs=[observation.metadata.get('ivProvenance') for observation in observations_at]
        iv_proof=iv_proofs[0] if iv_proofs and all(p and p==iv_proofs[0] for p in iv_proofs) else None
        output.append(ResearchContextObservation(provider, 'OPTIONS', at, effective, 0, 60, values, _hash(checksums),
            {'rawChecksums': checksums, 'status': 'PASS' if complete else 'PARTIAL',
             'ivProvenance':iv_proof,
             'previousMinuteUsed': prior['at'].isoformat() if comparable or atm_comparable else None,
             'availabilityBasis': 'COMPLETED_HISTORICAL_OPTION_CANDLES'}))
        previous[provider] = {'at': at, 'complete': complete, 'identities': identities, 'atm_ids': atm_ids, 'values': values}
    return output


class _ObservationIndex:
    def __init__(self, observations):
        self.rows = sorted((row for row in observations if row.effective_at is not None), key=lambda row: (row.effective_at, row.observed_at))
        self.times = [row.effective_at for row in self.rows]

    def at_or_before(self, at, max_staleness):
        index = bisect_right(self.times, at) - 1
        if index < 0: return None
        row = self.rows[index]
        if row.observed_at > at or (at-row.observed_at).total_seconds() > max_staleness: return None
        # Duplicate timestamp facts with different values are ambiguous, not last-write-wins.
        adjacent = self.rows[index-1] if index else None
        if adjacent and adjacent.effective_at == row.effective_at and _hash(adjacent.values) != _hash(row.values): return None
        return row


def _context(row, values=None):
    if row is None: return None
    return {'provider': row.provider, 'observed_at': row.observed_at.isoformat(),
            'effective_at': row.effective_at.isoformat() if row.effective_at else None,
            'source_latency': row.source_latency, 'granularity_seconds': row.granularity_seconds,
            'raw_checksum': row.raw_checksum, 'metadata': row.metadata, 'values': values if values is not None else row.values}


def cross_market_context(index, as_of, max_staleness):
    row = index.at_or_before(as_of, max_staleness)
    if row is None: return None
    values = {'level': row.values['close']}
    watermarks = {}
    for minutes in (1, 5, 15, 30):
        prior = index.at_or_before(as_of - timedelta(minutes=minutes), max_staleness)
        # No interpolation. The timestamps below explicitly disclose each sampled interval.
        exact_window = prior is not None and row.observed_at - prior.observed_at == timedelta(minutes=minutes)
        values[f'return_{minutes}m'] = row.values['close']/prior.values['close']-1 if exact_window else None
        values[f'change_{minutes}m'] = row.values['close']-prior.values['close'] if exact_window else None
        watermarks[str(minutes)] = {'observed_at': prior.observed_at.isoformat(), 'effective_at': prior.effective_at.isoformat(),
            'first_seen_at':prior.metadata.get('first_seen_at'), 'raw_checksum': prior.raw_checksum} if prior else None
    return {**_context(row, values), 'returnSourceWatermarks': watermarks}


def choose_options(candidates):
    complete = [row for row in candidates if row.values.get('complete')]
    ordered = sorted(complete or candidates, key=lambda row: SOURCE_PRIORITY.index(row.provider)
        if row.provider in SOURCE_PRIORITY else len(SOURCE_PRIORITY))
    if not ordered: return None, []
    selected, conflicts = ordered[0], []
    for other in candidates:
        if other is selected: continue
        if other.observed_at != selected.observed_at: continue
        comparable = selected.values.get('expiry') and selected.values['expiry'] == other.values.get('expiry') and selected.values['strikes'] == other.values.get('strikes')
        for key in ('spot', 'pcr_oi', 'ce_total_oi', 'pe_total_oi', 'atm_straddle_premium') if comparable else ('spot',):
            left, right = selected.values.get(key), other.values.get(key)
            if left is not None and right is not None and abs(left-right) > max(abs(left), abs(right), 1) * CONFLICT_RELATIVE_TOLERANCE:
                conflicts.append({'field': key, 'selected': selected.provider, 'other': other.provider,
                                  'selectedValue': left, 'otherValue': right, 'observed_at': selected.observed_at.isoformat()})
    return _context(selected), conflicts

class UpstoxAnalyticsResearchProvider(_UpstoxAnalyticsBase):
    def option_chain(self, expiry, *, missing_only=True):
        # Current snapshots are raw research evidence only, never backdated into historical minutes.
        return self._get('/v2/option/chain', {'instrument_key': self.INSTRUMENTS['BANKNIFTY'],
            'expiry_date': expiry.isoformat(), 'date': datetime.now(IST).date().isoformat()}, missing_only=missing_only)

    def historical_expiries(self, day, *, missing_only=True):
        if self.plus_state() != 'READY_FOR_REQUEST': raise ResearchProviderError(self.plus_state(), 'UPSTOX_PLUS_REQUIRED')
        raw = self._get('/v2/expired-instruments/expiries', {'instrument_key': self.INSTRUMENTS['BANKNIFTY'],
            'date': day.isoformat()}, missing_only=missing_only)
        data = raw['content'].get('data', [])
        if not isinstance(data, list): raise ResearchProviderError('PROVIDER_ERROR', 'EXPIRY_METADATA_INVALID')
        return sorted({date.fromisoformat(value) for value in data if isinstance(value, str)})

    def expired_option_contracts(self, expiry, *, missing_only=True):
        if self.plus_state() != 'READY_FOR_REQUEST': raise ResearchProviderError(self.plus_state(), 'UPSTOX_PLUS_REQUIRED')
        raw = self._get('/v2/expired-instruments/option/contract',
            {'instrument_key': self.INSTRUMENTS['BANKNIFTY'], 'expiry_date': expiry.isoformat()}, missing_only=missing_only)
        result = []
        for row in raw['content'].get('data', []):
            if row.get('weekly') is True: continue  # BANKNIFTY 2026 has no weekly contracts.
            if row.get('underlying_key', self.INSTRUMENTS['BANKNIFTY']) != self.INSTRUMENTS['BANKNIFTY']: continue
            if str(row.get('expiry', ''))[:10] != str(expiry) or row.get('instrument_type') not in ('CE', 'PE'): continue
            if _number(row.get('strike_price'), positive=True) is None: continue
            self._key(row['instrument_key'])
            result.append({**row, 'metadata_checksum': raw['content_checksum']})
        return result

    def expired_option_history(self, day, spots, *, missing_only=True):
        expiries = [expiry for expiry in self.historical_expiries(day, missing_only=missing_only) if expiry >= day]
        if not expiries or not spots: return []
        expiry = expiries[0]
        contracts = self.expired_option_contracts(expiry, missing_only=missing_only)
        strikes = sorted({float(row['strike_price']) for row in contracts})
        if not strikes: return []
        width = self._settings.option_width
        windows = {}
        for at, spot in spots.items():
            center = min(range(len(strikes)), key=lambda index: (abs(strikes[index] - spot), strikes[index]))
            windows[at] = {strikes[index]: index - center for index in range(max(0, center - width), min(len(strikes), center + width + 1))}
        required = {strike for window in windows.values() for strike in window}
        selected = [row for row in contracts if float(row['strike_price']) in required]
        if len({(row['instrument_type'], row['strike_price']) for row in selected}) != len(selected):
            raise ResearchProviderError('PROVIDER_ERROR', 'AMBIGUOUS_EXPIRED_CONTRACT_IDENTITY')
        output = []
        for contract in selected:
            key = self._key(contract['instrument_key'])
            path = f'/v2/expired-instruments/historical-candle/{key}/1minute/{day}/{day}'
            raw = self._get(path, {'fromDate': str(day), 'toDate': str(day)}, missing_only=missing_only)
            for observation in self._parse_candles(raw, 'BANKNIFTY_OPTIONS', day, 0, {'contract': contract}):
                at = observation.observed_at
                strike = float(contract['strike_price'])
                if at not in windows or strike not in windows[at]: continue
                expiry_type = str(contract.get('expiry_type', '')).upper()
                values = {**observation.values, 'side': contract['instrument_type'], 'offset': windows[at][strike],
                    'strike': strike, 'spot': spots[at], 'iv': None, 'expiry': str(expiry),
                    'expiry_type': expiry_type if expiry_type in ('MONTHLY', 'QUARTERLY') else 'UNKNOWN',
                    'contract_identity': contract['instrument_key']}
                output.append(ResearchContextObservation('UPSTOX_EXPIRED_OPTIONS', 'BANKNIFTY_OPTIONS', at,
                    observation.effective_at, observation.source_latency, 60, values, observation.raw_checksum,
                    {'contractMetadataChecksum': contract['metadata_checksum'], 'availabilityBasis': 'ONE_MINUTE_CANDLE_CLOSE'}))
        return output


class ResearchContextRepository:
    """Append-only derived evidence. Raw option legs never become database rows."""
    def __init__(self, database_url=None):
        self.url = database_url if database_url is not None else os.getenv('DATABASE_URL', '')

    @contextmanager
    def connection(self, *, write=False):
        if not self.url: raise ValueError('RESEARCH_CONTEXT_DATABASE_NOT_CONFIGURED')
        import psycopg
        from psycopg.rows import dict_row
        with psycopg.connect(self.url, row_factory=dict_row, connect_timeout=3, options='-c statement_timeout=15000') as db:
            if not write: db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            yield db

    def session(self, day):
        with self.connection() as db:
            return db.execute('SELECT session_date,market_open_utc,market_close_utc,status,source FROM trading_sessions WHERE session_date=%s', (day,)).fetchone()

    def save(self, run, rows):
        from psycopg.types.json import Jsonb
        from psycopg import sql
        with self.connection(write=True) as db:
            policy = run['configuration']['stalenessPolicyVersion']
            db.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('research-context-policy:' + policy,))
            prior = db.execute("SELECT configuration FROM research_context_runs WHERE configuration->>'stalenessPolicyVersion'=%s LIMIT 1", (policy,)).fetchone()
            if prior and prior['configuration']['stalenessSeconds'] != run['configuration']['stalenessSeconds']:
                raise ValueError('Changed staleness limits require a new policy version')
            for table, values, key in [('research_context_runs', [run], 'run_id'),
                                       ('research_context_minutes', rows, 'context_checksum')]:
                for value in values:
                    columns = list(value)
                    query = sql.SQL('INSERT INTO {} ({}) VALUES ({}) ON CONFLICT DO NOTHING').format(
                        sql.Identifier(table), sql.SQL(',').join(map(sql.Identifier, columns)),
                        sql.SQL(',').join(sql.Placeholder() for _ in columns))
                    db.execute(query, [Jsonb(_plain(value[column])) if isinstance(value[column], (dict, list)) else value[column] for column in columns])
                    check = sql.SQL('SELECT * FROM {} WHERE {}=%s').format(sql.Identifier(table), sql.Identifier(key))
                    stored = db.execute(check, (value[key],)).fetchone()
                    if not stored or _hash({column: stored[column] for column in columns}) != _hash(value):
                        raise ValueError('IMMUTABLE_RESEARCH_CONTEXT_CONFLICT')

    def day(self, day, version=RESEARCH_CONTEXT_VERSION):
        with self.connection() as db:
            return db.execute('''SELECT * FROM research_context_minutes WHERE session_date=%s AND context_version=%s
                ORDER BY as_of,run_id''', (day, version)).fetchall()

    def near(self, as_of, version, max_staleness, run_id=None):
        with self.connection() as db:
            return db.execute('''SELECT * FROM research_context_minutes WHERE context_version=%s AND as_of<=%s
                AND as_of>=%s AND (%s::text IS NULL OR run_id=%s) ORDER BY as_of DESC,run_id''',
                (version, as_of, as_of-timedelta(seconds=max_staleness), run_id, run_id)).fetchall()

    def last_runs(self):
        with self.connection() as db:
            return db.execute('''SELECT DISTINCT ON(provider) provider,status,source_manifest,created_at
                FROM research_context_runs WHERE context_version=%s ORDER BY provider,created_at DESC,run_id DESC''',
                (RESEARCH_CONTEXT_VERSION,)).fetchall()


def canonical_dates(root):
    manifest = json.loads((Path(root) / 'manifest.json').read_text(encoding='utf-8'))
    return sorted({date.fromisoformat(item['date']) for item in manifest['files']})


def _index_bounds(session, day):
    if not session or session['status'] != 'OPEN':
        # A special F&O session is not automatically a verified cash-index session.
        return None
    opened = max(session['market_open_utc'], datetime.combine(day, time(9, 15), IST).astimezone(UTC))
    closed = min(session['market_close_utc'], datetime.combine(day, time(15, 30), IST).astimezone(UTC))
    return (opened, closed) if opened < closed else None


def _usable(context, as_of, staleness):
    if not context or not context.get('effective_at'): return False
    observed, effective = _stamp(context['observed_at']), _stamp(context['effective_at'])
    return observed <= effective <= as_of and (as_of-observed).total_seconds() <= staleness


def _contexts(row):
    yield 'nifty50', row['index_context'].get('NIFTY50')
    yield 'options', row['options_context'].get('minute')
    yield 'pcr', row['options_context'].get('marketInformation', {}).get('PCR')
    yield 'oi', row['options_context'].get('marketInformation', {}).get('OI') or row['options_context'].get('marketInformation', {}).get('CHANGE_OI')
    yield 'maxpain', row['options_context'].get('marketInformation', {}).get('MAX_PAIN')
    yield 'vix', row['volatility_context'].get('INDIA_VIX')
    for name, key in [('gift', 'GIFT_NIFTY'), ('brent', 'BRENT'), ('usdinr', 'USDINR')]:
        yield name, row['global_context'].get(key)


def context_coverage(repository=None, root=None):
    repository = repository or ResearchContextRepository()
    dates = canonical_dates(root or memory_root())
    present = {name: set() for name in ('options', 'vix', 'gift', 'brent', 'usdinr', 'pcr', 'oi', 'maxpain', 'nifty50')}
    rows_seen, conflicts, signatures = set(), set(), {}
    try:
        with repository.connection() as db:
            # A server-side cursor keeps the bounded per-minute JSON stream off the heap.
            with db.cursor(name='research_context_coverage') as cursor:
                cursor.execute('''SELECT * FROM research_context_minutes WHERE session_date=ANY(%s)
                    AND context_version=%s ORDER BY session_date,as_of''', (dates, RESEARCH_CONTEXT_VERSION))
                for row in cursor:
                    identity = (row['session_date'], row['as_of'])
                    rows_seen.add(identity)
                    signature = _hash({key: row[key] for key in ('index_context', 'options_context', 'volatility_context', 'global_context', 'source_lineage')})
                    if identity in signatures and signatures[identity] != signature:
                        conflicts.add(identity)
                    signatures[identity] = signature
                    if row['quality_status'] == 'SOURCE_CONFLICT':
                        conflicts.add(identity)
                        continue
                    for name, context in _contexts(row):
                        if context and context.get('effective_at') and _stamp(context['effective_at']) <= row['as_of']:
                            if name == 'options' and not context['values'].get('complete'): continue
                            if name == 'pcr' and not any(_number(context['values'].get(key)) is not None for key in ('pcr', 'pcr_oi', 'value', 'put_call_ratio')): continue
                            if name == 'maxpain' and not any(_number(context['values'].get(key), positive=True) is not None for key in ('max_pain', 'max_pain_strike', 'value')): continue
                            if name == 'oi' and not any(context['values'].get(key) is not None for key in ('oi', 'total_oi', 'call_oi', 'put_oi', 'ce_oi', 'pe_oi', 'change_oi', 'strikes', 'value')): continue
                            policy_key = {'options': 'OPTIONS', 'pcr': 'MARKET_INFORMATION', 'oi': 'MARKET_INFORMATION', 'maxpain': 'MARKET_INFORMATION', 'vix': 'INDIA_VIX', 'gift': 'GIFT_NIFTY', 'brent': 'BRENT', 'usdinr': 'USDINR', 'nifty50':'BANKNIFTY'}[name]
                            if _usable(context, row['as_of'], row['source_lineage']['stalenessSeconds'][policy_key]):
                                present[name].add(identity)
        present = {name: {identity[0] for identity in values - conflicts} for name, values in present.items()}
        status = 'READY' if dates and all(len(values) == len(dates) for values in present.values()) and not conflicts else 'PARTIAL'
    except Exception:
        return asdict(ResearchContextCoverage(len(dates), 0, 0, 0, 0, 0, 0, 0, 0,
            {name: [str(day) for day in dates] for name in present}, 'NOT_CONFIGURED' if not repository.url else 'UNAVAILABLE'))
    return asdict(ResearchContextCoverage(len(dates), *(len(present[name]) for name in ('options', 'vix', 'gift', 'brent', 'usdinr', 'pcr')),
        len(rows_seen), len(conflicts), {name: [str(day) for day in dates if day not in values] for name, values in present.items()}, status,
        len(present['oi']), len(present['maxpain']), len(present['nifty50'])))


class HistoricalResearchContextReader:
    def __init__(self, repository=None, settings=None, root=None):
        self.repository = repository or ResearchContextRepository()
        self.settings = settings or ResearchContextSettings.from_env()
        self.root = Path(root or memory_root())

    def at_or_before(self, as_of, max_staleness=None, *, run_id=None, horizon_minutes=None):
        as_of = _stamp(as_of)
        if horizon_minutes is not None and horizon_minutes not in (10, 20, 30): raise ValueError('Unsupported index target horizon')
        bounds = _index_bounds(self.repository.session(as_of.astimezone(IST).date()), as_of.astimezone(IST).date())
        if not bounds or not bounds[0] <= as_of < bounds[1] or (horizon_minutes and as_of + timedelta(minutes=horizon_minutes) >= bounds[1]):
            return {'status': 'OUTSIDE_INDEX_PREDICTION_SESSION', 'context': None}
        limit = int(max_staleness) if max_staleness is not None else 60
        if not 0 <= limit <= 86400: raise ValueError('Invalid context staleness limit')
        rows = self.repository.near(as_of, RESEARCH_CONTEXT_VERSION, limit, run_id)
        if not rows: return {'status': 'CONTEXT_UNAVAILABLE', 'context': None}
        latest = rows[0]['as_of']
        candidates = [row for row in rows if row['as_of'] == latest]
        # Pin run_id when multiple immutable derivations differ; never silently switch a dataset's lineage.
        identities = {_hash({key: row[key] for key in ('index_context', 'options_context', 'volatility_context', 'global_context', 'source_lineage')}) for row in candidates}
        if len(identities) != 1 or any(row['quality_status'] == 'SOURCE_CONFLICT' for row in candidates):
            return {'status': 'SOURCE_CONFLICT', 'context': None, 'runIds': [row['run_id'] for row in candidates]}
        row = candidates[0]
        if _hash({key: value for key, value in row.items() if key not in ('context_checksum', 'created_at')}) != row['context_checksum']:
            return {'status': 'CONTEXT_CHECKSUM_INVALID', 'context': None}
        stored_policy = row['source_lineage']['stalenessSeconds']
        groups = {}
        for group in ('index_context', 'volatility_context', 'global_context'):
            groups[group] = {name: value if _usable(value, as_of, min(self.settings.staleness_seconds.get(name,self.settings.staleness_seconds['BANKNIFTY']), stored_policy.get(name,stored_policy['BANKNIFTY']))) else None
                             for name, value in row[group].items()}
        options = row['options_context']
        groups['options_context'] = {'minute': options.get('minute') if _usable(options.get('minute'), as_of, min(self.settings.staleness_seconds['OPTIONS'], stored_policy['OPTIONS'])) else None,
            'marketInformation': {name: value if _usable(value, as_of, min(self.settings.staleness_seconds['MARKET_INFORMATION'], stored_policy['MARKET_INFORMATION'])) else None
                                  for name, value in options.get('marketInformation', {}).items()}}
        # A context row can be fresh while one of its lower-frequency sources has expired.
        any_available = any(value for group in ('index_context', 'volatility_context', 'global_context') for value in groups[group].values()) or groups['options_context']['minute'] or any(groups['options_context']['marketInformation'].values())
        if not any_available: return {'status': 'CONTEXT_STALE', 'context': None}
        pruned = any(_hash(groups[group]) != _hash(row[group]) for group in groups)
        projection = {key: value for key, value in {**row, **groups}.items() if key not in ('context_checksum', 'created_at')}
        return {'status': 'PARTIAL' if pruned else row['quality_status'], 'context': _plain(projection),
                'storedContextChecksum': row['context_checksum'], 'projectionChecksum': _hash(projection),
                'marketAsOf': as_of.isoformat(), 'contextAsOf': latest.isoformat(), 'requiresPinnedRun': True}

    def day(self, day):
        day = day if isinstance(day, date) else date.fromisoformat(day)
        return {'date': str(day), 'contextVersion': RESEARCH_CONTEXT_VERSION,
                'rows': _plain(self.repository.day(day)), 'containsRawOptionLegs': False}


class ResearchContextEnrichmentService:
    def __init__(self, repository=None, root=None, settings=None):
        self.repository = repository or ResearchContextRepository()
        self.root = Path(root or memory_root())
        self.settings = settings or ResearchContextSettings.from_env()

    def derive_day(self, day, bounds, series, option_rows, information, required_blocks=None):
        limits = {**self.settings.staleness_seconds,'NIFTY50':self.settings.staleness_seconds['BANKNIFTY']}
        indexes = {name: _ObservationIndex(rows) for name, rows in series.items()}
        option_indexes = {provider: _ObservationIndex([row for row in option_rows if row.provider == provider])
            for provider in (*SOURCE_PRIORITY[:2], LOCAL_IMPORT_PROVIDER)}
        info_indexes = {kind: _ObservationIndex(rows) for kind, rows in information.items()}
        result = []
        at = bounds[0]
        while at < bounds[1]:
            candidates = [row for index in option_indexes.values() if (row := index.at_or_before(at, limits['OPTIONS'])) is not None]
            option, conflicts = choose_options(candidates)
            values = {name: cross_market_context(index, at, limits[name]) for name, index in indexes.items()}
            buckets = {kind: _context(index.at_or_before(at, limits['MARKET_INFORMATION'])) for kind, index in info_indexes.items()}
            if not option and not any(values.values()) and not any(buckets.values()):
                at += timedelta(minutes=1)
                continue
            if option and values.get('BANKNIFTY') and option['observed_at'] == values['BANKNIFTY']['observed_at']:
                spot, index_price = option['values'].get('spot'), values['BANKNIFTY']['values']['level']
                if spot is not None and abs(spot-index_price) / index_price > CONFLICT_RELATIVE_TOLERANCE:
                    conflicts.append({'field': 'spot', 'selected': option['provider'], 'other': 'UPSTOX_ANALYTICS', 'selectedValue': spot, 'otherValue': index_price})
            complete = bool(option and option['values'].get('complete') and all(values.get(name) for name in ('NIFTY50','INDIA_VIX', 'GIFT_NIFTY', 'BRENT', 'USDINR')))
            if required_blocks is not None:
                required=set(required_blocks)
                complete=bool(
                    ('DERIVATIVES' not in required or option and option['values'].get('complete'))
                    and ('INDIA_MARKET' not in required or all(values.get(name) for name in ('NIFTY50','INDIA_VIX')))
                    and ('GLOBAL' not in required or all(values.get(name) for name in ('GIFT_NIFTY','BRENT','USDINR'))))
            lineage = {'contextVersion': RESEARCH_CONTEXT_VERSION, 'sourcePriority': list(SOURCE_PRIORITY),
                'selectedOptionMinuteSource': option['provider'] if option else None,
                'aggregatePcrSource': option['provider'] if option and option['values'].get('pcr_oi') is not None else
                    'UPSTOX_MARKET_INFORMATION' if buckets.get('PCR') else None,
                'conflictRelativeTolerance': CONFLICT_RELATIVE_TOLERANCE, 'conflicts': conflicts,
                'stalenessPolicyVersion': self.settings.staleness_version, 'stalenessSeconds': limits,
                'indexSession': {'open': bounds[0].isoformat(), 'close': bounds[1].isoformat(),
                    'source': 'EXPLICIT_OPEN_CALENDAR_CAPPED_TO_CASH_INDEX_SESSION'},
                'availabilityBasis': 'RETROSPECTIVE_PROVIDER_TIMESTAMP_NOT_PROSPECTIVE_RECEIPT',
                'unavailableSources': [name for name in ('NIFTY50','INDIA_VIX', 'GIFT_NIFTY', 'BRENT', 'USDINR') if not values.get(name)]}
            result.append({'session_date': day, 'as_of': at, 'context_version': RESEARCH_CONTEXT_VERSION,
                'index_context': {'BANKNIFTY': values.get('BANKNIFTY'), 'NIFTY50':values.get('NIFTY50')}, 'options_context': {'minute': option, 'marketInformation': buckets},
                'volatility_context': {'INDIA_VIX': values.get('INDIA_VIX')},
                'global_context': {name: values.get(name) for name in ('GIFT_NIFTY', 'BRENT', 'USDINR')},
                'source_lineage': lineage, 'quality_status': 'SOURCE_CONFLICT' if conflicts else 'PASS' if complete else 'PARTIAL'})
            at += timedelta(minutes=1)
        return result

    def enrich(self, from_date, to_date, provider='auto', missing_only=True, required_blocks=None):
        required=set(required_blocks) if required_blocks is not None else None
        needs_options=required is None or 'DERIVATIVES' in required
        series_names=['BANKNIFTY']
        if required is None or 'INDIA_MARKET' in required: series_names+=['NIFTY50','INDIA_VIX']
        if required is None or 'GLOBAL' in required: series_names+=['GIFT_NIFTY','BRENT','USDINR']
        provider = provider or self.settings.provider
        if provider not in ('auto', 'dhan', 'upstox'): raise ValueError('Unknown research-context provider')
        canonical = canonical_dates(self.root)
        start = date.fromisoformat(from_date) if isinstance(from_date, str) else from_date or min(canonical)
        end = date.fromisoformat(to_date) if isinstance(to_date, str) else to_date or max(canonical)
        if end < start or end >= datetime.now(IST).date(): raise ValueError('Use a completed historical date range')
        dates = [day for day in canonical if start <= day <= end]
        if not dates: raise ValueError('No canonical Market Memory dates in the requested range')
        # Establish writable-storage prerequisites before spending any provider requests.
        with self.repository.connection() as db:
            db.execute('SELECT run_id FROM research_context_runs LIMIT 0')
            db.execute('SELECT context_checksum FROM research_context_minutes LIMIT 0')
        dhan = DhanHistoricalOptionProvider(self.settings, self.root)
        upstox = UpstoxAnalyticsResearchProvider(self.settings, self.root)
        results = []
        for day in dates:
            bounds = _index_bounds(self.repository.session(day), day)
            if not bounds:
                results.append({'date': str(day), 'status': 'INDEX_SESSION_NOT_VERIFIED', 'rows': 0})
                continue
            series, legs, information = {}, [], {}
            states = {'dhan': {'state': dhan.configuration_state()}, 'upstox': {'state': upstox.configuration_state()},
                      'upstoxPlus': {'state': upstox.plus_state()}}
            failures = []
            dhan.source_manifest, upstox.source_manifest = [], []

            def retrieve(name, operation):
                try:
                    value = operation()
                    return value
                except ResearchProviderError as error:
                    failures.append({'source': name, 'state': error.state, 'reason': error.reason})
                    group = 'dhan' if name == 'DHAN_ROLLING_OPTIONS' else 'upstoxPlus' if name == 'UPSTOX_EXPIRED_OPTIONS' else 'upstox'
                    states[group] = {'state': error.state, 'reason': error.reason}
                    return []
                except Exception:
                    failures.append({'source': name, 'state': 'PROVIDER_ERROR', 'reason': 'SOURCE_SCHEMA_OR_CACHE_INVALID'})
                    return []

            if needs_options and provider in ('auto', 'dhan') and self.settings.dhan_enabled:
                legs += retrieve('DHAN_ROLLING_OPTIONS', lambda: dhan.option_history(day, missing_only=missing_only))
            if provider in ('auto', 'upstox') and self.settings.upstox_enabled:
                for name in series_names:
                    series[name] = retrieve(name, lambda name=name: upstox.historical_candles(name, day, missing_only=missing_only))
                if needs_options and self.settings.upstox_plus_enabled:
                    # Actual same-minute historical spot only, never today's ATM or tomorrow's price.
                    spots = {row.observed_at: row.values['close'] for row in series.get('BANKNIFTY', [])}
                    legs += retrieve('UPSTOX_EXPIRED_OPTIONS', lambda: upstox.expired_option_history(day, spots, missing_only=missing_only))
                known_expiries = sorted({date.fromisoformat(row.values['expiry']) for row in legs if row.values.get('expiry') and date.fromisoformat(row.values['expiry']) >= day})
                expiry = known_expiries[0] if known_expiries else None
                for kind in (UpstoxAnalyticsResearchProvider.MARKET_ENDPOINTS if required is None else ()):
                    information[kind] = retrieve(kind, lambda kind=kind: upstox.market_information(kind, day, expiry=expiry, missing_only=missing_only))
            option_rows = derive_option_context(legs, self.settings.option_width)
            rows = self.derive_day(day, bounds, series, option_rows, information,required_blocks=required)
            source_manifest = {'providers': states, 'failures': failures,
                'derivedChecksum': _hash(rows),
                'sources': sorted(dhan.source_manifest + upstox.source_manifest, key=_hash),
                'availabilityWarnings': sorted({row.instrument + ':' + row.metadata.get('availabilityBasis', 'UNKNOWN')
                    for values in list(series.values()) + list(information.values()) for row in values if row.effective_at is None})}
            run = {'provider': provider, 'date_from': day, 'date_to': day, 'context_version': RESEARCH_CONTEXT_VERSION,
                'configuration': {**self.settings.public_configuration(), 'sourceConflictRelativeTolerance': CONFLICT_RELATIVE_TOLERANCE,
                    **({'requiredBlocks':sorted(required & {'INDIA_MARKET','GLOBAL','DERIVATIVES'})} if required is not None else {})},
                'source_manifest': source_manifest, 'status': 'SOURCE_CONFLICT' if any(row['quality_status'] == 'SOURCE_CONFLICT' for row in rows) else
                    'COMPLETE' if rows and all(row['quality_status'] == 'PASS' for row in rows) and not failures else 'PARTIAL' if rows else 'NO_CONTEXT',
                'raw_checksum': _hash(source_manifest['sources'])}
            run['run_id'] = _hash(run)
            for row in rows:
                row['run_id'] = run['run_id']
                row['context_checksum'] = _hash(row)
            self.repository.save(run, rows)
            results.append({'date': str(day), 'runId': run['run_id'], 'status': run['status'], 'rows': len(rows), 'providers': states, 'failures': failures})
        return {'contextVersion': RESEARCH_CONTEXT_VERSION, 'sessions': results,
                'coverage': context_coverage(self.repository, self.root),
                'modelFeatureSetModified': False, 'liveProvider': 'ZERODHA'}


LOCAL_IMPORT_PROVIDER = 'LOCAL_IMPORT'
LOCAL_SERIES = ('BANKNIFTY', 'NIFTY50', 'INDIA_VIX', 'GIFT_NIFTY', 'BRENT', 'USDINR')
LOCAL_OPTION_PROVIDERS = ('DHAN_ROLLING_OPTIONS', 'UPSTOX_EXPIRED_OPTIONS')
_LOCAL_NUMERIC = {
    'open', 'high', 'low', 'close', 'volume', 'oi', 'spot', 'strike', 'offset', 'iv',
    'total_oi', 'call_oi', 'put_oi', 'ce_oi', 'pe_oi', 'pcr', 'pcr_oi', 'value',
    'put_call_ratio', 'max_pain', 'max_pain_strike', 'change_oi',
}
_LOCAL_CONTROL = {
    'provider', 'instrument', 'observed_at', 'effective_at', 'source_latency',
    'source_latency_seconds', 'granularity_seconds', 'raw_checksum', 'values',
    'values_json', 'metadata', 'metadata_json',
}


def _local_file_records(path):
    raw = path.read_bytes()
    text = raw.decode('utf-8-sig')
    suffix = path.suffix.lower()
    if suffix == '.json':
        value = json.loads(text)
        records = value.get('observations') if isinstance(value, dict) else value
        if not isinstance(records, list):
            raise ValueError('LOCAL_IMPORT_JSON_REQUIRES_OBSERVATION_LIST')
        return records, sha256(raw).hexdigest(), 'JSON'
    if suffix in ('.jsonl', '.ndjson'):
        records = [json.loads(line) for line in text.splitlines() if line.strip()]
        return records, sha256(raw).hexdigest(), 'JSONL'
    if suffix == '.csv':
        records = list(csv.DictReader(StringIO(text)))
        return records, sha256(raw).hexdigest(), 'CSV'
    raise ValueError('LOCAL_IMPORT_UNSUPPORTED_FORMAT')


def _local_values(record):
    values = record.get('values')
    if values is None and record.get('values_json'):
        values = json.loads(record['values_json'])
    if values is None:
        values = {}
    if not isinstance(values, dict):
        raise ValueError('LOCAL_IMPORT_VALUES_REQUIRED')
    for key, value in record.items():
        if isinstance(key, str) and key not in _LOCAL_CONTROL and not key.startswith('_'):
            values[key] = value
    cleaned = {}
    for key, value in values.items():
        if isinstance(value, str):
            value = value.strip()
            if not value:
                value = None
        if value is not None and key in _LOCAL_NUMERIC:
            value = _number(value)
            if value is None:
                raise ValueError('LOCAL_IMPORT_INVALID_NUMBER')
        cleaned[str(key)] = value
    if not cleaned:
        raise ValueError('LOCAL_IMPORT_VALUES_REQUIRED')
    return cleaned


def _local_metadata(record):
    value = record.get('metadata')
    if value is None and record.get('metadata_json'):
        value = json.loads(record['metadata_json'])
    value = value or {}
    if not isinstance(value, dict):
        raise ValueError('LOCAL_IMPORT_METADATA_INVALID')
    return value


def _local_observations(records, *, source_observed_at, source_name=LOCAL_IMPORT_PROVIDER):
    observations, seen = [], {}
    for record in records:
        if not isinstance(record, dict):
            raise ValueError('LOCAL_IMPORT_RECORD_INVALID')
        provider, instrument = record.get('provider'), record.get('instrument')
        if not isinstance(instrument, str) or not instrument.strip():
            raise ValueError('LOCAL_IMPORT_INSTRUMENT_REQUIRED')
        provider = provider.strip() if isinstance(provider, str) and provider.strip() else LOCAL_IMPORT_PROVIDER
        if instrument == 'BANKNIFTY_OPTIONS' and provider not in (*LOCAL_OPTION_PROVIDERS, LOCAL_IMPORT_PROVIDER):
            raise ValueError('LOCAL_IMPORT_OPTION_PROVIDER_UNSUPPORTED')
        if instrument not in (*LOCAL_SERIES, 'BANKNIFTY_OPTIONS', *UpstoxAnalyticsResearchProvider.MARKET_ENDPOINTS):
            raise ValueError('LOCAL_IMPORT_INSTRUMENT_UNSUPPORTED')
        if instrument in LOCAL_SERIES:
            values = _local_values(record)
            if values.get('close') is None or values['close'] <= 0:
                raise ValueError('LOCAL_IMPORT_SERIES_CLOSE_REQUIRED')
        else:
            values = _local_values(record)
        observed = _stamp(record.get('observed_at'))
        latency = record.get('source_latency_seconds', record.get('source_latency'))
        latency = _number(latency) if latency is not None else None
        if latency is not None and latency < 0:
            raise ValueError('LOCAL_IMPORT_LATENCY_INVALID')
        granularity = record.get('granularity_seconds')
        granularity = int(granularity) if granularity not in (None, '') else 60
        if granularity <= 0:
            raise ValueError('LOCAL_IMPORT_GRANULARITY_INVALID')
        effective = _stamp(record['effective_at']) if record.get('effective_at') else None
        if effective is not None and effective < observed:
            raise ValueError('LOCAL_IMPORT_EFFECTIVE_BEFORE_OBSERVED')
        if effective is None and latency is not None:
            effective = observed + timedelta(seconds=granularity + latency)
        metadata = _local_metadata(record)
        metadata.setdefault('sourceObservedAt', source_observed_at.isoformat())
        metadata.setdefault('localSourceName', source_name.strip())
        metadata.setdefault('availabilityBasis', 'CALLER_SUPPLIED_EFFECTIVE_AT' if record.get('effective_at')
            else 'OBSERVED_AT_PLUS_GRANULARITY_AND_LATENCY' if latency is not None else 'UNKNOWN')
        checksum = record.get('raw_checksum')
        if checksum is None:
            checksum = _hash({'provider': provider, 'instrument': instrument,
                'observed_at': observed.isoformat(), 'effective_at': effective.isoformat() if effective else None,
                'values': values, 'metadata': metadata})
        if not isinstance(checksum, str) or not checksum:
            raise ValueError('LOCAL_IMPORT_CHECKSUM_INVALID')
        identity = (provider.strip(), instrument.strip(), observed)
        if instrument == 'BANKNIFTY_OPTIONS':
            identity += (values.get('side'), values.get('offset'), values.get('strike'),
                values.get('expiry'), values.get('contract_identity'))
        if identity in seen:
            if seen[identity] != checksum:
                raise ValueError('LOCAL_IMPORT_DUPLICATE_CONFLICT')
            continue
        observation = ResearchContextObservation(provider.strip(), instrument.strip(), observed,
            effective, latency, granularity, values, checksum, metadata)
        seen[identity] = checksum
        if observation.observed_at > source_observed_at or (
                observation.effective_at is not None and observation.effective_at > source_observed_at):
            raise ValueError('LOCAL_IMPORT_FUTURE_SOURCE_TIMESTAMP')
        observations.append(observation)
    if not observations:
        raise ValueError('LOCAL_IMPORT_NO_OBSERVATIONS')
    return observations


class LocalResearchContextImportService:
    """Import caller-supplied local observations without network access or imputation."""
    def __init__(self, repository=None, root=None, settings=None):
        self.repository = repository or ResearchContextRepository()
        self.root = Path(root or memory_root())
        self.settings = settings or ResearchContextSettings.from_env()
        self.enricher = ResearchContextEnrichmentService(self.repository, self.root, self.settings)

    def import_file(self, path, *, instrument_id, source_name, source_observed_at):
        path = Path(path)
        if not path.is_file():
            raise ValueError('LOCAL_IMPORT_FILE_NOT_FOUND')
        if not isinstance(source_name, str) or not source_name.strip():
            raise ValueError('LOCAL_IMPORT_SOURCE_NAME_REQUIRED')
        observed_at = _stamp(source_observed_at)
        if observed_at > datetime.now(UTC):
            raise ValueError('LOCAL_IMPORT_FUTURE_SOURCE_OBSERVATION')
        records, input_checksum, input_format = _local_file_records(path)
        observations = _local_observations(records, source_observed_at=observed_at, source_name=source_name)
        canonical = set(canonical_dates(self.root))
        grouped = defaultdict(list)
        for observation in observations:
            grouped[observation.observed_at.astimezone(IST).date()].append(observation)
        skipped, rows = [], []
        processed = []
        for day in sorted(grouped):
            if day not in canonical or day >= datetime.now(IST).date():
                skipped.append({'date': str(day), 'status': 'LOCAL_IMPORT_REQUIRES_COMPLETED_CANONICAL_DATE'})
                continue
            bounds = _index_bounds(self.repository.session(day), day)
            if not bounds:
                skipped.append({'date': str(day), 'status': 'INDEX_SESSION_NOT_VERIFIED'})
                continue
            day_observations = grouped[day]
            series = {name: [row for row in day_observations if row.instrument == name]
                      for name in LOCAL_SERIES}
            legs = [row for row in day_observations if row.instrument == 'BANKNIFTY_OPTIONS']
            information = {kind: [row for row in day_observations if row.instrument == kind]
                           for kind in UpstoxAnalyticsResearchProvider.MARKET_ENDPOINTS}
            option_rows = derive_option_context(legs, self.settings.option_width)
            rows.extend(self.enricher.derive_day(day, bounds, series, option_rows, information))
            processed.append(day)
        date_from, date_to = (min(processed), max(processed)) if processed else (min(grouped), max(grouped))
        local_manifest = {
            'source': LOCAL_IMPORT_PROVIDER, 'sourceName': source_name.strip(),
            'fileName': path.name, 'format': input_format, 'inputSha256': input_checksum,
            'recordCount': len(records), 'observationCount': len(observations),
            'observationChecksum': _hash(sorted(row.raw_checksum for row in observations)),
            'sourceObservedAt': observed_at.isoformat(),
            'availabilityBasis': 'CALLER_ATTESTED_LOCAL_IMPORT_NOT_INDEPENDENTLY_VERIFIED',
        }
        source_manifest = {
            'providers': {'localImport': {'state': 'READY_FOR_IMPORT', 'sourceName': source_name.strip()}},
            'failures': [], 'derivedChecksum': _hash(rows), 'sources': [local_manifest],
            'availabilityWarnings': sorted({row.instrument + ':' + str(row.metadata.get('availabilityBasis', 'UNKNOWN'))
                for row in observations if row.effective_at is None}),
            'skippedDates': skipped,
        }
        run = {
            'provider': LOCAL_IMPORT_PROVIDER, 'date_from': date_from, 'date_to': date_to,
            'context_version': RESEARCH_CONTEXT_VERSION,
            'configuration': {
                **self.settings.public_configuration(),
                'sourceConflictRelativeTolerance': CONFLICT_RELATIVE_TOLERANCE,
                'localImport': {
                    'sourceName': source_name.strip(), 'sourceObservedAt': observed_at.isoformat(),
                    'inputSha256': input_checksum, 'format': input_format,
                },
            },
            'source_manifest': source_manifest,
            'status': 'SOURCE_CONFLICT' if any(row['quality_status'] == 'SOURCE_CONFLICT' for row in rows)
                else 'NO_CONTEXT' if not rows
                else 'COMPLETE' if all(row['quality_status'] == 'PASS' for row in rows) and not skipped
                else 'PARTIAL',
            'raw_checksum': _hash(source_manifest['sources']),
        }
        run['run_id'] = _hash(run)
        for row in rows:
            row['run_id'] = run['run_id']
            row['context_checksum'] = _hash(row)
        self.repository.save(run, rows)
        return {'status': run['status'], 'runId': run['run_id'], 'dateFrom': str(date_from),
                'dateTo': str(date_to), 'rows': len(rows), 'skippedDates': skipped,
                'sourceManifest': local_manifest, 'coverage': context_coverage(self.repository, self.root)}


class ResearchContextBackfillService:
    """Chronological, restart-safe audit/import backfill. Never calls a provider."""
    def __init__(self, repository=None, root=None):
        self.repository = repository or ResearchContextRepository()
        self.root = Path(root or memory_root())

    def _dates(self, revision_id, instrument_id, from_date, to_date):
        canonical = canonical_dates(self.root)
        if revision_id and instrument_id is None:
            raise ValueError('INSTRUMENT_ID_REQUIRED')
        if revision_id:
            from .config import Settings
            from .repository import PostgresRepository
            repository = PostgresRepository(Settings.from_env().database_url)
            revision = repository.get_revision(str(revision_id))
            instrument_id = int(instrument_id)
            if instrument_id not in revision.instrument_ids:
                raise ValueError('INSTRUMENT_NOT_IN_REVISION')
            dates = sorted({row.timestamp.astimezone(IST).date()
                for row in repository.revision_candles(revision.id, instrument_id)})
        else:
            if instrument_id is None:
                raise ValueError('INSTRUMENT_ID_REQUIRED')
            dates = canonical
        if from_date is not None:
            dates = [day for day in dates if day >= from_date]
        if to_date is not None:
            dates = [day for day in dates if day <= to_date]
        if not dates:
            raise ValueError('NO_ELIGIBLE_HISTORICAL_DATES')
        if dates[-1] >= datetime.now(IST).date():
            raise ValueError('BACKFILL_REQUIRES_COMPLETED_HISTORICAL_DATES')
        return dates

    def _local_input(self, directory, day):
        directory = Path(directory)
        candidates = [directory / f'{day}{suffix}' for suffix in ('.json', '.jsonl', '.ndjson', '.csv')
                      if (directory / f'{day}{suffix}').is_file()]
        if len(candidates) > 1:
            raise ValueError('AMBIGUOUS_LOCAL_INPUT')
        return candidates[0] if candidates else None

    def backfill(self, *, revision_id=None, instrument_id=None, from_date=None, to_date=None,
                 input_dir=None, source_name=None, source_observed_at=None):
        from_date = date.fromisoformat(from_date) if isinstance(from_date, str) else from_date
        to_date = date.fromisoformat(to_date) if isinstance(to_date, str) else to_date
        if from_date and to_date and to_date < from_date:
            raise ValueError('INVALID_BACKFILL_RANGE')
        directory = Path(input_dir) if input_dir else None
        if directory is not None and not directory.is_dir():
            raise ValueError('LOCAL_INPUT_DIRECTORY_NOT_FOUND')
        dates = self._dates(revision_id, instrument_id, from_date, to_date)
        if directory is not None and (not source_name or not source_observed_at):
            raise ValueError('BACKFILL_LOCAL_IMPORT_PROVENANCE_REQUIRED')
        importer = LocalResearchContextImportService(self.repository, self.root) if directory is not None else None
        items = []
        for day in dates:
            item = {'date': str(day)}
            try:
                if importer:
                    path = self._local_input(directory, day)
                    if path is None:
                        item.update(status='SKIPPED', reasons=['LOCAL_INPUT_MISSING'])
                    else:
                        imported = importer.import_file(path, instrument_id=instrument_id,
                            source_name=source_name, source_observed_at=source_observed_at)
                        item.update(status='COMPLETED', runId=imported['runId'],
                            runStatus=imported['status'], sourceObservedAt=imported['sourceManifest']['sourceObservedAt'])
                else:
                    with self.repository.connection() as db:
                        runs = db.execute('''SELECT run_id,status,date_from,date_to,source_manifest,configuration
                            FROM research_context_runs WHERE context_version=%s AND date_from<=%s AND date_to>=%s
                            ORDER BY run_id''', (RESEARCH_CONTEXT_VERSION, day, day)).fetchall()
                    complete = [row for row in runs if row['status'] == 'COMPLETE']
                    if not complete:
                        item.update(status='SKIPPED', reasons=['NO_COMPLETE_RESEARCH_CONTEXT_RUN'],
                            candidates=[{'runId': row['run_id'], 'status': row['status']} for row in runs])
                    elif len(complete) > 1:
                        item.update(status='SKIPPED', reasons=['AMBIGUOUS_COMPLETE_RESEARCH_CONTEXT_RUNS'],
                            candidateRunIds=sorted(row['run_id'] for row in complete))
                    else:
                        run = complete[0]
                        timestamps = sorted({row.get('sourceObservedAt') for row in
                            run['source_manifest'].get('sources', []) if row.get('sourceObservedAt')})
                        item.update(status='COMPLETED', runId=run['run_id'], runStatus=run['status'],
                            sourceTimestamps=timestamps)
            except (ValueError, TypeError, OSError) as exc:
                item.update(status='SKIPPED', reasons=[str(exc)])
            except Exception as exc:
                item.update(status='SKIPPED', reasons=['BACKFILL_DATE_FAILED:' + type(exc).__name__])
            items.append(item)
        completed = [item for item in items if item['status'] == 'COMPLETED']
        skipped = [item for item in items if item['status'] == 'SKIPPED']
        fully_complete = bool(completed) and not skipped and all(
            item.get('runStatus') == 'COMPLETE' for item in completed)
        return {
            'status': 'COMPLETE' if fully_complete else 'PARTIAL' if completed else 'BLOCKED',
            'datesRequested': [str(day) for day in dates],
            'datesCompleted': [item['date'] for item in completed],
            'datesSkipped': [item['date'] for item in skipped],
            'missingReasons': {item['date']: item.get('reasons', []) for item in skipped},
            'runIds': {item['date']: item['runId'] for item in items if item.get('runId')},
            'sourceTimestamps': {item['date']: item.get('sourceTimestamps') or
                ([item['sourceObservedAt']] if item.get('sourceObservedAt') else []) for item in completed},
            'items': items,
            'providerCalls': False,
        }


def research_context_readiness(database_url=None, root=None):
    settings = ResearchContextSettings.from_env()
    root = Path(root or memory_root())
    repository = ResearchContextRepository(database_url)
    dhan, upstox = DhanHistoricalOptionProvider(settings, root), UpstoxAnalyticsResearchProvider(settings, root)
    states = {'dhan': dhan.configuration_state(), 'upstoxAnalytics': upstox.configuration_state(), 'upstoxPlus': upstox.plus_state()}
    coverage = context_coverage(repository, root)
    last = []
    try:
        for run in repository.last_runs():
            last.append({'provider': run['provider'], 'status': run['status'], 'createdAt': run['created_at'].isoformat(),
                         'providers': run['source_manifest'].get('providers', {}), 'failures': run['source_manifest'].get('failures', [])})
    except Exception:
        pass
    any_enabled = settings.dhan_enabled or settings.upstox_enabled
    ready_to_request = any(state == 'READY_FOR_REQUEST' for state in states.values())
    status = 'READY' if coverage['status'] == 'READY' else 'DISABLED' if not any_enabled else 'PARTIAL' if coverage['contextMinuteRows'] or ready_to_request else 'NOT_CONFIGURED'
    actions = []
    if states['dhan'] != 'READY_FOR_REQUEST' and coverage['missingDatesBySource']['options']:
        actions.append('Configure Dhan Data API to enrich historical BANKNIFTY monthly options.')
    if states['upstoxAnalytics'] != 'READY_FOR_REQUEST': actions.append('Configure Upstox Analytics Token for NIFTY50/VIX/global context.')
    if coverage['missingDatesBySource']['nifty50']: actions.append('Explicitly enrich missing NIFTY50 sessions using NSE_INDEX|Nifty 50; existing pinned runs stay unchanged.')
    if coverage['missingDatesBySource']['usdinr'] or coverage['missingDatesBySource']['gift'] or coverage['missingDatesBySource']['brent']:
        actions.append('Supply the official Upstox Global Instruments file with instrument identity and source latency metadata.')
    if ready_to_request and coverage['status'] != 'READY': actions.append('Run enrich-research-context explicitly; no enrichment runs during startup.')
    if coverage['status'] in ('NOT_CONFIGURED', 'UNAVAILABLE'): actions.append('Configure PostgreSQL and apply migration 018 through the normal deployment workflow.')
    if coverage['sourceConflicts']: actions.append('Inspect conflicting source facts or immutable run revisions; pin a reviewed run before feature generation.')
    return {'status': status, 'contextVersion': RESEARCH_CONTEXT_VERSION, 'providers': states,
            'lastRequestStates': last, 'coverage': coverage, 'nextRequiredAction': actions,
            'coverageMeaning': 'Sessions with usable observations, not a claim of complete minute coverage.',
            'configuration': settings.public_configuration(), 'liveProvider': 'ZERODHA', 'modelFeatureScope': 'PRICE_CORE_V2'}
