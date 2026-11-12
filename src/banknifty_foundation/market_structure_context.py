"""Versioned research context; identical closed-minute mathematics for live and history.

This is not a model feature schema. Historical retrieval time is never promoted
to prospective availability. Runtime reads additionally require receipt evidence.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from decimal import Decimal, InvalidOperation
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
from itertools import combinations
from pathlib import Path
from statistics import mean, median, pstdev
from threading import Event, Thread
import gzip
import csv
import io
import json
import logging
import os
import re
import zipfile
from urllib.parse import urlsplit

from .calendar import IST
from .config import ResearchContextSettings
from .paths import memory_root
from .research_context import (ResearchContextRepository, ResearchProviderError,
    UpstoxAnalyticsResearchProvider, _RawCache, _hash, _plain, _stamp, _number,
    _index_bounds, _ObservationIndex, cross_market_context, _context)

UTC = timezone.utc
MARKET_STRUCTURE_CONTEXT_VERSION = 'banknifty-market-structure-v1'
HISTORICAL_KNOWLEDGE_BASIS = 'OFFICIAL_PUBLICATION_DATE_CONSERVATIVE_NEXT_DAY'
HEAVYWEIGHT_LEADERS = ('HDFCBANK', 'ICICIBANK', 'SBIN', 'KOTAKBANK', 'AXISBANK')


def _historical_knowledge_at(rules):
    """Use the historical publication date only when its full provenance is verified."""
    fallback = rules.get('sourceObservedAt')
    publication = rules.get('sourcePublicationDate')
    historical = rules.get('historicalKnowledgeAvailableFrom')
    verified = (publication, historical, rules.get('historicalKnowledgeBasis'),
        rules.get('sourceReference'), rules.get('sourceUrl'))
    if not all(isinstance(value, str) and value.strip() for value in verified):
        return fallback
    if rules.get('historicalKnowledgeBasis') != HISTORICAL_KNOWLEDGE_BASIS:
        return fallback
    try:
        published = date.fromisoformat(publication)
        available = _stamp(historical).astimezone(IST).date()
    except (TypeError, ValueError):
        return fallback
    # The named conservative basis may never backdate knowledge before publication.
    return historical if available > published else fallback


@dataclass(frozen=True)
class BankNiftyConstituentMember:
    symbol: str
    instrument_key: str | None = None
    isin: str | None = None
    weight: float | None = None
    weight_source_quality: str = 'NONE'


@dataclass(frozen=True)
class BankNiftyConstituentSnapshot:
    snapshot_id: str
    effective_from: date
    effective_to: date
    members: tuple[BankNiftyConstituentMember, ...]
    source: str
    source_url: str | None
    source_observed_at: datetime
    source_quality: str
    source_checksum: str


class ConstituentUniverseService:
    def __init__(self, root=None):
        self.root = Path(root or memory_root())

    def resolve(self, day, *, decision_at=None):
        path = self.root / 'config/banknifty_constituent_snapshots.json'
        candidates = []
        configured = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else []
        for evidence in sorted((self.root/'config/official_constituents').glob('*.json')):
            document=json.loads(evidence.read_text(encoding='utf-8'))
            if document.get('checksum')!=_hash(document.get('snapshot')): raise ValueError('Membership evidence checksum mismatch')
            configured.append(document['snapshot'])
        for row in configured:
            start, end = date.fromisoformat(row['effectiveFrom']), date.fromisoformat(row['effectiveTo'])
            observed = _stamp(row['sourceObservedAt'])
            if not start <= day <= end or (decision_at and observed > _stamp(decision_at)): continue
            if row['sourceQuality'] not in ('OFFICIAL_INDEX', 'TRACKING_PROXY'): continue
            if row['sourceQuality']=='OFFICIAL_INDEX' and not OfficialConstituentImportService.valid_provenance(row):
                raise ValueError('Official membership source evidence required')
            members = []
            for item in row['symbols']:
                item = {'symbol': item} if isinstance(item, str) else item
                symbol, weight = item['symbol'], item.get('weight')
                quality = item.get('weightSourceQuality', 'NONE')
                if not re.fullmatch('[A-Z0-9]+', symbol): raise ValueError('Invalid constituent symbol')
                if quality not in ('OFFICIAL_INDEX', 'TRACKING_PROXY', 'NONE'): raise ValueError('Invalid weight provenance')
                if weight is not None and (_number(weight) is None or not 0 <= float(weight) <= 1 or quality == 'NONE'):
                    raise ValueError('Invalid constituent weight')
                members.append(BankNiftyConstituentMember(symbol, instrument_key=item.get('instrumentKey'), isin=item.get('isin'), weight=weight, weight_source_quality=quality))
            if not members or len({m.symbol for m in members}) != len(members): raise ValueError('Invalid membership snapshot')
            candidates.append(BankNiftyConstituentSnapshot(row['snapshotId'], start, end, tuple(members),
                row['source'], row.get('sourceUrl'), observed, row['sourceQuality'], _hash(row)))
        official=[s for s in candidates if s.source_quality=='OFFICIAL_INDEX']
        if official: candidates=official
        if len(candidates) > 1: raise ValueError('Overlapping constituent membership evidence; pin a non-overlapping date range')
        return candidates[0] if candidates else None

    def symbols(self, day, *, decision_at=None):
        try: snapshot = self.resolve(day, decision_at=decision_at)
        except (ValueError, KeyError, TypeError, OSError): return ()
        return tuple(m.symbol for m in snapshot.members) if snapshot else ()


class OfficialConstituentImportService:
    """Explicit local CSV import only. Does not download, infer weights or backdate evidence."""
    SOURCE_URL='https://www.niftyindices.com/IndexConstituent/ind_niftybanklist.csv'
    # Absolute normalized-weight tolerance: 0.001 = 0.1 percentage point,
    # allowing published rounding only, never renormalizing incomplete weights.
    WEIGHT_TOTAL_TOLERANCE=Decimal('0.001')

    def __init__(self,root=None): self.root=Path(root or memory_root())

    @staticmethod
    def valid_provenance(row):
        url=urlsplit(row.get('sourceUrl') or '')
        if url.scheme!='https' or url.hostname not in ('niftyindices.com','www.niftyindices.com') or url.username or url.password:
            return False
        # Preserve the previous explicit current-list importer contract.
        if row.get('source')=='NIFTY_INDICES_OFFICIAL_CSV' and row.get('sourceUrl')==OfficialConstituentImportService.SOURCE_URL:
            return bool(row.get('csvSha256') and row.get('effectiveRangeBasis'))
        try:
            date.fromisoformat(row['sourcePublicationDate'])
            _stamp(row['sourceObservedAt'])
        except (KeyError,TypeError,ValueError): return False
        return bool(row.get('sourceAuthority')=='NIFTY_INDICES' and row.get('sourceReference')
                    and row.get('effectiveRangeBasis') and re.fullmatch('[a-f0-9]{64}',row.get('rawArtifactSha256','')))

    def import_history(self,path,*,effective_from,effective_to,source_publication_date,
                       source_observed_at,source_reference,source_url,effective_range_basis,weight_unit):
        """Local-only, bounded ZIP/CSV parsing; a CSV must identify NIFTY BANK explicitly."""
        start,end=date.fromisoformat(str(effective_from)),date.fromisoformat(str(effective_to))
        published=date.fromisoformat(str(source_publication_date)); observed=_stamp(source_observed_at)
        if start>end or published>observed.astimezone(IST).date(): raise ValueError('Invalid official evidence dates')
        if weight_unit not in ('PERCENT','FRACTION','NONE'): raise ValueError('Explicit weight unit required')
        raw=Path(path).read_bytes()
        if len(raw)>20_000_000: raise ValueError('Official artifact too large')
        files=[]
        if zipfile.is_zipfile(io.BytesIO(raw)):
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                entries=[item for item in archive.infolist() if item.filename.lower().endswith('.csv')]
                if sum(item.file_size for item in entries)>20_000_000: raise ValueError('Official archive too large')
                files=[(item.filename,archive.read(item)) for item in entries]
        else: files=[(Path(path).name,raw)]
        members=[]; normalized_weights=[]
        for name,payload in files:
            reader=csv.DictReader(io.StringIO(payload.decode('utf-8-sig')))
            for original in reader:
                row={str(k).strip().lower():str(v or '').strip() for k,v in original.items()}
                index=row.get('index name',row.get('index','')).upper().replace('_',' ')
                if index and index not in ('NIFTY BANK','BANK NIFTY'): continue
                if not index and not re.search(r'(nifty[ _-]?bank|bank[ _-]?nifty)',name,re.I):
                    raise ValueError('NIFTY BANK identity required in CSV index column or artifact filename')
                symbol=row.get('symbol','').upper(); isin=row.get('isin code',row.get('isin','')) or None
                if not re.fullmatch('[A-Z0-9]+',symbol) or (isin and not re.fullmatch('IN[A-Z0-9]{10}',isin)):
                    raise ValueError('Invalid official constituent identity')
                populated=[row[key] for key in ('weight','weight (%)') if row.get(key)]
                weight=None
                if weight_unit=='NONE':
                    if populated: raise ValueError('Populated official weights require an explicit weight unit')
                else:
                    if not populated: raise ValueError('Missing official weight')
                    if len(set(populated))>1: raise ValueError('Conflicting official weight columns')
                    try: normalized=Decimal(populated[0])/(100 if weight_unit=='PERCENT' else 1)
                    except InvalidOperation: raise ValueError('Invalid official weight') from None
                    if not normalized.is_finite() or not 0<=normalized<=1: raise ValueError('Invalid official weight')
                    normalized_weights.append(normalized); weight=float(normalized)
                members.append({'symbol':symbol,'isin':isin,'weight':weight,
                                'weightSourceQuality':'OFFICIAL_INDEX' if weight is not None else 'NONE'})
        if not members or len({m['symbol'] for m in members})!=len(members): raise ValueError('Empty or duplicate NIFTY BANK evidence')
        if weight_unit!='NONE' and abs(sum(normalized_weights)-Decimal(1))>self.WEIGHT_TOTAL_TOLERANCE:
            raise ValueError('Official weights must total 1.0 within absolute tolerance 0.001')
        snapshot={'effectiveFrom':str(start),'effectiveTo':str(end),'sourcePublicationDate':str(published),
            'sourceObservedAt':observed.isoformat(),'sourceReference':str(source_reference).strip(),
            'sourceAuthority':'NIFTY_INDICES','sourceQuality':'OFFICIAL_INDEX','source':'NIFTY_INDICES_HISTORICAL_ARTIFACT',
            'sourceUrl':source_url,'effectiveRangeBasis':str(effective_range_basis).strip(),
            'rawArtifactSha256':sha256(raw).hexdigest(),'weightUnit':weight_unit,'symbols':sorted(members,key=lambda m:m['symbol'])}
        if not self.valid_provenance(snapshot): raise ValueError('Official Nifty Indices provenance required')
        snapshot['snapshotId']='nifty-bank-official-'+_hash(snapshot)
        document={'snapshot':snapshot,'checksum':_hash(snapshot)}
        directory=self.root/'config/official_constituents'; directory.mkdir(parents=True,exist_ok=True)
        target=directory/(snapshot['snapshotId']+'.json')
        for existing in directory.glob('*.json'):
            previous=json.loads(existing.read_text(encoding='utf-8'))
            old=previous['snapshot']
            if previous.get('checksum')!=_hash(old): raise ValueError('Membership evidence checksum mismatch')
            if old['snapshotId']!=snapshot['snapshotId'] and max(start,date.fromisoformat(old['effectiveFrom']))<=min(end,date.fromisoformat(old['effectiveTo'])):
                raise ValueError('AMBIGUOUS_OFFICIAL_EVIDENCE')
        try:
            with target.open('x',encoding='utf-8') as stream: json.dump(document,stream,sort_keys=True,indent=2)
        except FileExistsError:
            if json.loads(target.read_text(encoding='utf-8'))!=document: raise ValueError('Immutable membership evidence conflict') from None
        return {'snapshotId':snapshot['snapshotId'],'checksum':document['checksum'],'path':str(target),
            'membershipQuality':'OFFICIAL_INDEX','weightQuality':'OFFICIAL_INDEX' if all(m['weight'] is not None for m in members) else 'NONE',
            'status':'OFFICIAL_MEMBERSHIP_WITH_OFFICIAL_WEIGHTS' if all(m['weight'] is not None for m in members) else 'OFFICIAL_MEMBERSHIP_EQUAL_WEIGHT_PROXY',
            'weightingLineage':['OFFICIAL_MEMBERSHIP','OFFICIAL_INDEX_WEIGHTS' if all(m['weight'] is not None for m in members) else 'EQUAL_WEIGHT_RESEARCH_PROXY']}

    def import_csv(self,path,*,effective_from,effective_to,source_observed_at,source_url,effective_range_basis):
        if source_url!=self.SOURCE_URL: raise ValueError('Official Nifty Bank constituent source required')
        basis=str(effective_range_basis or '').strip()
        if not basis: raise ValueError('Non-empty effective range basis required')
        start,end=date.fromisoformat(str(effective_from)),date.fromisoformat(str(effective_to))
        observed=_stamp(source_observed_at)
        if start>end: raise ValueError('Invalid membership effective range')
        raw=Path(path).read_bytes(); reader=csv.DictReader(io.StringIO(raw.decode('utf-8-sig')))
        if not {'Symbol','ISIN Code'}.issubset(reader.fieldnames or []): raise ValueError('Official CSV Symbol and ISIN Code columns required')
        members=[]
        for row in reader:
            symbol,isin=row['Symbol'].strip(),row['ISIN Code'].strip()
            if not re.fullmatch('[A-Z0-9]+',symbol) or not re.fullmatch('IN[A-Z0-9]{10}',isin): raise ValueError('Invalid official constituent identity')
            members.append({'symbol':symbol,'isin':isin,'weight':None,'weightSourceQuality':'NONE'})
        if not members or len({m['symbol'] for m in members})!=len(members) or len({m['isin'] for m in members})!=len(members):
            raise ValueError('Empty or duplicate official constituent membership')
        row={'effectiveFrom':str(start),'effectiveTo':str(end),'sourceObservedAt':observed.isoformat(),
            'effectiveRangeBasis':basis,
            'source':'NIFTY_INDICES_OFFICIAL_CSV','sourceUrl':source_url,'sourceQuality':'OFFICIAL_INDEX',
            'csvSha256':sha256(raw).hexdigest(),'symbols':sorted(members,key=lambda m:m['symbol'])}
        row['snapshotId']='nifty-bank-official-'+_hash(row)
        checksum=_hash(row); directory=self.root/'config/official_constituents'
        directory.mkdir(parents=True,exist_ok=True); target=directory/(row['snapshotId']+'.json')
        document={'snapshot':row,'checksum':checksum}
        # Exclusive creation makes re-import idempotent without rewriting evidence.
        try:
            with target.open('x',encoding='utf-8') as stream: json.dump(document,stream,sort_keys=True,indent=2)
        except FileExistsError:
            if json.loads(target.read_text(encoding='utf-8'))!=document: raise ValueError('Immutable membership evidence conflict') from None
        return {'snapshotId':row['snapshotId'],'checksum':checksum,'sourceQuality':'OFFICIAL_INDEX','path':str(target),
            'effectiveRangeBasis':basis,
            'weightingMethod':'EQUAL_WEIGHT_PROXY','historicalDateAttestation':'CALLER_SUPPLIED_EFFECTIVE_RANGE_NOT_INFERRED_FROM_CURRENT_CSV'}


class HistoricalConstituentProvider(UpstoxAnalyticsResearchProvider):
    def resolve_members(self, snapshot, *, strict=True):
        configured = self._settings.bod_instruments_file
        if not configured: raise ResearchProviderError('NOT_CONFIGURED', 'UPSTOX_BOD_INSTRUMENTS_FILE_REQUIRED')
        path = Path(configured)
        raw = path.read_bytes()
        data = json.loads(gzip.decompress(raw) if path.suffix == '.gz' else raw)
        rows = data.get('instruments', data.get('data', [])) if isinstance(data, dict) else data
        if not isinstance(rows, list): raise ResearchProviderError('PROVIDER_ERROR', 'INVALID_BOD_METADATA')
        resolved = []
        for member in snapshot.members:
            matches = [r for r in rows if r.get('trading_symbol') == member.symbol
                and r.get('segment') == 'NSE_EQ' and r.get('instrument_type') == 'EQ'
                and str(r.get('instrument_key', '')).startswith('NSE_EQ|') and r.get('isin')]
            if len(matches) != 1:
                if strict: raise ResearchProviderError('NOT_CONFIGURED', 'CONSTITUENT_INSTRUMENT_UNRESOLVED')
                resolved.append(member)
                continue
            row = matches[0]
            if member.isin and member.isin!=row['isin']:
                raise ResearchProviderError('NOT_CONFIGURED','OFFICIAL_MEMBERSHIP_ISIN_MISMATCH')
            resolved.append(replace(member, instrument_key=row['instrument_key'], isin=row['isin']))
        self.source_manifest.append({'source': 'UPSTOX_BOD_INSTRUMENTS_FILE', 'checksum': sha256(raw).hexdigest(),
            'snapshotId': snapshot.snapshot_id, 'members': [asdict(m) for m in resolved]})
        return replace(snapshot, members=tuple(resolved))

    def candles(self, member, day, *, missing_only=True):
        if not member.instrument_key or not member.isin: raise ValueError('Resolve official BOD identity first')
        previous = self._cache
        self._cache = _RawCache(self._root, 'upstox/constituents/' + member.symbol)
        try:
            self._cache.save({'operation':'resolved_instrument','fromDate':str(day),'toDate':str(day)},
                {'instrument_key':member.instrument_key,'isin':member.isin,'trading_symbol':member.symbol},
                'UPSTOX_BOD_INSTRUMENTS_FILE')
            path = f'/v3/historical-candle/{self._key(member.instrument_key)}/minutes/1/{day}/{day}'
            raw = self._get(path, {'fromDate': str(day), 'toDate': str(day)}, missing_only=missing_only)
        finally:
            self._cache = previous
        metadata = {'instrumentKey': member.instrument_key, 'isin': member.isin, 'symbol': member.symbol,
            'retrievedAt': raw['retrieved_at'], 'sourceUrl': raw['source_url'], 'request': raw['request']}
        # Cash OI is deliberately absent, even when a provider sends a placeholder.
        return [replace(r, values={k: v for k, v in r.values.items() if k != 'oi'})
                for r in self._parse_candles(raw, member.symbol, day, 0, metadata)]


class UpstoxExpiredFuturesProvider(UpstoxAnalyticsResearchProvider):
    def session_history(self, day, *, missing_only=True):
        if not self._settings.upstox_plus_enabled:
            raise ResearchProviderError('SUBSCRIPTION_REQUIRED', 'UPSTOX_PLUS_EXPIRED_DATA_REQUIRED')
        expiries = sorted(e for e in self.historical_expiries(day,missing_only=missing_only) if e >= day)
        for expiry in expiries:
            raw = self._get('/v2/expired-instruments/future/contract',
                {'instrument_key': self.INSTRUMENTS['BANKNIFTY'], 'expiry_date': str(expiry)}, missing_only=missing_only)
            rows = raw['content'].get('data', [])
            contracts = [r for r in rows if r.get('instrument_type') == 'FUT'
                and str(r.get('underlying_symbol', r.get('name', ''))).replace(' ', '').upper() in ('BANKNIFTY', 'NIFTYBANK')
                and str(r.get('expiry', ''))[:10] == str(expiry) and r.get('instrument_key')
                and (not r.get('listing_date') or date.fromisoformat(r['listing_date'][:10]) <= day)]
            if not contracts: continue
            if len(contracts) != 1: raise ResearchProviderError('PROVIDER_ERROR', 'AMBIGUOUS_FRONT_FUTURE')
            contract = contracts[0]
            key = contract['instrument_key']
            candles = self._get(f'/v2/expired-instruments/historical-candle/{self._key(key)}/1minute/{day}/{day}',
                {'fromDate': str(day), 'toDate': str(day)}, missing_only=missing_only)
            metadata = {'contractIdentity': key, 'expiry': str(expiry), 'rollReason': 'NEAREST_NON_EXPIRED_SESSION_CONTRACT',
                'selectionDate': str(day), 'sourceMetadata': contract, 'contractChecksum': raw['content_checksum']}
            return self._parse_candles(candles, 'BANKNIFTY_FUT', day, 0, metadata), metadata
        raise ResearchProviderError('PROVIDER_ERROR', 'HISTORICAL_FRONT_FUTURE_UNAVAILABLE')


def _minute_map(rows, as_of):
    """Require exact completed bars; duplicate conflicts invalidate the timestamp."""
    output, conflicts = {}, set()
    for row in rows:
        at = _stamp(row['at'])
        if at.astimezone(IST).date() != as_of.astimezone(IST).date() or at + timedelta(minutes=1) > as_of: continue
        if row.get('effective_at') and _stamp(row['effective_at']) > as_of: continue
        values = {k: _number(row.get(k), positive=k in ('open', 'high', 'low', 'close')) for k in ('open', 'high', 'low', 'close', 'volume', 'oi')}
        if any(values[k] is None for k in ('open', 'high', 'low', 'close')): continue
        if not values['low'] <= min(values['open'], values['close']) <= max(values['open'], values['close']) <= values['high']: continue
        if at in output and output[at] != values: conflicts.add(at)
        output[at] = values
    return {at: row for at, row in output.items() if at not in conflicts}


def _ret(rows, at, horizon):
    # Every intervening minute must exist; never bridge gaps or sessions.
    if not all(at - timedelta(minutes=i) in rows for i in range(horizon + 1)): return None
    return rows[at]['close'] / rows[at - timedelta(minutes=horizon)]['close'] - 1


def _difference(left, right):
    return left - right if left is not None and right is not None else None


def _spike(rows, at):
    values = [rows.get(at - timedelta(minutes=i), {}).get('volume') for i in range(1, 21)]
    current = rows.get(at, {}).get('volume')
    return current / mean(values) if current is not None and all(v is not None for v in values) and mean(values) > 0 else None


def constituent_context(snapshot, series, index_rows, as_of):
    if snapshot is None: return {'status': 'CONSTITUENT_UNIVERSE_UNAVAILABLE'}
    as_of = _stamp(as_of)
    if not snapshot.effective_from<=as_of.astimezone(IST).date()<=snapshot.effective_to:
        return {'status':'CONSTITUENT_UNIVERSE_UNAVAILABLE'}
    at = as_of.replace(second=0, microsecond=0) - timedelta(minutes=1)
    maps = {m.symbol: _minute_map(series.get(m.symbol, []), as_of) for m in snapshot.members}
    available = sum(at in rows for rows in maps.values())
    output = {'status': 'PASS' if available == len(maps) else 'PARTIAL', 'snapshotId': snapshot.snapshot_id,
        'snapshotChecksum':snapshot.source_checksum,'sourceObservedAt':snapshot.source_observed_at.isoformat(),
        'effectiveFrom':str(snapshot.effective_from),'effectiveTo':str(snapshot.effective_to),
        'constituentSymbols':list(maps),
        'sourceQuality': snapshot.source_quality, 'weighting': 'EQUAL_WEIGHT',
        'constituent_count_expected': len(maps), 'constituent_count_available': available,
        'constituent_completeness': available / len(maps), 'heavyweights': {}}
    returns = {h: {s: _ret(rows, at, h) for s, rows in maps.items()} for h in (1, 5, 10, 15)}
    for h, values in returns.items():
        valid = [r for r in values.values() if r is not None]
        output[f'return_count_{h}m'] = len(valid)
        for direction in ('up', 'down'):
            output[f'fraction_{direction}_{h}m'] = sum((r > 0 if direction == 'up' else r < 0) for r in valid) / len(valid) if valid else None
        output[f'breadth_{h}m'] = _difference(output[f'fraction_up_{h}m'], output[f'fraction_down_{h}m'])
        if h in (1, 5, 10):
            output[f'median_constituent_return_{h}m'] = median(valid) if valid else None
            output[f'return_dispersion_{h}m'] = pstdev(valid) if len(valid) > 1 else None
        if h in (5, 10): output[f'cross_sectional_std_{h}m'] = output[f'return_dispersion_{h}m']
    output['breadth_acceleration'] = _difference(output['breadth_1m'], output['breadth_5m'])
    output['breadth_acceleration_definition']='breadth_1m_minus_breadth_5m'
    five = [r for r in returns[5].values() if r is not None]
    output.update(leader_return=max(five) if five else None, laggard_return=min(five) if five else None,
        leader_laggard_spread=max(five)-min(five) if five else None,
        constituent_return_dispersion=output['return_dispersion_5m'],
        fraction_same_direction_5m=max(sum(r > 0 for r in five), sum(r < 0 for r in five))/len(five) if five else None)
    for symbol in HEAVYWEIGHT_LEADERS:
        output['heavyweights'][symbol] = {f'return_{h}m': returns[h].get(symbol) for h in returns}
    index = _minute_map(index_rows, as_of)
    for h in (5, 10):
        for name, symbols in (('top5', [s for s in maps if s in HEAVYWEIGHT_LEADERS]), ('rest', [s for s in maps if s not in HEAVYWEIGHT_LEADERS])):
            values = [returns[h][s] for s in symbols]
            output[f'{name}_equal_return_{h}m'] = mean(values) if values and all(v is not None for v in values) else None
            if name=='top5' and len(symbols)!=len(HEAVYWEIGHT_LEADERS): output[f'{name}_equal_return_{h}m']=None
        basket = list(returns[h].values())
        output[f'equal_bank_basket_return_{h}m'] = mean(basket) if basket and all(v is not None for v in basket) else None
        output[f'index_breadth_residual_{h}m'] = _difference(_ret(index, at, h), output[f'equal_bank_basket_return_{h}m'])
    output['top5_rest_divergence'] = _difference(output['top5_equal_return_5m'], output['rest_equal_return_5m'])
    volume = [(_spike(rows, at), returns[1][symbol]) for symbol, rows in maps.items()]
    volume = [(spike, ret) for spike, ret in volume if spike is not None and ret is not None]
    output['volume_spike_constituent_count']=len(volume)
    output['volume_spike_definition'] = 'CURRENT_VOLUME_GT_2X_PRIOR_20_COMPLETE_MINUTES_MEAN'
    for name, positive in (('positive', True), ('negative', False)):
        output[f'{name}_volume_spike_fraction'] = sum(s > 2 and (r > 0 if positive else r < 0) for s, r in volume)/len(volume) if volume else None
    vectors = []
    for rows in maps.values():
        vector = [_ret(rows, at-timedelta(minutes=i), 1) for i in range(20)]
        if all(v is not None for v in vector) and pstdev(vector) > 0:
            center,scale=mean(vector),pstdev(vector)
            vectors.append([(v-center)/scale for v in vector])
    correlations = [max(-1,min(1,sum(a*b for a,b in zip(x,y))/20)) for x,y in combinations(vectors,2)]
    output['median_pairwise_correlation_20m'] = median(correlations) if correlations else None
    output['correlation_pair_count'] = len(correlations)
    return output


def futures_context(future_rows, spot_rows, as_of, identity=None):
    from .big_move_features import classify_futures_buildup
    as_of=_stamp(as_of)
    at = _stamp(as_of).replace(second=0, microsecond=0) - timedelta(minutes=1)
    future, spot = _minute_map(future_rows, as_of), _minute_map(spot_rows, as_of)
    current, cash = future.get(at), spot.get(at)
    if not current or not cash: return {'status': 'FUTURES_CONTEXT_UNAVAILABLE', 'contract': identity}
    basis = current['close'] - cash['close']
    output = {'status': 'PASS' if current['oi'] is not None and current['volume'] is not None else 'PARTIAL', 'contract': identity, 'future_spot_basis': basis,
        'future_spot_basis_pct': basis/cash['close'], 'future_oi': current['oi'],
        'future_volume': current['volume'], 'future_volume_spike': _spike(future, at)}
    for h in (1, 5, 10):
        f, s = _ret(future, at, h), _ret(spot, at, h)
        output[f'future_return_{h}m'], output[f'spot_return_{h}m'] = f, s
        prior_f, prior_s = future.get(at-timedelta(minutes=h)), spot.get(at-timedelta(minutes=h))
        output[f'basis_change_{h}m'] = basis-(prior_f['close']-prior_s['close']) if f is not None and s is not None else None
        if h in (1, 5):
            output[f'future_minus_spot_return_{h}m'] = _difference(f, s)
            output[f'future_oi_change_{h}m'] = _difference(current['oi'], prior_f['oi']) if f is not None else None
    price, oi = output['future_return_1m'], output['future_oi_change_1m']
    output['buildup'] = classify_futures_buildup(price, oi) if price is not None and oi is not None else 'UNKNOWN'
    output['futures_lead_divergence'] = {str(h): output[f'future_minus_spot_return_{h}m'] for h in (1, 5)}
    return output


def market_structure(as_of, session, root=None, *, rules=None, scheduled_configuration=None):
    local = _stamp(as_of).astimezone(IST)
    bounds = _index_bounds(session, local.date())
    if rules is None:
        root = Path(root or memory_root())
        path = root/'config/market_structure_rules.json'
        rules = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
    effective = date.fromisoformat(rules['casEffectiveFrom']) if rules.get('casEffectiveFrom') else None
    regime = 'UNKNOWN' if not effective else 'PRE_CAS_2026' if local.date() < effective else 'CAS_2026'
    historical = rules.get('historicalKnowledgeAvailableFrom')
    knowledge_at = _historical_knowledge_at(rules)
    phases = []
    if rules.get('sourceUrl') and knowledge_at and _stamp(knowledge_at) <= as_of:
        for item in rules.get('phases', []):
            if item['phase'] not in ('CONTINUOUS', 'CAS_REFERENCE', 'CAS_ORDER_ENTRY_MARKET_LIMIT', 'CAS_ORDER_ENTRY_LIMIT_ONLY', 'CAS_MATCHING'): raise ValueError('Invalid CAS phase')
            start, end = (datetime.combine(local.date(), time.fromisoformat(item[k]), IST) for k in ('from', 'to'))
            if start >= end or (phases and start < phases[-1][1]): raise ValueError('Overlapping CAS phases')
            phases.append((start, end, item['phase']))
    phase = 'UNKNOWN'
    if bounds and as_of >= bounds[1]:
        phase = next((p for start,end,p in phases if start<=local<end and p=='CAS_MATCHING'),'POST_INDEX_CLOSE')
    elif bounds and bounds[0] <= as_of < bounds[1]:
        phase = 'CONTINUOUS' if regime == 'PRE_CAS_2026' else next((p for start, end, p in phases if start <= local < end), 'UNKNOWN')
    result = {'version': MARKET_STRUCTURE_CONTEXT_VERSION, 'regime': regime, 'phase': phase,
        'CASRegimeAvailable': regime != 'UNKNOWN', 'CASPhaseTimesAvailable': bool(phases),
        'rulesChecksum': _hash(rules), 'ruleSourceObservedAt':rules.get('sourceObservedAt'),
        'ruleSourcePublicationDate':rules.get('sourcePublicationDate'),
        'ruleHistoricalKnowledgeAvailableFrom':historical,
        'ruleHistoricalKnowledgeBasis':rules.get('historicalKnowledgeBasis'),
        'ruleSourceReference':rules.get('sourceReference'),
        'ruleKnowledgeAvailableAt':knowledge_at if phases else None,
        'indexPredictionClose':bounds[1].isoformat() if bounds else None, 'horizons': {}}
    for horizon in (10, 20, 30):
        expiry = local+timedelta(minutes=horizon)
        crossing = None if phase == 'UNKNOWN' else any(local < boundary <= expiry for start, end, _ in phases for boundary in (start, end)) if regime == 'CAS_2026' else False
        cas_crossing=crossing
        if bounds and as_of < bounds[1] <= expiry: crossing=True
        result['horizons'][str(horizon)] = {'crossesMarketStructureBoundary': crossing,
            'crossesCasBoundary':cas_crossing,
            'crossesIndexClose': expiry >= bounds[1] if bounds else None}
    from .news.scheduled import scheduled_events
    schedule = scheduled_events(now=as_of, root=root, configuration=scheduled_configuration)
    visible = schedule.get('visibleEvents', schedule.get('events', []))
    future = [e for e in visible if e.get('knowledgeAvailableAt') and _stamp(e['knowledgeAvailableAt']) <= as_of
        and e.get('timingPrecision') == 'EXACT_TIME' and _stamp(e['scheduledAt']) >= as_of]
    next_event = min(future, key=lambda e: _stamp(e['scheduledAt']), default=None)
    result.update(scheduledConfigurationStatus=schedule.get('status', 'NOT_CONFIGURED'),
        scheduledConfiguredEventCount=schedule.get('configuredEventCount', 0),
        scheduledVisibleEventCount=schedule.get('visibleEventCount', len(visible)),
        minutes_to_scheduled_event=(_stamp(next_event['scheduledAt'])-as_of).total_seconds()/60 if next_event else None,
        scheduled_event_type=next_event['eventType'] if next_event else None,
        scheduled_event_timing_precision=next_event.get('timingPrecision') if next_event else None)
    return result


def _observation_rows(rows):
    return [{'at': r.observed_at, 'effective_at': r.effective_at, **r.values} for r in rows if r.effective_at is not None]


class MarketStructureContextRepository(ResearchContextRepository):
    @staticmethod
    def insert(db, table, row, keys):
        from psycopg import sql
        from psycopg.types.json import Jsonb
        columns = list(row)
        db.execute(sql.SQL('INSERT INTO {} ({}) VALUES ({}) ON CONFLICT DO NOTHING').format(sql.Identifier(table),
            sql.SQL(',').join(map(sql.Identifier, columns)), sql.SQL(',').join(sql.Placeholder() for _ in columns)),
            [Jsonb(_plain(row[k])) if isinstance(row[k], (dict, list, tuple)) else row[k] for k in columns])
        stored = db.execute(sql.SQL('SELECT * FROM {} WHERE {}').format(sql.Identifier(table),
            sql.SQL(' AND ').join(sql.SQL('{}=%s').format(sql.Identifier(k)) for k in keys)), [row[k] for k in keys]).fetchone()
        # Numeric weights are stored as SQL numeric; compare JSON canonical floating representation.
        values = {k: float(stored[k]) if k == 'weight' and stored[k] is not None else stored[k] for k in row} if stored else None
        if values is None or _hash(values) != _hash(row): raise ValueError('IMMUTABLE_CONTEXT_CONFLICT')

    def save_structure(self, snapshot, run, minutes):
        with self.connection(write=True) as db:
            if snapshot:
                self.insert(db, 'banknifty_constituent_snapshots', {k: v for k, v in asdict(snapshot).items() if k != 'members'}, ['snapshot_id'])
                if all(m.instrument_key and m.isin for m in snapshot.members):
                    for member in snapshot.members:
                        self.insert(db, 'banknifty_constituent_members', {'snapshot_id': snapshot.snapshot_id, **asdict(member)}, ['snapshot_id', 'symbol'])
            self.insert(db, 'market_structure_context_runs', run, ['run_id'])
            for row in minutes: self.insert(db, 'market_structure_context_minutes', row, ['run_id', 'as_of'])


class MarketStructureContextService:
    def __init__(self, repository=None, root=None, settings=None):
        self.repository = repository or MarketStructureContextRepository()
        self.root = Path(root or memory_root())
        self.settings = settings or ResearchContextSettings.from_env()

    def enrich_day(self, day, *, missing_only=True):
        if day >= datetime.now(IST).date(): raise ValueError('Historical enrichment accepts completed past sessions only')
        session = self.repository.session(day)
        bounds = _index_bounds(session, day)
        if not bounds: return {'status': 'SESSION_UNAVAILABLE', 'date': str(day)}
        constituent = HistoricalConstituentProvider(self.settings, self.root)
        futures = UpstoxExpiredFuturesProvider(self.settings, self.root)
        index_provider = UpstoxAnalyticsResearchProvider(self.settings, self.root)
        try: snapshot = ConstituentUniverseService(self.root).resolve(day)
        except (ValueError,TypeError,KeyError,OSError): snapshot=None
        series, states, future, identity = {}, {}, [], None
        if snapshot:
            try:
                snapshot = constituent.resolve_members(snapshot,strict=False)
                for member in snapshot.members:
                    if not member.instrument_key:
                        states[member.symbol]='CONSTITUENT_INSTRUMENT_UNRESOLVED'
                        continue
                    try: series[member.symbol] = _observation_rows(constituent.candles(member, day, missing_only=missing_only))
                    except (ResearchProviderError, ValueError, TypeError, KeyError, OSError) as exc: states[member.symbol] = getattr(exc,'state','PROVIDER_ERROR')
            except (ResearchProviderError, OSError, ValueError, TypeError, KeyError) as exc: states['constituents'] = getattr(exc, 'state', 'NOT_CONFIGURED')
        else: states['constituents'] = 'CONSTITUENT_UNIVERSE_UNAVAILABLE'
        try:
            future, identity = futures.session_history(day, missing_only=missing_only)
            future = _observation_rows(future)
        except (ResearchProviderError, ValueError, TypeError, KeyError, OSError) as exc: states['futures'] = getattr(exc,'state','PROVIDER_ERROR')
        try: spot = _observation_rows(index_provider.historical_candles('BANKNIFTY', day, missing_only=missing_only))
        except (ResearchProviderError, ValueError, TypeError, KeyError, OSError) as exc:
            spot = []; states['index'] = getattr(exc,'state','PROVIDER_ERROR')
        manifest = {'sources': constituent.source_manifest+futures.source_manifest+index_provider.source_manifest,
            'states': states, 'membership': snapshot.source_checksum if snapshot else None,
            'session': _plain(session), 'basis': 'RETROSPECTIVE_COMPLETED_CANDLES_NOT_RUNTIME_RECEIPT'}
        for filename in ('market_structure_rules.json','scheduled_events.json'):
            path=self.root/'config'/filename
            manifest[filename]=json.loads(path.read_text(encoding='utf-8')) if path.is_file() else None
        lineage={'sourceManifestChecksum':_hash(manifest),'membershipSnapshotId':snapshot.snapshot_id if snapshot else None,
            'availabilityBasis':manifest['basis']}
        rows = []
        for minute in range(1, int((bounds[1]-bounds[0]).total_seconds()/60)+1):
            at = bounds[0]+timedelta(minutes=minute)
            breadth = constituent_context(snapshot, series, spot, at)
            flow = futures_context(future, spot, at, identity)
            if not future: flow['status'] = states.get('futures', flow['status'])
            rows.append({'session_date': day, 'as_of': at, 'constituent_context': breadth, 'futures_context': flow,
                'market_structure': market_structure(at, session, self.root), 'source_lineage': lineage,
                'quality_status': 'PASS' if breadth['status'] == flow['status'] == 'PASS' else 'PARTIAL'})
        checksum = _hash(rows)
        run_id = _hash([MARKET_STRUCTURE_CONTEXT_VERSION, day, checksum])
        run = {'run_id': run_id, 'date_from': day, 'date_to': day, 'context_version': MARKET_STRUCTURE_CONTEXT_VERSION,
            'source_manifest': manifest, 'status': 'PASS' if all(r['quality_status'] == 'PASS' for r in rows) else 'PARTIAL', 'checksum': checksum}
        for row in rows:
            row['run_id'] = run_id; row['context_checksum'] = _hash(row)
        self.repository.save_structure(snapshot, run, rows)
        return {'runId': run_id, 'status': run['status'], 'minutes': len(rows), 'states': states}


class MarketStructureContextReader:
    def __init__(self, repository=None, root=None):
        self.repository = repository or MarketStructureContextRepository()
        self.root = Path(root or memory_root())

    def day(self, day):
        with self.repository.connection() as db:
            rows = db.execute('''SELECT m.* FROM market_structure_context_minutes m JOIN market_structure_context_runs r USING(run_id)
                WHERE m.session_date=%s AND r.context_version=%s ORDER BY as_of,run_id''', (day, MARKET_STRUCTURE_CONTEXT_VERSION)).fetchall()
        return {'date': str(day), 'version': MARKET_STRUCTURE_CONTEXT_VERSION, 'minutes': _plain(rows), 'requiresPinnedRun': True}

    def at_or_before(self, as_of, *, run_id):
        with self.repository.connection() as db:
            row = db.execute('''SELECT m.* FROM market_structure_context_minutes m JOIN market_structure_context_runs r USING(run_id)
                WHERE m.run_id=%s AND r.context_version=%s AND m.as_of<=%s AND m.as_of>=%s
                ORDER BY m.as_of DESC LIMIT 1''', (run_id, MARKET_STRUCTURE_CONTEXT_VERSION, as_of, as_of-timedelta(seconds=60))).fetchone()
        if row and _hash({k: v for k, v in row.items() if k != 'context_checksum'}) != row['context_checksum']:
            raise ValueError('CONTEXT_CHECKSUM_MISMATCH')
        return _plain(row)

    def runtime(self, market_as_of, decision_at):
        """Single repeatable snapshot, newest eligible revision BEFORE quality filtering."""
        as_of, decision = _stamp(market_as_of), _stamp(decision_at)
        if as_of > decision: raise ValueError('Market cutoff cannot exceed decision time')
        day = as_of.astimezone(IST).date()
        try: snapshot = ConstituentUniverseService(self.root).resolve(day, decision_at=decision)
        except (ValueError,TypeError,KeyError,OSError): snapshot=None
        symbols = [m.symbol for m in snapshot.members] if snapshot else []
        with self.repository.connection() as db:
            session = db.execute('SELECT session_date,market_open_utc,market_close_utc,status,source FROM trading_sessions WHERE session_date=%s', (day,)).fetchone()
            bounds = _index_bounds(session, day)
            if not bounds or not bounds[0] < as_of <= bounds[1]:
                return {'status':'MARKET_CLOSED' if bounds or (session and session['status']=='CLOSED') else 'SESSION_UNAVAILABLE',
                    'marketStructure':market_structure(as_of,session,self.root)}
            rows = db.execute('''SELECT DISTINCT ON(m.instrument_id,m.minute_at) i.id,i.tradingsymbol,i.instrument_type,i.exchange,m.payload
                FROM live_minutes_1m m JOIN canonical_instruments i ON i.id=m.instrument_id
                WHERE m.minute_at>=%s AND m.minute_at+interval '1 minute'<=%s
                  AND (m.payload->>'first_finalized_at')::timestamptz<=%s
                  AND ((i.exchange='NSE' AND (i.tradingsymbol=ANY(%s) OR i.tradingsymbol IN ('NIFTY BANK','BANKNIFTY')))
                    OR (i.exchange='NFO' AND i.instrument_type='FUT' AND i.name='BANKNIFTY' AND i.expiry>=%s))
                ORDER BY m.instrument_id,m.minute_at,m.revision DESC''', (bounds[0], as_of, decision, symbols, day)).fetchall()
            selection = db.execute('''SELECT s.instrument_id,s.payload FROM live_futures_selections s
                JOIN canonical_instruments i ON i.id=s.instrument_id WHERE s.selected_at<=%s
                  AND i.exchange='NFO' AND i.instrument_type='FUT' AND i.name='BANKNIFTY' AND i.expiry>=%s
                ORDER BY s.selected_at DESC,s.selection_id DESC LIMIT 1''', (min(as_of, decision),day)).fetchone()
            globals_rows = db.execute('''SELECT * FROM runtime_research_observations
                WHERE first_seen_at<=%s AND effective_at<=%s AND observed_at>=%s ORDER BY first_seen_at''',
                (decision, as_of, as_of-timedelta(minutes=31))).fetchall()
            option_universe=db.execute('''SELECT payload FROM live_option_universes WHERE selected_at<=%s AND expiry>=%s
                ORDER BY selected_at DESC,universe_id DESC LIMIT 1''',(min(as_of,decision),day)).fetchone()
            option_rows=[]
            if option_universe and option_universe['payload'].get('quality_state')=='PASS':
                option_rows=db.execute('''SELECT s.payload AS snapshot,m.payload AS minute FROM live_option_snapshots_1m s
                    JOIN canonical_instruments i ON i.id=s.instrument_id
                    JOIN LATERAL (SELECT payload FROM live_minutes_1m m WHERE m.instrument_id=s.instrument_id AND m.minute_at=s.minute_at
                        AND (m.payload->>'first_finalized_at')::timestamptz<=%s ORDER BY revision DESC LIMIT 1) m ON true
                    WHERE s.universe_id=%s AND i.exchange='NFO' AND i.name='BANKNIFTY'
                        AND s.minute_at>=%s AND s.minute_at+interval '1 minute'<=%s
                        AND (s.payload->>'first_seen_at')::timestamptz<=%s''',
                    (decision,option_universe['payload']['universe_id'],as_of-timedelta(minutes=2),as_of,decision)).fetchall()
        grouped, lineage = {}, []
        # Never merge different canonical snapshot identities into a synthetic price series.
        identities = {}
        for row in rows:
            symbol = 'BANKNIFTY' if row['exchange'] == 'NSE' and row['tradingsymbol'] in ('NIFTY BANK', 'BANKNIFTY') else row['tradingsymbol']
            if row['instrument_type'] == 'FUT':
                if not selection or row['id'] != selection['instrument_id']: continue
                symbol = 'BANKNIFTY_FUT'
            identities.setdefault(symbol, set()).add(row['id'])
        for row in rows:
            p = row['payload']
            symbol = 'BANKNIFTY' if row['exchange'] == 'NSE' and row['tradingsymbol'] in ('NIFTY BANK', 'BANKNIFTY') else row['tradingsymbol']
            if row['instrument_type'] == 'FUT':
                if not selection or row['id'] != selection['instrument_id']: continue
                symbol = 'BANKNIFTY_FUT'
            if len(identities.get(symbol, ())) != 1 or p['quality_state'] != 'PASS': continue
            grouped.setdefault(symbol, []).append({**p, 'at': p['minute_at'], 'volume': p.get('volume_delta'), 'oi': p.get('oi_close')})
            lineage.append({'instrumentId': row['id'], 'minute': p['minute_at'], 'revision': p['revision'], 'firstFinalizedAt': p['first_finalized_at']})
        spot = grouped.get('BANKNIFTY', [])
        settings = ResearchContextSettings.from_env()
        from .research_context import ResearchContextObservation
        global_context = {}
        for name in ('NIFTY50', 'INDIA_VIX', 'GIFT_NIFTY', 'BRENT', 'USDINR'):
            observations = []
            for row in globals_rows:
                if row['instrument'] != name or row['quality_status'] != 'PASS': continue
                p = row['payload']
                observations.append(ResearchContextObservation(p['provider'], name, _stamp(p['observed_at']), _stamp(p['effective_at']),
                    p['source_latency'], p['granularity_seconds'], p['values'], p['raw_checksum'],
                    {**p['metadata'], 'first_seen_at': row['first_seen_at'].isoformat()}))
            global_context[name] = cross_market_context(_ObservationIndex(observations), as_of, settings.staleness_seconds.get(name, 120))
        options=None
        if option_rows:
            from .research_context import derive_option_context
            observations=[]
            spot_by_at=_minute_map(spot,as_of)
            universe=option_universe['payload']
            for row in option_rows:
                p,m=row['snapshot'],row['minute']
                at=_stamp(p['minute_at'])
                if p['quality_state']!='PASS' or m['quality_state']!='PASS' or p['instrument_id'] not in universe['instrument_ids'] or at not in spot_by_at: continue
                values={key:_number(m.get(key),positive=True) for key in ('open','high','low','close')}
                values.update(side=p['option_type'],offset=p['atm_offset'],strike=_number(p['strike'],positive=True),
                    expiry=p['expiry'],expiry_type='UNKNOWN',contract_identity=str(p['instrument_id']),
                    spot=spot_by_at[at]['close'],volume=_number(p.get('volume_delta')),oi=_number(p.get('oi')),iv=None)
                observations.append(ResearchContextObservation('ZERODHA_LIVE','OPTIONS',at,at+timedelta(minutes=1),0,60,values,
                    _hash(p),{'first_seen_at':p['first_seen_at'],'first_finalized_at':m['first_finalized_at']}))
            derived=derive_option_context(observations,universe['strike_width'])
            latest=next((r for r in reversed(derived) if r.observed_at==as_of-timedelta(minutes=1)),None)
            options=_context(latest)
            if options:
                options['ivStatus']='PROSPECTIVE_ONLY_UNAVAILABLE'
                options['receiptEvidence']=[{'snapshotId':r['snapshot']['snapshot_id'],'firstSeenAt':r['snapshot']['first_seen_at'],
                    'firstFinalizedAt':r['minute']['first_finalized_at']} for r in option_rows]
        return {'status': 'AVAILABLE', 'marketAsOf': as_of.isoformat(), 'decisionAt': decision.isoformat(),
            'stalenessPolicy':{'version':settings.staleness_version,'seconds':settings.staleness_seconds},
            'session':_plain(session),
            'priceRows': _plain(spot), 'constituentContext': constituent_context(snapshot, grouped, spot, as_of),
            'futuresContext': futures_context(grouped.get('BANKNIFTY_FUT', []), spot, as_of, selection['payload'] if selection else None),
            'marketStructure': market_structure(as_of, session, self.root), 'indiaMarketContext':global_context.pop('NIFTY50',None), 'globalContext': global_context,
            'globalContextStatus':'DISABLED' if not settings.upstox_enabled else 'AVAILABLE' if any(global_context.values()) else 'STALE_OR_UNAVAILABLE',
            'sourceLineage': lineage, 'optionsContext': options, 'optionsStatus': 'AVAILABLE' if options else 'POINT_IN_TIME_OPTIONS_UNAVAILABLE'}


class RuntimeResearchContextPoller:
    """Optional REST sampling, separate from the canonical Zerodha ticker."""
    def __init__(self, repository=None, root=None):
        self.repository = repository or MarketStructureContextRepository()
        self.root = Path(root or memory_root())
        self.stop_event, self.thread = Event(), None
        self.state = 'DISABLED'

    def start(self):
        if os.getenv('MARKET_STRUCTURE_CONTEXT_ENABLED', 'false').lower() != 'true': return
        try: settings = ResearchContextSettings.from_env()
        except (ValueError,TypeError):
            self.state='NOT_CONFIGURED'; return
        if not settings.upstox_enabled: return
        self.interval=settings.market_structure_interval
        if self.thread and self.thread.is_alive(): return
        self.stop_event.clear()
        self.thread = Thread(target=self._run, name='runtime-research-context', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread: self.thread.join(timeout=35)

    def _run(self):
        interval = self.interval
        provider = UpstoxAnalyticsResearchProvider(root=self.root)
        # Intraday partial response revisions must not poison G1's completed-day cache.
        provider._cache=_RawCache(self.root,'upstox/runtime')
        while not self.stop_event.is_set():
            provider.source_manifest.clear()
            now = datetime.now(UTC)
            try:
                session = self.repository.session(now.astimezone(IST).date())
                bounds = _index_bounds(session, now.astimezone(IST).date())
                if bounds and bounds[0] <= now <= bounds[1]:
                    for name in ('NIFTY50', 'INDIA_VIX', 'GIFT_NIFTY', 'BRENT', 'USDINR'):
                        if self.stop_event.is_set(): break
                        try:
                            observations = provider.historical_candles(name, now.astimezone(IST).date(), missing_only=False)
                            received = datetime.now(UTC)
                            with self.repository.connection(write=True) as db:
                                for observation in observations:
                                    if observation.observed_at+timedelta(minutes=1) > received: continue
                                    payload = _context(observation)
                                    identifier = _hash([name, observation.observed_at, observation.values, observation.effective_at])
                                    from psycopg.types.json import Jsonb
                                    db.execute('''INSERT INTO runtime_research_observations
                                        (observation_id,instrument,observed_at,effective_at,first_seen_at,provider_latency,source,quality_status,payload)
                                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                                        (identifier, name, observation.observed_at, observation.effective_at, received, observation.source_latency,
                                         observation.provider, 'PASS' if observation.effective_at else 'SOURCE_LATENCY_UNKNOWN', Jsonb(payload)))
                            self.state = 'RUNNING'
                        except (ResearchProviderError,ValueError,TypeError,KeyError,OSError) as exc: self.state = getattr(exc,'state','PROVIDER_ERROR')
                else: self.state = 'MARKET_CLOSED'
            except Exception:
                self.state = 'UNAVAILABLE'
                logging.getLogger(__name__).warning('runtime_research_context_unavailable')
            self.stop_event.wait(interval)


def market_structure_readiness(database_url=None, root=None):
    root=Path(root or memory_root())
    now=datetime.now(UTC)
    try: snapshot=ConstituentUniverseService(root).resolve(now.astimezone(IST).date(),decision_at=now)
    except (ValueError,TypeError,KeyError,OSError): snapshot=None
    repository=MarketStructureContextRepository(database_url)
    provider=UpstoxExpiredFuturesProvider(root=root)
    constituents={'status':'PARTIAL' if snapshot else 'NOT_CONFIGURED',
        'membershipSnapshotStatus':snapshot.source_quality if snapshot else 'CONSTITUENT_UNIVERSE_UNAVAILABLE',
        'membershipSnapshotId':snapshot.snapshot_id if snapshot else None,
        'constituentCount':len(snapshot.members) if snapshot else 0,
        'historicalConstituentSessions':None,'historicalBreadthMinutes':None,'liveConstituentStatus':'UNAVAILABLE'}
    futures={'status':'PARTIAL','historicalFuturesStatus':'SUBSCRIPTION_REQUIRED' if not provider._settings.upstox_plus_enabled else provider.configuration_state(),
        'historicalFuturesSessions':None,'liveFuturesStatus':'UNAVAILABLE'}
    news_enabled=os.getenv('UPSTOX_ANALYTICS_ENABLED','false').lower()=='true' and os.getenv('NEWS_SOURCE_UPSTOX_INSTRUMENT_ENABLED','false').lower()=='true'
    news={'configured':bool(news_enabled and provider._settings.upstox_token and os.getenv('UPSTOX_BOD_INSTRUMENTS_FILE') and snapshot),
        'sourceState':'NOT_CONFIGURED' if news_enabled else 'DISABLED'}
    structure={'status':'PARTIAL','marketStructureVersion':MARKET_STRUCTURE_CONTEXT_VERSION,'CASRegimeAvailable':False}
    try:
        with repository.connection() as db:
            counts=db.execute('''SELECT count(DISTINCT session_date) FILTER(WHERE constituent_context->>'status'='PASS') AS sessions,
                count(DISTINCT (session_date,as_of)) FILTER(WHERE constituent_context->>'status'='PASS' AND
                    jsonb_typeof(constituent_context->'breadth_5m')='number') AS minutes,
                count(DISTINCT session_date) FILTER(WHERE futures_context->>'status'='PASS') AS futures
                FROM market_structure_context_minutes m JOIN market_structure_context_runs r USING(run_id)
                WHERE r.context_version=%s''',(MARKET_STRUCTURE_CONTEXT_VERSION,)).fetchone()
            constituents.update(historicalConstituentSessions=counts['sessions'],historicalBreadthMinutes=counts['minutes'])
            futures['historicalFuturesSessions']=counts['futures']
            latest=db.execute('SELECT source_manifest FROM market_structure_context_runs WHERE context_version=%s ORDER BY created_at DESC LIMIT 1',
                (MARKET_STRUCTURE_CONTEXT_VERSION,)).fetchone()
            if latest:
                futures['historicalFuturesStatus']=latest['source_manifest'].get('states',{}).get('futures','AVAILABLE' if counts['futures'] else futures['historicalFuturesStatus'])
            health=db.execute("SELECT state FROM market_news_source_health WHERE source='UPSTOX_INSTRUMENT'").fetchone()
            if news['configured'] and health: news['sourceState']=health['state']
            session=db.execute('SELECT session_date,market_open_utc,market_close_utc,status,source FROM trading_sessions WHERE session_date=%s',
                (now.astimezone(IST).date(),)).fetchone()
        structure.update(market_structure(now,session,root))
        runtime=MarketStructureContextReader(repository,root).runtime(now.replace(second=0,microsecond=0),now)
        constituents['liveConstituentStatus']=runtime.get('constituentContext',{}).get('status',runtime['status'])
        futures['liveFuturesStatus']=runtime.get('futuresContext',{}).get('status',runtime['status'])
    except Exception:
        constituents['storageStatus']='UNAVAILABLE'; futures['storageStatus']='UNAVAILABLE'
    return {'status':'PARTIAL','CONSTITUENT_CONTEXT':constituents,'FUTURES_CONTEXT':futures,
        'UPSTOX_INSTRUMENT_NEWS':news,'MARKET_STRUCTURE':structure,
        'coverageMeaning':'Sessions with valid observations; not a claim of complete session coverage'}
