"""Guarded durable live-market lifecycle, using the existing collector and selectors."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import logging
from queue import Queue, Empty, Full
from threading import Event, RLock, Thread
import time

from .calendar import IST
from .config import Settings
from .instruments import InstrumentSnapshotService
from .live import ContractSelector, InstrumentTokenMap, LiveCollector
from .live_adapters import KiteTickerLiveAdapter
from .live_models import FinalizedMinute, LiveConnection, LiveQualityIssue
from .live_product_reader import LiveProductDataReader
from .live_repository import PostgresLiveRepository
from .repository import canonical_json
from .zerodha import create_read_only_zerodha_adapter

log=logging.getLogger(__name__)
UTC=timezone.utc


class LiveSessionCatchupService:
    """Index-only historical gap fill; never overwrite a ticker minute or its revisions."""
    def __init__(self,adapter,repository,settings,stop_event=None):
        self.adapter,self.repository,self.settings=adapter,repository,settings
        self.stop_event=stop_event or Event()

    def fill(self,index,session,startup_at):
        if index.instrument_type!='INDEX': raise ValueError('Session catch-up is BANKNIFTY index only')
        return self._fill_current_session(index,session,startup_at)

    def _fill_current_session(self,index,session,startup_at):
        if session.session_date != startup_at.astimezone(IST).date() or session.session_date != datetime.now(IST).date():
            raise ValueError('Catch-up is restricted to the current session')
        end=min(startup_at.replace(second=0,microsecond=0)-timedelta(minutes=1),session.close_at-timedelta(minutes=1))
        if end<session.open_at: return 0
        existing=self.repository.index_minute_keys(index.instrument_id,session)
        expected={session.open_at+timedelta(minutes=i) for i in range(int((end-session.open_at).total_seconds()/60)+1)}
        missing=expected-existing
        if not missing: return 0
        raw=None
        for attempt in range(self.settings.max_retries):
            if self.stop_event.wait(max(1/self.settings.requests_per_second, .35)): return 0
            try:
                raw=self.adapter.historical_data(index.instrument_token,min(missing),end,interval='minute',continuous=False,oi=False)
                break
            except ConnectionError:
                if attempt+1==self.settings.max_retries: raise
                if self.stop_event.wait(min(2**attempt,8)): return 0
        if raw is None: return 0
        # Validate the complete response before inserting any rows.
        parsed={}; previous=None
        for row in raw:
            stamp=row['date']
            if isinstance(stamp,str): stamp=datetime.fromisoformat(stamp)
            if stamp.tzinfo is None: raise ValueError('Naive session catch-up timestamp')
            stamp=stamp.astimezone(UTC)
            if stamp not in expected or stamp.second or stamp.microsecond: raise ValueError('Catch-up outside requested session minutes')
            if previous is not None and stamp<previous: raise ValueError('Out-of-order catch-up response')
            values=tuple(Decimal(str(row[k])) for k in ('open','high','low','close'))
            opening,high,low,close=values
            if not all(v.is_finite() for v in values) or not 0<low<=min(opening,close)<=max(opening,close)<=high:
                raise ValueError('Invalid session catch-up OHLC')
            volume=int(row['volume']) if row.get('volume') is not None else None
            if volume is not None and volume<0: raise ValueError('Negative catch-up volume')
            item=(values,volume)
            if stamp in parsed and parsed[stamp]!=item: raise ValueError('Conflicting catch-up candles')
            parsed[stamp]=item; previous=stamp
        inserted=0
        observed=datetime.now(UTC)
        for stamp,(values,volume) in sorted(parsed.items()):
            if self.stop_event.is_set(): break
            if stamp not in missing: continue
            checksum=sha256(canonical_json({'source':'SESSION_CATCHUP','at':stamp,'ohlc':values,'volume':volume}).encode()).hexdigest()
            identifier=sha256(f'catchup:{index.instrument_id}:{stamp.isoformat()}'.encode()).hexdigest()
            minute=FinalizedMinute('catchup-'+identifier,index.instrument_id,stamp,0,observed,*values,
                None,volume,None,None,None,None,None,(),('SESSION_CATCHUP','NO_TICK_OR_DEPTH_EVIDENCE'),'PASS',checksum)
            inserted+=int(self.repository.save_session_catchup(minute))
        for stamp in sorted(missing-parsed.keys()):
            identity=sha256(f'catchup-missing:{index.instrument_id}:{stamp.isoformat()}'.encode()).hexdigest()
            self.repository.save_live_issue(LiveQualityIssue(identity,'SESSION_CATCHUP_MISSING',observed,index.instrument_id,stamp,
                {'source':'historical_data','action':'NO_FILL'}))
        return inserted


class ConstituentSessionCatchupService(LiveSessionCatchupService):
    """At most the configured membership; no prior-session or arbitrary-equity fetches."""
    def fill(self, instrument, session, startup_at):
        from .market_structure_context import ConstituentUniverseService
        symbols = ConstituentUniverseService().symbols(session.session_date, decision_at=startup_at)
        if instrument.exchange != 'NSE' or instrument.instrument_type != 'EQ' or instrument.tradingsymbol not in symbols:
            raise ValueError('Catch-up requires current constituent membership')
        return self._fill_current_session(instrument,session,startup_at)


class LiveMarketWorker:
    def __init__(self,settings=None):
        self.settings=settings
        self.stop_event=Event()
        self.thread=None
        self.lock=RLock()
        self.state='DISABLED'
        self.message='Live collection is disabled'
        self.collector=None
        self.adapter=None
        self.repository=None
        self.started_at=None
        self.connection_id=None
        self.constituent_status='CONSTITUENT_CONTEXT_UNAVAILABLE'

    def status(self):
        with self.lock:
            collector=self.collector
            return {'state':self.state,'message':self.message,'connected':self.state=='CONNECTED',
                'collectorMode':'LIVE','constituentContextStatus':self.constituent_status,
                'lastTick':collector.connection.last_tick_at.isoformat() if collector and collector.connection.last_tick_at else None,
                'receivedTickCount':collector.stats['ticks'] if collector else 0,
                'desiredSubscriptions':sorted(collector.registry.desired) if collector else [],
                'activeSubscriptions':sorted(collector.registry.active) if collector else [],
                'startedAt':self.started_at.isoformat() if self.started_at else None}

    def _state(self,state,message=None,persist=True):
        with self.lock:
            self.state,self.message=state,message
        if persist and self.repository and self.connection_id:
            now=datetime.now(UTC)
            collector=self.collector
            self.repository.save_connection(LiveConnection(self.connection_id,state,now,
                collector.connection.connected_at if collector else None,
                now if state in ('STOPPED','MARKET_CLOSED','AUTH_NOT_CONFIGURED') else None,message,
                collector.connection.reconnect_attempt if collector else 0,
                collector.connection.last_tick_at if collector else None))

    def start(self):
        if self.thread and self.thread.is_alive(): return
        try: self.settings=self.settings or Settings.from_env(require_database=False)
        except (ValueError,TypeError):
            self._state('DEGRADED','Invalid live-market configuration',persist=False); return
        if not self.settings.live_market_enabled:
            self._state('DISABLED','Live collection is disabled',persist=False); return
        if not self.settings.allow_real_zerodha or not self.settings.zerodha_api_key or not self.settings.zerodha_access_token:
            self._state('AUTH_NOT_CONFIGURED','Guarded real access and credentials are required',persist=False); return
        if not self.settings.database_url:
            self._state('DEGRADED','DATABASE_URL is required',persist=False); return
        self.stop_event.clear()
        self.started_at=datetime.now(UTC)
        self.connection_id='live-worker-'+self.started_at.strftime('%Y%m%dT%H%M%S%fZ')
        self._state('STARTING',persist=False)
        self.thread=Thread(target=self._run,name='banknifty-live-market',daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread: self.thread.join()
        if self.state!='DISABLED': self._state('STOPPED',persist=False)

    def _run(self):
        self.repository=PostgresLiveRepository(self.settings.database_url)
        reader=LiveProductDataReader(self.settings.database_url)
        while not self.stop_event.is_set():
            try:
                with self.repository.worker_lease() as acquired:
                    if not acquired:
                        self._state('DEGRADED','Another live collector owns the database lease',persist=False)
                    else:
                        self._sessions(reader)
            except Exception as exc:
                log.exception('live_market_worker_failed')
                self._state('AUTH_NOT_CONFIGURED' if type(exc).__name__=='TokenException' else 'DEGRADED',type(exc).__name__,persist=False)
            self.stop_event.wait(30)

    def _sessions(self,reader):
        from .calendar_bootstrap import TradingCalendarBootstrapService
        calendar = TradingCalendarBootstrapService(self.settings.database_url)
        while not self.stop_event.is_set():
            now=datetime.now(UTC)
            readiness = calendar.status(now.astimezone(IST).date())
            if readiness['status'] != 'READY':
                self._state(readiness['status'], 'Resolve the stored, source-backed trading calendar')
                self.stop_event.wait(30)
                continue
            session=reader.session(now.astimezone(IST).date())
            if session is None:
                self._state('SESSION_NOT_CONFIGURED','Today has no stored exchange session')
            elif session.status not in ('OPEN','SPECIAL') or not session.open_at<=now<session.close_at:
                self._state('MARKET_CLOSED','Outside the explicitly stored open session')
            else:
                self._collect(session,reader)
            self.stop_event.wait(30)

    def _collect(self,session,reader):
        self._state('STARTING')
        settings=self.settings
        api_key,access_token=settings.require_live_credentials()
        rest=create_read_only_zerodha_adapter(api_key,access_token,allow_real_api=settings.allow_real_zerodha)
        now=datetime.now(UTC)
        instruments=reader.latest_instrument_snapshot(session.session_date)
        from .market_structure_context import ConstituentUniverseService
        member_symbols=ConstituentUniverseService().symbols(session.session_date,decision_at=now)
        if not instruments:
            InstrumentSnapshotService(rest,self.repository.base).capture(now)
            instruments=reader.latest_instrument_snapshot(session.session_date)
        if not instruments: raise ValueError('No complete current BANKNIFTY instrument snapshot')
        if set(member_symbols)-{i.tradingsymbol for i in instruments if i.instrument_type=='EQ'}:
            try:
                InstrumentSnapshotService(rest,self.repository.base).capture(now)
                instruments=reader.latest_instrument_snapshot(session.session_date) or instruments
            except Exception:
                log.warning('constituent_instrument_snapshot_unavailable')
        constituents=[item for item in instruments if item.instrument_type=='EQ' and item.tradingsymbol in member_symbols]
        self.constituent_status='SUBSCRIBING' if member_symbols and len(constituents)==len(member_symbols) else 'CONSTITUENT_CONTEXT_UNAVAILABLE'
        index=next(r for r in instruments if r.instrument_type=='INDEX')
        key=f'{index.exchange}:{index.tradingsymbol}'
        underlying=Decimal(str(rest.ltp(key)[key]['last_price']))
        if not underlying.is_finite() or underlying<=0: raise ValueError('Invalid BANKNIFTY LTP')
        selector=ContractSelector(instruments)
        future,_=selector.select_future(now)
        universe=selector.select_options(underlying,now,width=settings.live_option_strike_width)
        if universe.quality_state!='PASS': raise ValueError('Incomplete ATM option universe')
        self.repository.save_futures_selection(future)
        self.repository.save_option_universe(universe)
        events=Queue(maxsize=settings.live_queue_limit)
        overflow=Event()
        def enqueue(kind,value):
            try: events.put_nowait((kind,value,datetime.now(UTC)))
            except Full: overflow.set()
        self.adapter=KiteTickerLiveAdapter(api_key,access_token,
            lambda ticks:enqueue('ticks',ticks),lambda state,reason:enqueue('state',(state,reason)))
        self.collector=LiveCollector(self.adapter,self.repository,InstrumentTokenMap(instruments,instruments[0].snapshot_version),
            session,self.connection_id,stale_after_seconds=settings.live_stale_seconds,
            lateness_seconds=settings.live_lateness_seconds,queue_limit=settings.live_queue_limit)
        collector=self.collector
        collector.registry.generation=self.repository.latest_generation()
        collector.builder.set_universe(universe,instruments)
        # Existing completed evidence is authoritative across process restarts.
        collector.builder.finalized_keys.update((index.instrument_id,stamp) for stamp in self.repository.index_minute_keys(index.instrument_id,session))
        constituent_catchup=ConstituentSessionCatchupService(rest,self.repository,settings,self.stop_event)
        for member in constituents:
            if self.stop_event.is_set(): break
            try:
                constituent_catchup.fill(member,session,datetime.now(UTC))
                collector.builder.finalized_keys.update((member.instrument_id,stamp) for stamp in self.repository.index_minute_keys(member.instrument_id,session))
            except Exception:
                self.constituent_status='PARTIAL_CATCHUP'
                log.warning('constituent_catchup_unavailable symbol=%s',member.tradingsymbol)
        catchup=LiveSessionCatchupService(rest,self.repository,settings,self.stop_event)
        # Bound retries across a minute boundary while the initial REST catch-up is in flight.
        for _ in range(3):
            startup=datetime.now(UTC)
            catchup.fill(index,session,startup)
            if self.stop_event.is_set() or datetime.now(UTC).replace(second=0,microsecond=0)==startup.replace(second=0,microsecond=0): break
        collector.builder.finalized_keys.update((index.instrument_id,stamp) for stamp in self.repository.index_minute_keys(index.instrument_id,session))
        if self.stop_event.is_set() or datetime.now(UTC)>=session.close_at:
            self.adapter=None
            self._state('STOPPED' if self.stop_event.is_set() else 'MARKET_CLOSED')
            return
        last_rotation=time.monotonic(); last_heartbeat=0
        latest_price=None; latest_at=None
        try:
            self.adapter.connect()
            while not self.stop_event.is_set():
                now=datetime.now(UTC)
                if now>=session.close_at+timedelta(seconds=settings.live_lateness_seconds+1):
                    collector.finalize(now); self._state('MARKET_CLOSED'); break
                try: kind,value,observed=events.get(timeout=.5)
                except Empty: kind=None
                if kind=='state':
                    state,reason=value
                    collector.on_state(state,reason,at=observed)
                    if state=='CONNECTED' and not collector.registry.desired:
                        collector.registry.replace([index.instrument_token,future.instrument_token,*universe.tokens,*[m.instrument_token for m in constituents]],observed)
                    self._state('AUTH_NOT_CONFIGURED' if state=='AUTH_EXPIRED' else 'STARTING' if state=='CONNECTING' else state,reason)
                    if state=='AUTH_EXPIRED': break
                elif kind=='ticks':
                    collector.on_ticks(value,received_at=observed)
                    if collector.connection.state=='DEGRADED' and not collector.connection.open_gap and not collector.registry.awaiting_fresh:
                        collector.on_state('CONNECTED','Fresh subscribed data restored',at=observed)
                        self._state('CONNECTED')
                if overflow.is_set():
                    overflow.clear()
                    collector.on_state('DEGRADED','Bounded ticker event queue overflow',at=now)
                    self._state('DEGRADED','Ticker queue overflow; affected minutes are not trusted')
                minutes,_=collector.finalize(now)
                for minute in minutes:
                    if minute.instrument_id==index.instrument_id and minute.quality_state=='PASS':
                        latest_price,latest_at=minute.close,minute.minute_at
                if time.monotonic()-last_rotation>=settings.live_universe_refresh_seconds:
                    next_future,_=selector.select_future(now)
                    changed_future=next_future.instrument_id!=future.instrument_id
                    next_universe=selector.select_options(latest_price,now,width=settings.live_option_strike_width) if latest_price and latest_at and now-latest_at<timedelta(minutes=2) else universe
                    changed_universe=(next_universe.expiry,next_universe.atm_strike,next_universe.tokens)!=(universe.expiry,universe.atm_strike,universe.tokens)
                    if changed_universe and next_universe.quality_state!='PASS':
                        self._state('DEGRADED','Proposed option universe is incomplete'); changed_universe=False
                    if changed_future:
                        self.repository.save_futures_selection(next_future); future=next_future
                    if changed_universe:
                        self.repository.save_option_universe(next_universe)
                        collector.builder.set_universe(next_universe,instruments); universe=next_universe
                    if changed_future or changed_universe:
                        collector.registry.replace([index.instrument_token,future.instrument_token,*universe.tokens,*[m.instrument_token for m in constituents]],now)
                    last_rotation=time.monotonic()
                if time.monotonic()-last_heartbeat>=30:
                    if member_symbols and len(constituents)==len(member_symbols) and all(m.instrument_token in collector.registry.active for m in constituents):
                        self.constituent_status='SUBSCRIBED' if self.constituent_status!='PARTIAL_CATCHUP' else self.constituent_status
                    stale=collector.connection.last_tick_at is None or (now-collector.connection.last_tick_at).total_seconds()>settings.live_stale_seconds
                    state='DEGRADED' if stale or collector.registry.awaiting_fresh or collector.connection.open_gap else collector.connection.state
                    self._state(state,'Awaiting fresh market data' if state=='DEGRADED' else None)
                    last_heartbeat=time.monotonic()
        finally:
            self.adapter.close()
            collector.on_state('STOPPED','Live ticker lifecycle ended',at=datetime.now(UTC))
            if self.stop_event.is_set(): self._state('STOPPED')
            self.adapter=None
