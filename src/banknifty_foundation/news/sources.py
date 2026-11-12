from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import os
import re
from typing import Any
from urllib.parse import urljoin
from urllib.request import Request, build_opener
import httpx
from bs4 import BeautifulSoup
from ..calendar import IST
import time
import xml.etree.ElementTree as ET

from .models import NewsArticle


class NewsSource(ABC):
    name: str

    @abstractmethod
    async def fetch(self, first_seen_at: datetime) -> list[NewsArticle]: ...


_client = httpx.Client(timeout=10, follow_redirects=True, headers={'User-Agent':'BANKNIFTY-Research/1.0'},
                       limits=httpx.Limits(max_connections=6,max_keepalive_connections=5))
_cooldown = {}

def _request(url: str, *, headers: dict[str, str] | None = None) -> bytes:
    if _cooldown.get(url,0)>time.monotonic():raise RuntimeError('SOURCE_COOLDOWN')
    for attempt in range(2):
        try:
            response=_client.get(url,headers=headers)
            if response.status_code in {403,429}:
                _cooldown[url]=time.monotonic()+900
            response.raise_for_status()
            return response.content
        except (httpx.TimeoutException,httpx.NetworkError):
            if attempt:raise
            time.sleep(1)
    raise RuntimeError('SOURCE_UNAVAILABLE')


