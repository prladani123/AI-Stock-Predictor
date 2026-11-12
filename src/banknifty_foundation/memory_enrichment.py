from datetime import date, datetime, time, timedelta
from hashlib import sha256
import json
import logging
import os
from pathlib import Path
import time as clock
from typing import Protocol

from .calendar import IST
from .paths import memory_root
from .market_memory import FileMarketMemoryRepository
from .memory_builder import load_intraday, build_episodes, build_events
from .zerodha import create_read_only_zerodha_adapter

class HistoricalMinuteProvider(Protocol):
    def fetch(self, day: date) -> list[dict]: ...

class StoredMinuteProvider:
    def __init__(self,root=None):self.root=Path(root) if root else memory_root()
    def fetch(self,day):return load_intraday(self.root).get(day.isoformat(),[])

class ZerodhaHistoricalMinuteProvider:
    def __init__(self):
        self.adapter=create_read_only_zerodha_adapter(os.getenv('ZERODHA_API_KEY',''),os.getenv('ZERODHA_ACCESS_TOKEN',''),
            allow_real_api=os.getenv('ALLOW_REAL_ZERODHA','false').lower()=='true')
        matches=[row for row in self.adapter.instruments('NSE') if row.get('tradingsymbol') in {'NIFTY BANK','BANKNIFTY'}]
        if len(matches)!=1:raise ValueError('BANKNIFTY index identity not uniquely resolved')
        self.token=int(matches[0]['instrument_token'])
    def fetch(self,day):
        for attempt in range(3):
            clock.sleep(max(.34,1/max(.1,float(os.getenv('HISTORICAL_REQUESTS_PER_SECOND','3')))))
            try:
                rows=self.adapter.historical_data(self.token,datetime.combine(day,time(9,15),IST),datetime.combine(day,time(15,29),IST),oi=False)
                return [{**row,'at':row['date']} for row in rows]
            except ConnectionError:
                if attempt==2:raise
                clock.sleep(2**attempt)

def validate_minutes(rows,day):
    expected={datetime.combine(day,time(9,15),IST)+timedelta(minutes=i) for i in range(375)}
    stamps=[row['at'] for row in rows]
    valid=all(row['at'].tzinfo is not None and min(row[k] for k in ('open','high','low','close'))>0
        and row['low']<=min(row['open'],row['close'])<=max(row['open'],row['close'])<=row['high'] for row in rows)
    return {'complete':valid and len(stamps)==375 and set(stamps)==expected and stamps==sorted(stamps),
            'rows':len(rows),'missingMinutes':len(expected-set(stamps)),'duplicates':len(stamps)-len(set(stamps))}

def coverage(root=None):
    root=Path(root) if root else memory_root()
    repo=FileMarketMemoryRepository(root); days=repo.days(); rows=load_intraday(root); events=repo.events()
    complete=[item.trading_date.isoformat() for item in days if validate_minutes(rows.get(item.trading_date.isoformat(),[]),item.trading_date)['complete']]
    return {'dailyMemoryDays':len(days),'minuteDataDays':sum(bool(rows.get(item.trading_date.isoformat())) for item in days),
            'completeMinuteDays':len(complete),'numericalEpisodes':len(repo.episodes()),'structuredEvents':len(events),
            'reactionMeasurableEvents':sum(item.reaction.get('status') in {'REACTION_AVAILABLE','COMPLETE_30M'} for item in events),
            'completeReactionEvents':sum(all(item.reaction.get('returns',{}).get(str(h)) is not None for h in (5,10,20,30)) for item in events),
            'missingMinuteDates':[item.trading_date.isoformat() for item in reversed(days) if item.trading_date.isoformat() not in complete]}

def enrich(root=None,from_date=None,to_date=None,provider='stored',missing_only=True):
    root=Path(root) if root else memory_root(); before=coverage(root)
    if provider=='zerodha' and (os.getenv('ALLOW_REAL_ZERODHA','false').lower()!='true' or not os.getenv('ZERODHA_API_KEY') or not os.getenv('ZERODHA_ACCESS_TOKEN')):
        return {'status':'INTRADAY_ENRICHMENT_PENDING',**before}
    source=ZerodhaHistoricalMinuteProvider() if provider=='zerodha' else StoredMinuteProvider(root)
    results=[]
    for day in reversed(FileMarketMemoryRepository(root).days()):
        value=day.trading_date.isoformat()
        if from_date and value<from_date or to_date and value>to_date:continue
        if missing_only and value not in before['missingMinuteDates']:continue
        rows=source.fetch(day.trading_date); quality=validate_minutes(rows,day.trading_date)
        if rows and provider=='zerodha':
            payload=json.dumps({'provider':provider,'session_date':value,'quality':quality,'rows':rows},default=lambda x:x.isoformat(),sort_keys=True)
            target=root/'raw'/'index_history'/f'zerodha_{value}_{sha256(payload.encode()).hexdigest()[:16]}.json'
            if not target.exists():target.write_text(payload,encoding='utf-8')
        results.append({'date':value,**quality})
        logging.getLogger(__name__).info('memory_enrichment date=%s complete=%s',value,quality['complete'])
    build_episodes(root);build_events(root)
    return {'status':'COMPLETED','sessions':results,**coverage(root)}