def _published(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
        except ValueError:
            for pattern in ("%d-%b-%Y %H:%M", "%d-%b-%Y", "%d-%m-%Y %H:%M", "%d-%m-%Y"):
                try:
                    return datetime.strptime(value.strip(), pattern).replace(tzinfo=IST).astimezone(timezone.utc)
                except ValueError:
                    continue
            return None


class RSSNewsSource(NewsSource):
    def __init__(self, name: str, url: str) -> None:
        self.name, self.url = name, url

    async def fetch(self, first_seen_at: datetime) -> list[NewsArticle]:
        raw = await asyncio.to_thread(_request, self.url)
        root = ET.fromstring(raw)
        output = []
        for item in root.findall(".//item")[:80]:
            headline = (item.findtext("title") or "").strip()
            url = (item.findtext("link") or "").strip()
            if headline and url:
                output.append(NewsArticle(headline, self.name, url,
                    _published(item.findtext("pubDate") or item.findtext("published")),
                    first_seen_at, first_seen_at))
        return output


class NSEOfficialSource(NewsSource):
    name = "NSE_OFFICIAL"
    base = "https://www.nseindia.com"

    async def fetch(self, first_seen_at: datetime) -> list[NewsArticle]:
        def load() -> list[NewsArticle]:
            headers = {"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"}
            _request(self.base,headers=headers)
            time.sleep(.4)
            raw = _request(self.base + "/api/corporate-announcements?index=equities",
                headers={**headers, "Referer": self.base + "/companies-listing/corporate-filings-announcements"})
            payload = json.loads(raw)
            output = []
            for item in payload if isinstance(payload, list) else []:
                symbol = str(item.get("symbol") or "").strip()
                subject = str(item.get("subject") or item.get("desc") or "").strip()
                if subject:
                    output.append(NewsArticle(f"{symbol}: {subject}" if symbol else subject, self.name,
                        self.base + "/companies-listing/corporate-filings-announcements",
                        _published(item.get("an_dt")), first_seen_at, first_seen_at,
                        (symbol,) if symbol else (), raw_metadata={"nse": item}))
            return output
        return await asyncio.to_thread(load)


class RBIOfficialSource(NewsSource):
    name = "RBI_OFFICIAL"
    url = "https://website.rbi.org.in/web/rbi/notifications"

    async def fetch(self, first_seen_at: datetime) -> list[NewsArticle]:
        raw = (await asyncio.to_thread(_request, self.url)).decode("utf-8", "ignore")
        soup=BeautifulSoup(raw,'html.parser')
        for tag in soup(['script','style','nav','header','footer']):tag.decompose()
        links = [(tag.get('href',''),tag.get_text(' ',strip=True)) for tag in soup.select('a[href]')]
        output = []
        for href, html in links:
            headline = re.sub(r"<[^>]+>", " ", html)
            headline = re.sub(r"\s+", " ", headline).strip()
            lowered_href = href.lower()
            is_content_link = any(part in lowered_href for part in (
                "notification", "pressrelease", "press-release", "bs_pressreleasedisplay"
            ))
            looks_like_headline = 25 <= len(headline) <= 300 and not any(
                marker in headline.lower() for marker in ("{", "}", "function(", "javascript:", "select your preferred language")
            )
            if is_content_link and looks_like_headline and any(word in headline.lower() for word in ("policy", "bank", "rate", "inflation", "liquidity", "regulation")):
                output.append(NewsArticle(headline, self.name, urljoin(self.url, href), None,
                                          first_seen_at, first_seen_at))
        return output[:50]


class MoneycontrolSource(RSSNewsSource):
    def __init__(self):super().__init__('MONEYCONTROL','https://www.moneycontrol.com/rss/marketreports.xml')

class ETMarketsSource(RSSNewsSource):
    def __init__(self):super().__init__('ET_MARKETS','https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms')

class LiveMintSource(RSSNewsSource):
    def __init__(self):super().__init__('LIVEMINT','https://www.livemint.com/rss/markets')

class UpstoxInstrumentNewsSource(NewsSource):
    name = 'UPSTOX_INSTRUMENT'

    async def fetch(self, first_seen_at):
        def load():
            from ..market_structure_context import ConstituentUniverseService, HistoricalConstituentProvider
            from ..research_context import _http, _stamp, ResearchProviderError
            provider=HistoricalConstituentProvider()
            if os.getenv('NEWS_SOURCE_UPSTOX_INSTRUMENT_ENABLED','false').lower()!='true' or provider.configuration_state()!='READY_FOR_REQUEST':
                raise ResearchProviderError('DISABLED','UPSTOX_INSTRUMENT_NEWS_NOT_READY')
            now=datetime.now(timezone.utc)
            snapshot=ConstituentUniverseService().resolve(now.astimezone(IST).date(),decision_at=now)
            if snapshot is None: raise ResearchProviderError('NOT_CONFIGURED','CONSTITUENT_UNIVERSE_UNAVAILABLE')
            snapshot=provider.resolve_members(snapshot)
            keys={m.instrument_key:m for m in snapshot.members}
            if not 0<len(keys)<=30: raise ValueError('Instrument news requires 1 to 30 resolved keys')
            content=_http('GET','https://api.upstox.com/v2/news/instruments',
                {'Authorization':'Bearer '+provider._settings.upstox_token,'Accept':'application/json'},
                params={'instrument_key':','.join(sorted(keys))})
            received=datetime.now(timezone.utc)
            data=content.get('data',[])
            rows=data.get('news',[]) if isinstance(data,dict) else data
            if not isinstance(rows,list): raise ResearchProviderError('PROVIDER_ERROR','INSTRUMENT_NEWS_SCHEMA_INVALID')
            output=[]
            for row in rows:
                references=row.get('instrument_keys',[])
                if row.get('instrument_key'): references=[*references,row['instrument_key']]
                members=[keys[key] for key in references if key in keys]
                headline=row.get('title') or row.get('headline')
                url=row.get('url') or row.get('news_url')
                if not members or not headline or not isinstance(url,str) or not url.startswith('https://'): continue
                published=_stamp(row['published_time']) if row.get('published_time') else None
                output.append(NewsArticle(headline,self.name,url,published,received,received,
                    tuple(m.symbol for m in members),raw_metadata={'published_time':row.get('published_time'),
                        'scope':'CONSTITUENT_DIRECT','affectedSymbols':[m.symbol for m in members],
                        'constituentUniverseMember':True,'constituentSnapshotId':snapshot.snapshot_id,
                        'instrumentKeys':[m.instrument_key for m in members],
                        'verifiedWeights':{m.symbol:m.weight for m in members if m.weight_source_quality=='OFFICIAL_INDEX' and snapshot.source_quality=='OFFICIAL_INDEX'}}))
            return output
        return await asyncio.to_thread(load)


def configured_sources() -> list[NewsSource]:
    definitions: list[NewsSource] = [
        RBIOfficialSource(), NSEOfficialSource(), MoneycontrolSource(), ETMarketsSource(), LiveMintSource(),
    ]
    if os.getenv('UPSTOX_ANALYTICS_ENABLED','false').lower()=='true' and os.getenv('NEWS_SOURCE_UPSTOX_INSTRUMENT_ENABLED','false').lower()=='true':
        definitions.append(UpstoxInstrumentNewsSource())
    return [source for source in definitions if os.getenv(f"NEWS_SOURCE_{source.name}_ENABLED", "true").lower() == "true"]
