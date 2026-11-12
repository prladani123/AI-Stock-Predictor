from __future__ import annotations

from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import wraps
import logging
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import os
from threading import RLock
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse

from .calendar import IST
from .live_models import FinalizedMinute, LiveInstrument, OptionMinuteSnapshot
from .live_simulation import run_phase8_simulation, simulation_instruments
from .big_move_features import VIX_DATA_STATUS, build_big_move_features, classify_futures_buildup
from .kronos_experiment import KronosForecastAdapter
from .models import TradingSession
from .product_queries import ProductDataReader
from .market_memory import FileMarketMemoryRepository, SimilarityEngine, public_day
from .news.service import NewsIntelligenceService, NewsPollingService
from .live_product_reader import LiveProductDataReader, CurrentMarketState
from .big_move_runtime import BigMoveRuntimeWorker, live_features
from .live_market_worker import LiveMarketWorker


HORIZONS = (5, 10, 15, 20)
APP_VERSION = "0.7.0"
_market_snapshot = ContextVar('product_market_snapshot', default=None)


def market_view(method):
    """Nested endpoint helpers share one repeatable market snapshot, never global cached data."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            if _market_snapshot.get() is not None:
                return method(self,*args,**kwargs)
            state=CurrentMarketState()
            if not self.running and self.live_reader.database_url:
                try: state=self.live_reader.current_state()
                except Exception:
                    logging.getLogger(__name__).exception('live_product_read_failed')
                    state.quality='DATABASE_UNAVAILABLE'
            token=_market_snapshot.set(state)
            try: return method(self,*args,**kwargs)
            finally: _market_snapshot.reset(token)
    return wrapped


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _time_machine_availability() -> dict[str, Any]:
    enabled = os.getenv("TIME_MACHINE_ENABLED", "false").strip().lower() == "true"
    replay_pool = os.getenv("TIME_MACHINE_REPLAY_POOL_REVISION_ID")
    status = "READY" if enabled and replay_pool else ("DISABLED" if not enabled
                                                       else "REPLAY_POOL_NOT_CONFIGURED")
    reason = (None if status == "READY" else
              "TIME_MACHINE_DISABLED" if not enabled else "REPLAY_POOL_NOT_CONFIGURED")
    return {"available": status == "READY", "status": status, "reason": reason,
            "replayPoolRevisionId": replay_pool,
            "model": "BANKNIFTY_TIME_MACHINE_V1", "researchOnly": True,
            "usedByPriceCoreV2": False}


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


class ProductRuntime:
    """Small state holder for development simulation and read-only API views."""

    def __init__(self) -> None:
        self._lock = RLock()
        self.repository = None
        self.live_reader = LiveProductDataReader()
        self.live_worker = LiveMarketWorker()
        self.report: dict[str, Any] | None = None
        self.running = False
        self.started_at: str | None = None
        self.stopped_at: str | None = None
        self.news = NewsIntelligenceService()
        self.instruments: dict[int, LiveInstrument] = {
            item.instrument_id: item for item in simulation_instruments()
        }

    def start(self) -> dict[str, Any]:
        with self._lock:
            if not self.running:
                self.repository, self.report = run_phase8_simulation()
                self.running = True
                self.started_at = _now()
                self.stopped_at = None
            return self.live_status()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self.running = False
            self.stopped_at = _now()
            return self.live_status()

    def _minutes(self, instrument_type: str | None = None) -> list[FinalizedMinute]:
        source = self._source()
        if source is None:
            return []
        latest = {}
        for value in source.finalized_minutes.values():
            key=(value.instrument_id,value.minute_at)
            if key not in latest or value.revision>latest[key].revision: latest[key]=value
        values = [value for value in latest.values() if value.quality_state=='PASS']
        if instrument_type:
            values = [
                value for value in values
                if self._instrument_map()[value.instrument_id].instrument_type == instrument_type
            ]
        return sorted(values, key=lambda item: (item.minute_at, item.instrument_id))

    def _source(self):
        return self.repository if self.running else _market_snapshot.get() if self.live_reader.database_url else None

    def _instrument_map(self):
        return self.instruments if self.running else (self._source().instruments if self._source() else {})

    @market_view
    def candles(self) -> list[dict[str, Any]]:
        return [
            {
                "instrument": self._instrument_map()[item.instrument_id].tradingsymbol,
                "timestamp": item.minute_at.isoformat(),
                "open": float(item.open), "high": float(item.high),
                "low": float(item.low), "close": float(item.close),
                "volume": item.volume_delta, "qualityStatus": item.quality_state,
            }
            for item in self._minutes("INDEX")
        ]

    @staticmethod
    def unavailable_predictions() -> list[dict[str, Any]]:
        return [
            {
                "available": False, "horizonMinutes": horizon, "direction": None,
                "rawClass": None, "confidence": None, "model": None, "asOf": None,
                "expiresAt": None, "abstentionReason": "NO_APPROVED_LIVE_MODEL",
                "actualOutcome": None, "resolved": False, "id": None,
                "rawProbabilities": None, "calibratedProbabilities": None,
                "systemAbstained": True, "abstentionReasons": ["NO_APPROVED_LIVE_MODEL"],
                "algorithm": None, "modelVersion": None, "modelChecksum": None,
                "calibrationMethod": None, "calibrationVersion": None,
                "reliabilityBucket": None, "createdAt": None, "qualityStatus": None,
            }
            for horizon in HORIZONS
        ]

    @market_view
    def live_status(self) -> dict[str, Any]:
        if not self.running and self.live_reader.database_url:
            state=self._source()
            result=state.status or {'connected':False,'state':'UNAVAILABLE','collectorMode':'LIVE',
                'credentialsConfigured':bool(os.getenv('ZERODHA_ACCESS_TOKEN')),'message':state.quality,
                'lastTick':None,'lastCompletedCandle':None,'receivedTickCount':None,'reconnectCount':0,
                'gapCount':0,'activeGapCount':0,'desiredSubscriptions':[],'activeSubscriptions':[],
                'startedAt':None,'stoppedAt':None,'recentQualityWarning':None}
            worker=self.live_worker.status()
            if worker['state']!='DISABLED':
                result={**result,**worker,'connected':worker['connected'] and result.get('connected',False)}
            elif result['state'] in ('STOPPED','UNAVAILABLE'): result['state']='DISABLED'
            return result
        repo = self.repository if self.running else None
        minutes = self._minutes()
        ticks = list(repo.raw_ticks.values()) if repo else []
        subscriptions = list(repo.subscription_generations.values()) if repo else []
        latest = max(subscriptions, key=lambda item: item.generation) if subscriptions else None
        gaps = list(repo.gaps.values()) if repo else []
        return _json_value({
            "connected": self.running, "state": "SIMULATION" if self.running else self.live_worker.status()['state'],
            "collectorMode": "SIMULATED" if repo else "OFFLINE",
            "credentialsConfigured": bool(os.getenv("ZERODHA_API_KEY") and os.getenv("ZERODHA_ACCESS_TOKEN")),
            "message": None if self.running else self.live_worker.status()['message'],
            "lastTick": max((item.received_at for item in ticks), default=None),
            "lastCompletedCandle": max((item.minute_at for item in minutes), default=None),
            "receivedTickCount": len(ticks),
            "reconnectCount": int(self.report.get("reconnect_count", 0)) if self.report else 0,
            "gapCount": len(gaps), "activeGapCount": sum(item.state != "RESOLVED" for item in gaps),
            "desiredSubscriptions": list(latest.desired_tokens) if latest else [],
            "activeSubscriptions": list(latest.active_tokens) if latest else [],
            "startedAt": self.started_at, "stoppedAt": self.stopped_at,
            "recentQualityWarning": max(repo.live_issues.values(), key=lambda item: item.detected_at).code if repo and repo.live_issues else None,
        })

    @market_view
    def dashboard(self) -> dict[str, Any]:
        candles = self.candles()
        latest = candles[-1] if candles else None
        prior = candles[-2] if len(candles) > 1 else None
        quality = self.quality()
        options = self.options()
        live = self.live_status()
        return {
            "instrument": "BANKNIFTY", "available": latest is not None,
            "lastPrice": latest["close"] if latest else None,
            "priceChange": latest["close"] - prior["close"] if latest and prior else None,
            "priceChangePercent": ((latest["close"] / prior["close"] - 1) * 100) if latest and prior else None,
            "latestCandle": latest,
            "marketState": "SIMULATION" if self.running else ("OPEN" if self._source().quality=='PASS' else self._source().quality) if self._source() else "OFFLINE",
            "dataMode": "SIMULATED" if self.running else "LIVE" if self.live_reader.database_url else "OFFLINE",
            "zerodhaStatus": "CONNECTED" if live.get('connected') and not self.running else "CONFIGURED_NOT_CONNECTED" if os.getenv("ZERODHA_ACCESS_TOKEN") else "NOT_CONFIGURED",
            "pythonStatus": "UP", "future": self.futures(),
            "selectedOptionExpiry": options.get("expiry"),
            "unresolvedQualityWarnings": quality["unresolvedCount"], "live": live, "timestamp": _now(),
            "intelligence": self.intelligence(),
        }

    @staticmethod
    def unavailable_intelligence_horizons() -> list[dict[str, Any]]:
        return [{"id": None, "available": False, "minutes": horizon,
                 "bigMoveProbability": None, "quietProbability": None, "state": "WAIT",
                 "direction": None, "directionProbability": None,
                 "upGivenBigMoveProbability": None, "downGivenBigMoveProbability": None,
                 "expectedMovePoints": None, "expectedMovePercent": None,
                 "modelVersion": None, "qualityStatus": "MODEL_UNAVAILABLE",
                 "actionState": "DATA_NOT_READY", "moveAlgorithm": None,
                 "directionAlgorithm": None, "referencePrice": None,
                 "actualResult": None, "resolved": False,
                 "asOf": None, "expiresAt": None} for horizon in (10, 20, 30)]

    @staticmethod
    def _number(value: Any) -> float | None:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def memory_similarity(self, state: dict[str, Any], as_of: datetime | str | None = None) -> dict[str, Any]:
        normalized = dict(state)
        normalized["regime"] = state.get("market_regime") or state.get("regime")
        if as_of is not None:
            normalized["_as_of"] = as_of
        return SimilarityEngine().search(normalized, FileMarketMemoryRepository().episodes())

    @staticmethod
    def _memory_summary(result: dict[str, Any]) -> dict[str, Any]:
        historical = result.get("historicalOutcome", {})
        up, down = historical.get("up30Rate"), historical.get("down30Rate")
        direction = "UP" if up is not None and (down is None or up > down) else "DOWN" if down is not None else None
        rate = max(value for value in (up, down) if value is not None) if any(value is not None for value in (up, down)) else None
        return {"available": result.get("available", False), "reason": result.get("reason"),
                "matchedEpisodes": result.get("matchedEpisodes", 0),
                "similarityStrength": result.get("similarityStrength", "UNAVAILABLE"),
                "bigMove30HistoricalRate": historical.get("bigMove30Rate"),
                "directionBias": direction, "directionRate": rate,
                "historicalOutcome": historical, "topMatches": result.get("topMatches", [])}

    @staticmethod
    def _news_summary(result: dict[str, Any]) -> dict[str, Any]:
        if not result.get("available"):
            return {"available": False, "status": result.get("status", result.get("reason", "UNAVAILABLE")),
                    "monitoringWindow": result.get("monitoringWindow")}
        event = result.get("event") or {}
        memory = result.get("historicalMemory") or {}
        reaction = (memory.get("historicalReaction") or {}).get("30") or {}
        up, down = reaction.get("upRate"), reaction.get("downRate")
        return {"available": True, "status": "AVAILABLE", "monitoringWindow": result.get("monitoringWindow"),
                "latestHighImpactEvent": {"eventId": event.get("event_id"), "category": event.get("event_category"),
                    "headline": event.get("headline_or_summary"), "firstSeenAt": event.get("first_seen_at"),
                    "source": event.get("source_name"), "url": event.get("source_url"),
                    "relevance": event.get("relevance")},
                "historicalMatches": memory.get("matchedEvents", 0),
                "similarityStrength": memory.get("eventSimilarityStrength", "UNAVAILABLE"),
                "historicalBigMove30Rate": reaction.get("bigMoveRate"),
                "historicalDirectionBias": "UP" if up is not None and (down is None or up > down) else "DOWN" if down is not None else None,
                "historicalDirectionRate": max(value for value in (up, down) if value is not None) if any(value is not None for value in (up, down)) else None,
                "marketConfirmation": (result.get("marketConfirmation") or {}).get("state", "INSUFFICIENT_DATA"),
                "confirmationDrivers": (result.get("marketConfirmation") or {}).get("drivers", [])}

    @staticmethod
    def memory_days() -> dict[str, Any]:
        days = FileMarketMemoryRepository().days()
        return {"available": bool(days), "count": len(days), "days": [public_day(item) for item in days]}

    @staticmethod
    def memory_day(value: date) -> dict[str, Any]:
        repository = FileMarketMemoryRepository()
        item = repository.day(value)
        return ({"available": True, "day": public_day(item), "markdown": repository.day_markdown(value)}
                if item else {"available": False, "reason": "MEMORY_DAY_NOT_FOUND"})

    def _intelligence_context(
        self, values: dict[str, Any], index: list[FinalizedMinute],
        reference_price: float | None,
    ) -> dict[str, Any]:
        price = float(index[-1].close) if index else reference_price
        ret5 = self._number(values.get("return_5m"))
        vwap_distance = self._number(values.get("session_vwap_distance"))
        vwap = price / (1 + vwap_distance) if price is not None and vwap_distance is not None else None
        trend = "UP" if ret5 is not None and ret5 > 0 else "DOWN" if ret5 is not None and ret5 < 0 else "FLAT" if ret5 == 0 else "UNAVAILABLE"
        vwap_relationship = "ABOVE" if vwap_distance is not None and vwap_distance > 0 else "BELOW" if vwap_distance is not None and vwap_distance < 0 else "AT" if vwap_distance == 0 else "UNAVAILABLE"
        session_high = float(max(item.high for item in index)) if index else None
        session_low = float(min(item.low for item in index)) if index else None
        opening = index[:15]
        opening_high = float(max(item.high for item in opening)) if opening else None
        opening_low = float(min(item.low for item in opening)) if opening else None
        future = self.futures()
        option = self.options()
        call_wall = self._number(values.get("call_oi_wall"))
        put_wall = self._number(values.get("put_oi_wall"))
        banknifty = {
            "price": price, "vwap": vwap, "vwapRelationship": vwap_relationship,
            "sessionHigh": session_high, "sessionLow": session_low,
            "openingHigh": opening_high, "openingLow": opening_low,
        }
        evidence = {
            "price": {
                "shortTrend": trend, "return5m": ret5,
                "return10m": self._number(values.get("return_10m")),
                "vwapRelationship": vwap_relationship,
                "vwapDistancePercent": vwap_distance * 100 if vwap_distance is not None else None,
                "rangeExpansion": self._number(values.get("range_expansion_ratio")),
                "breakoutHighDistancePercent": self._number(values.get("breakout_high_distance_20m")),
                "breakoutLowDistancePercent": self._number(values.get("breakout_low_distance_20m")),
            },
            "futures": {
                "available": bool(future.get("available")), "price": future.get("ltp"),
                "premium": self._number(values.get("futures_index_premium")),
                "premiumPercent": (self._number(values.get("futures_index_premium_pct")) * 100
                                   if self._number(values.get("futures_index_premium_pct")) is not None else None),
                "oi": values.get("futures_oi"),
                "oiChange1m": values.get("futures_oi_change_1m"),
                "oiChange5m": values.get("futures_oi_change_5m"),
                "volume": values.get("futures_volume_delta"),
                "buildup": values.get("futures_buildup", future.get("buildup", "NEUTRAL")),
            },
            "options": {
                "available": bool(values.get("option_data_available", option.get("available"))),
                "pcrOi": self._number(values.get("pcr_oi", option.get("pcrOi"))),
                "totalCeOi": values.get("total_ce_oi", option.get("totalCeOi")),
                "totalPeOi": values.get("total_pe_oi", option.get("totalPeOi")),
                "totalCeOiChange": values.get("total_ce_oi_change", option.get("totalCeOiChange")),
                "totalPeOiChange": values.get("total_pe_oi_change", option.get("totalPeOiChange")),
                "oiChangeImbalance": self._number(values.get("oi_change_imbalance")),
                "atmStraddle": self._number(values.get("atm_straddle_premium", option.get("atmStraddle"))),
                "strongCeOi": call_wall or option.get("strongCeOi"),
                "strongPeOi": put_wall or option.get("strongPeOi"),
                "completeness": self._number(values.get("selected_universe_completeness")),
                "ivStatus": values.get("iv_data_status", "IV_DATA_PENDING"),
            },
            "volatility": {
                "realized5m": self._number(values.get("realized_volatility_5m")),
                "realized10m": self._number(values.get("realized_volatility_10m")),
                "realized20m": self._number(values.get("realized_volatility_20m")),
                "atrNormalized": self._number(values.get("atr_normalized_14m")),
                "vixStatus": values.get("vix_data_status", VIX_DATA_STATUS),
            },
        }
        levels = {
            "current": price, "vwap": vwap, "atm": option.get("atmStrike"),
            "support": put_wall or option.get("strongPeOi"),
            "resistance": call_wall or option.get("strongCeOi"),
            "strongCeOi": call_wall or option.get("strongCeOi"),
            "strongPeOi": put_wall or option.get("strongPeOi"),
            "openingHigh": opening_high, "openingLow": opening_low,
            "sessionHigh": session_high, "sessionLow": session_low,
        }
        return {"banknifty": banknifty, "evidence": evidence, "levels": levels}

    def _feature_snapshot(self):
        source=self._source()
        if source is None: return None
        if self.running:
            index=self._minutes('INDEX')
            if not index: return None
            opening=min(item.minute_at for item in index)
            state=CurrentMarketState(
                session=TradingSession(opening.astimezone(IST).date(),opening,opening+timedelta(hours=6,minutes=15)),
                instruments=self.instruments,finalized_minutes=source.finalized_minutes,
                option_snapshots=source.option_snapshots,option_universes=source.option_universes,
                futures_selections=source.futures_selections,quality='PASS')
        else:
            state=source
            if state.quality!='PASS': return None
        try: return live_features(state)
        except ValueError: return None

    @market_view
    def current_feature_values(self):
        snapshot=self._feature_snapshot()
        return {k:float(v) if isinstance(v,Decimal) else v for k,v in snapshot.values.items()} if snapshot else {}

    @market_view
    def current_similarity(self):
        snapshot=self._feature_snapshot()
        return self.memory_similarity(snapshot.values if snapshot else {},snapshot.as_of if snapshot else None)

    @market_view
    def intelligence(self) -> dict[str, Any]:
        index=self._minutes('INDEX')
        snapshot=self._feature_snapshot()
        as_of=index[-1].minute_at if index else None
        stored=(ProductDataReader().current_intelligence(as_of=as_of,instrument_id=index[-1].instrument_id)
                if index and snapshot and not self.running else {})
        usable=snapshot is not None and snapshot.quality_status=='PASS'
        by_horizon={item['minutes']:item for item in stored.get('horizons',[]) if usable}
        horizons=[by_horizon.get(item['minutes'],item) for item in self.unavailable_intelligence_horizons()]
        values=snapshot.values if snapshot else {}
        context=self._intelligence_context(values,index,float(index[-1].close) if index else None)
        bias=snapshot.session_bias if snapshot else {'direction':'NEUTRAL','confidence':0.0,'drivers':[]}
        return _json_value({'available':snapshot is not None,'reason':None if usable else 'DATA_NOT_READY',
            'asOf':as_of.isoformat() if as_of else None,'marketRegime':snapshot.market_regime if snapshot else 'UNKNOWN',
            'sessionBias':bias,'qualityStatus':snapshot.quality_status if snapshot else 'DATA_NOT_READY',
            'horizons':horizons,'featureSetVersion':'banknifty-bigmove-v2','vixStatus':VIX_DATA_STATUS,
            'why':bias['drivers'],**context,
            'memory':{'numerical':self._memory_summary(self.memory_similarity(values,as_of)),
                      'news':self._news_summary(self.news.current_memory(values))},
            'kronosExperimental':stored.get('kronosExperimental',KronosForecastAdapter().availability()),
            'timeMachineExperimental':stored.get('timeMachineExperimental', _time_machine_availability())})

    @market_view
    def options(self, expiry_value: str | None = None) -> dict[str, Any]:
        source=self._source()
        if source is None or not source.option_snapshots or not self._minutes('INDEX'):
            return {"available": False, "reason": "NO_LIVE_DATA", "expiry": None, "rows": []}
        snapshots: list[OptionMinuteSnapshot] = list(source.option_snapshots.values())
        eligible = sorted({item.expiry for item in snapshots})
        try:
            expiry = date.fromisoformat(expiry_value) if expiry_value else max(source.option_universes.values(),key=lambda item:item.selected_at).expiry
        except ValueError:
            return {"available": False, "reason": "INVALID_EXPIRY", "expiry": None,
                    "eligibleExpiries": [value.isoformat() for value in eligible], "rows": []}
        selected = [item for item in snapshots if item.expiry == expiry]
        if not selected:
            return {"available": False, "reason": "EXPIRY_NOT_STORED", "expiry": expiry.isoformat(),
                    "eligibleExpiries": [value.isoformat() for value in eligible], "rows": []}
        latest_minute = max(item.minute_at for item in selected)
        latest = [item for item in selected if item.minute_at == latest_minute]
        universe = max((u for u in source.option_universes.values() if u.expiry==expiry), key=lambda item: item.selected_at)
        latest = [item for item in latest if item.universe_id==universe.universe_id]
        index = self._minutes("INDEX")[-1]
        by_strike: dict[Decimal, dict[str, Any]] = {}
        for item in latest:
            row = by_strike.setdefault(item.strike, {"strike": float(item.strike), "atm": item.atm_offset == 0})
            row[item.option_type.lower()] = {
                "ltp": float(item.last_price), "volume": item.volume_delta,
                "oi": item.oi, "oiChange": item.oi_change,
                "bid": float(item.best_bid_price) if item.best_bid_price is not None else None,
                "ask": float(item.best_ask_price) if item.best_ask_price is not None else None,
                "spread": float(item.spread) if item.spread is not None else None,
                "qualityStatus": item.quality_state,
            }
        rows = [by_strike[key] for key in sorted(by_strike)]
        calls = [(row["strike"], (row.get("ce") or {}).get("oi") or 0) for row in rows]
        puts = [(row["strike"], (row.get("pe") or {}).get("oi") or 0) for row in rows]
        total_ce_oi = sum(value for _, value in calls)
        total_pe_oi = sum(value for _, value in puts)
        total_ce_change = sum((row.get("ce") or {}).get("oiChange") or 0 for row in rows)
        total_pe_change = sum((row.get("pe") or {}).get("oiChange") or 0 for row in rows)
        strongest_ce = max(calls, key=lambda item: item[1])[0] if calls and any(value for _, value in calls) else None
        strongest_pe = max(puts, key=lambda item: item[1])[0] if puts and any(value for _, value in puts) else None
        atm_row = next((row for row in rows if row["atm"]), None)
        atm_ce = (atm_row.get("ce") or {}).get("ltp") if atm_row else None
        atm_pe = (atm_row.get("pe") or {}).get("ltp") if atm_row else None
        atm_straddle = atm_ce + atm_pe if atm_ce is not None and atm_pe is not None else None
        return {"available": True, "reason": None, "expiry": expiry.isoformat(),
                "eligibleExpiries": [value.isoformat() for value in eligible],
                "underlying": float(index.close), "atmStrike": float(universe.atm_strike),
                "strikeRange": universe.strike_width, "freshnessStatus": "STALE" if (not self.running and source.quality!='PASS') or latest_minute!=index.minute_at or any(
                    (side or {}).get("qualityStatus") != "PASS" for row in rows for side in (row.get("ce"), row.get("pe"))
                ) else "FRESH",
                "asOf": latest_minute.isoformat(), "pcrOi": total_pe_oi / total_ce_oi if total_ce_oi else None,
                "totalCeOi": total_ce_oi, "totalPeOi": total_pe_oi,
                "totalCeOiChange": total_ce_change, "totalPeOiChange": total_pe_change,
                "atmStraddle": atm_straddle, "strongCeOi": strongest_ce,
                "strongPeOi": strongest_pe, "rows": rows}

    @market_view
    def futures(self) -> dict[str, Any]:
        source=self._source()
        if source is None or not source.futures_selections:
            return {"available": False, "reason": "NO_LIVE_DATA"}
        selection = max(source.futures_selections.values(), key=lambda item: item.selected_at)
        future_minutes = [item for item in self._minutes("FUT") if item.instrument_id == selection.instrument_id]
        index_minutes = self._minutes("INDEX")
        future = future_minutes[-1] if future_minutes else None
        index = index_minutes[-1] if index_minutes else None
        premium = future.close - index.close if future and index and future.minute_at==index.minute_at else None
        previous_candidate = future_minutes[-2] if len(future_minutes) > 1 else None
        previous = previous_candidate if future and previous_candidate and future.minute_at - previous_candidate.minute_at == timedelta(minutes=1) else None
        price_change = future.close / previous.close - 1 if future and previous else None
        oi_delta = Decimal(future.oi_close - previous.oi_close) if future and previous and future.oi_close is not None and previous.oi_close is not None else None
        return {
            "available": future is not None, "reason": None if future else "NO_COMPLETED_FUTURES_CANDLE",
            "contract": self._instrument_map()[selection.instrument_id].tradingsymbol,
            "expiry": selection.expiry.isoformat(), "ltp": float(future.close) if future else None,
            "indexPrice": float(index.close) if index else None,
            "premium": float(premium) if premium is not None else None,
            "premiumPercent": float(premium / index.close * 100) if premium is not None and index else None,
            "oi": future.oi_close if future else None, "oiChange": future.oi_change if future else None,
            "volume": future.volume_delta if future else None, "rollover": selection.rollover,
            "buildup": classify_futures_buildup(price_change, oi_delta),
            "asOf": future.minute_at.isoformat() if future else None,
        }

    def models(self) -> dict[str, Any]:
        return ProductDataReader().model_status()

    @market_view
    def quality(self) -> dict[str, Any]:
        source=self._source()
        if source is None:
            return {"available": False, "reason": "NO_LIVE_DATA", "unresolvedCount": 0, "issues": []}
        issues = [{"id": item.issue_id,
                   "severity": "ERROR" if item.code in {"INVALID_PRICE", "OUTSIDE_SESSION"} else "WARNING",
                   "code": item.code, "detectedAt": item.detected_at.isoformat(),
                   "instrumentId": item.instrument_id,
                   "minuteAt": item.minute_at.isoformat() if item.minute_at else None,
                   "resolved": False, "details": _json_value(item.details)}
                  for item in source.live_issues.values()]
        return {"available": True, "reason": None, "unresolvedCount": len(issues), "issues": issues}


runtime = ProductRuntime()
news_poller = NewsPollingService(runtime.news)
from .daily_rollup import DailyRollupScheduler
rollup_scheduler = DailyRollupScheduler()
big_move_worker = BigMoveRuntimeWorker()
live_market_worker = runtime.live_worker
from .market_structure_context import RuntimeResearchContextPoller
research_context_poller = RuntimeResearchContextPoller()


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        research_context_poller.start()
        if os.getenv('LIVE_MARKET_ENABLED','false').lower() == 'true':
            live_market_worker.start()
        if os.getenv('BIG_MOVE_RUNTIME_ENABLED','false').lower() == 'true':
            big_move_worker.start()
        if os.getenv('MEMORY_ROLLUP_ENABLED','false').lower() == 'true':
            rollup_scheduler.start()
        if os.getenv("NEWS_POLLING_ENABLED", "false").lower() == "true":
            news_poller.start()
        yield
    finally:
        research_context_poller.stop()
        live_market_worker.stop()
        big_move_worker.stop()
        rollup_scheduler.stop()
        news_poller.stop()


app = FastAPI(title="BANKNIFTY Python Internal API", version=APP_VERSION,
              docs_url=None, redoc_url=None, openapi_url="/internal/openapi.json",
              lifespan=lifespan)


def require_internal_token(x_internal_token: str | None = Header(default=None)) -> None:
    if x_internal_token != os.getenv("INTERNAL_API_TOKEN", "development-internal-token"):
        raise HTTPException(status_code=401, detail="invalid internal service token")


@app.get("/internal/health")
def health() -> dict[str, Any]:
    from .system_readiness import database_health
    return {"status": "UP", "service": "python", "version": APP_VERSION,
            "postgres": database_health(),
            "collector": {'state': 'SIMULATION', 'collectorMode': 'SIMULATED', 'connected': True}
                if runtime.running else runtime.live_worker.status(), "timestamp": _now()}


@app.get('/internal/system/readiness', dependencies=[Depends(require_internal_token)])
def system_readiness() -> dict[str, Any]:
    from .system_readiness import SystemReadinessService
    return _json_value(SystemReadinessService(live_worker=runtime.live_worker,
        runtime_worker=big_move_worker, news=runtime.news).inspect())


@app.get('/internal/research/context/coverage', dependencies=[Depends(require_internal_token)])
def research_context_coverage():
    from .research_context import research_context_readiness
    try:
        return research_context_readiness()
    except Exception:
        raise HTTPException(status_code=503, detail='RESEARCH_CONTEXT_CONFIGURATION_OR_STORAGE_UNAVAILABLE') from None


@app.get('/internal/research/context/day/{session_date}', dependencies=[Depends(require_internal_token)])
def research_context_day(session_date: date):
    from .research_context import HistoricalResearchContextReader
    try:
        return HistoricalResearchContextReader().day(session_date)
    except Exception:
        raise HTTPException(status_code=503, detail='RESEARCH_CONTEXT_STORAGE_UNAVAILABLE') from None


@app.get('/internal/research/context-v3/profiles', dependencies=[Depends(require_internal_token)])
def context_v3_profiles():
    from .context_v3 import ContextV3CoverageService
    return ContextV3CoverageService().profiles()


@app.get('/internal/research/context-v3/coverage', dependencies=[Depends(require_internal_token)])
def context_v3_coverage():
    from .context_v3 import ContextV3CoverageService
    try: return ContextV3CoverageService().coverage()
    except Exception: raise HTTPException(status_code=503, detail='CONTEXT_V3_STORAGE_UNAVAILABLE') from None


@app.get('/internal/research/context-v3/experiments', dependencies=[Depends(require_internal_token)])
def context_v3_experiments():
    from .context_v3_experiments import ContextV3ExperimentService
    from .repository import PostgresRepository
    try:
        return ContextV3ExperimentService(PostgresRepository(os.getenv('DATABASE_URL', ''))).stored()
    except Exception:
        raise HTTPException(status_code=503, detail='CONTEXT_V3_EXPERIMENT_STORAGE_UNAVAILABLE') from None


@app.get('/internal/research/context-v3/experiments/{experiment_id}', dependencies=[Depends(require_internal_token)])
def context_v3_experiment(experiment_id: str):
    from .context_v3_experiments import ContextV3ExperimentService
    from .repository import PostgresRepository
    try:
        result = ContextV3ExperimentService(PostgresRepository(os.getenv('DATABASE_URL', ''))).get(experiment_id)
    except Exception:
        raise HTTPException(status_code=503, detail='CONTEXT_V3_EXPERIMENT_STORAGE_UNAVAILABLE') from None
    if not result.get('available'):
        raise HTTPException(status_code=404, detail=result.get('reason', 'EXPERIMENT_NOT_FOUND'))
    return result


@app.get('/internal/research/context-v3/preflight', dependencies=[Depends(require_internal_token)])
def context_v3_preflight(revision_id: str, instrument_id: int,
                         date_from: date = Query(..., alias='from'),
                         date_to: date = Query(..., alias='to'), profiles: str = 'all'):
    from .context_v3_history import ContextV3HistoryPreflightService
    from .repository import PostgresRepository
    try:
        return ContextV3HistoryPreflightService(PostgresRepository(os.getenv('DATABASE_URL', ''))).inspect(
            revision_id, instrument_id, date_from=date_from, date_to=date_to, profiles=profiles,
            allow_network=False)
    except Exception:
        raise HTTPException(status_code=503, detail='CONTEXT_V3_PREFLIGHT_UNAVAILABLE') from None


@app.get('/internal/research/context-v3/runtime', dependencies=[Depends(require_internal_token)])
def context_v3_runtime():
    from .context_v3 import ContextV3RuntimeFeatureService
    try: return ContextV3RuntimeFeatureService().snapshot()
    except Exception:
        current=datetime.now(timezone.utc)
        try: return ContextV3RuntimeFeatureService.unavailable(current,None,'RUNTIME_CONTEXT_UNAVAILABLE')
        except Exception:
            return {'status':'UNAVAILABLE','asOf':current.isoformat(),
                'marketAsOf':None,'profiles':{},'reasons':['RUNTIME_CONTEXT_UNAVAILABLE']}


@app.get('/internal/research/microstructure/runtime', dependencies=[Depends(require_internal_token)])
def microstructure_runtime():
    from .microstructure import MicrostructureRuntimeService
    try: return MicrostructureRuntimeService().snapshot()
    except Exception:
        return {'status':'UNAVAILABLE','schemaVersion':'banknifty-microstructure-v1',
            'marketAsOf':None,'decisionAt':datetime.now(timezone.utc).isoformat(),
            'futures':None,'options':None,'greeks':{'status':'GREEKS_UNAVAILABLE'},
            'missingReasons':['LOCAL_LIVE_EVIDENCE_UNAVAILABLE'],'freshness':None}


@app.get('/internal/research/shadow/summary', dependencies=[Depends(require_internal_token)])
def shadow_summary():
    from .shadow_observations import ShadowRepository
    try: return ShadowRepository().summary()
    except Exception:
        raise HTTPException(status_code=503,detail='SHADOW_EVIDENCE_UNAVAILABLE') from None


@app.get('/internal/research/shadow/recent', dependencies=[Depends(require_internal_token)])
def shadow_recent(limit:int=Query(50,ge=1,le=100)):
    from .shadow_observations import ShadowRepository
    try: return {'items':ShadowRepository().recent(limit)}
    except Exception:
        raise HTTPException(status_code=503,detail='SHADOW_EVIDENCE_UNAVAILABLE') from None


@app.get('/internal/research/market-structure/coverage', dependencies=[Depends(require_internal_token)])
def market_structure_coverage():
    from .market_structure_context import market_structure_readiness
    try: return market_structure_readiness()
    except Exception: raise HTTPException(status_code=503, detail='MARKET_STRUCTURE_UNAVAILABLE') from None


@app.get('/internal/research/market-structure/day/{session_date}', dependencies=[Depends(require_internal_token)])
def market_structure_day(session_date: date):
    from .market_structure_context import MarketStructureContextReader
    try: return MarketStructureContextReader().day(session_date)
    except Exception: raise HTTPException(status_code=503, detail='MARKET_STRUCTURE_UNAVAILABLE') from None


@app.get('/internal/research/event-impact/summary', dependencies=[Depends(require_internal_token)])
def event_impact_summary():
    from .news.impact_study import EventImpactStudyService
    try: return EventImpactStudyService().summary()
    except Exception: raise HTTPException(status_code=503, detail='EVENT_IMPACT_UNAVAILABLE') from None


@app.get('/internal/research/event-impact/{event_id}', dependencies=[Depends(require_internal_token)])
def event_impact_detail(event_id: str):
    from .news.impact_study import EventImpactStudyService
    try: return EventImpactStudyService().detail(event_id)
    except Exception: raise HTTPException(status_code=503, detail='EVENT_IMPACT_UNAVAILABLE') from None


@app.get("/internal/dashboard", dependencies=[Depends(require_internal_token)])
def dashboard() -> dict[str, Any]: return runtime.dashboard()


@app.get("/internal/candles", dependencies=[Depends(require_internal_token)])
def candles(instrument: str = "BANKNIFTY", interval: str = Query(default="1m", pattern="^1m$"),
            limit: int = Query(default=500, ge=1, le=2000)) -> dict[str, Any]:
    if instrument.upper() not in {"BANKNIFTY", "NIFTY BANK"}:
        raise HTTPException(status_code=400, detail="only BANKNIFTY is supported")
    return {"instrument": "BANKNIFTY", "interval": interval, "candles": runtime.candles()[-limit:]}


@app.get("/internal/predictions/latest", dependencies=[Depends(require_internal_token)])
def predictions_latest() -> dict[str, Any]:
    result = ProductDataReader().latest_predictions()
    if not result["predictions"]:
        result["predictions"] = runtime.unavailable_predictions()
    return result


@app.get("/internal/predictions/history", dependencies=[Depends(require_internal_token)])
def predictions_history(
    page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
    date: str | None = None, horizon: int | None = Query(None, ge=5, le=20),
    direction: str | None = Query(None, pattern="^(UP|DOWN|NO_TRADE)$"), resolved: bool | None = None,
) -> dict[str, Any]:
    if horizon is not None and horizon not in HORIZONS:
        raise HTTPException(status_code=422, detail="unsupported prediction horizon")
    return ProductDataReader().prediction_history(page=page, page_size=page_size,
        date_value=date, horizon=horizon, direction=direction, resolved=resolved)


@app.get("/internal/models/status", dependencies=[Depends(require_internal_token)])
def models_status() -> dict[str, Any]: return runtime.models()


def _evidence_read(operation, **kwargs):
    from .evidence_journal import EvidenceAnalysisService
    try:
        service=EvidenceAnalysisService()
        return getattr(service,operation)(**kwargs)
    except ValueError as error:
        raise HTTPException(status_code=422,detail=str(error)) from error
    except Exception as error:
        logging.getLogger(__name__).exception('evidence_journal_read_failed')
        raise HTTPException(status_code=503,detail='EVIDENCE_STORAGE_UNAVAILABLE') from error


@app.get('/internal/research/evidence-summary',dependencies=[Depends(require_internal_token)])
def evidence_summary():
    return _evidence_read('summary')


@app.get('/internal/research/evidence-journal',dependencies=[Depends(require_internal_token)])
def evidence_journal(page:int=Query(1,ge=1),page_size:int=Query(25,alias='pageSize',ge=1,le=100),
                     date_value:date|None=Query(None,alias='date'),horizon:int|None=None,resolved:bool|None=None):
    return _evidence_read('journal',page=page,page_size=page_size,date_value=date_value,horizon=horizon,resolved=resolved)


@app.get('/internal/research/evidence-journal/{prediction_id}',dependencies=[Depends(require_internal_token)])
def evidence_journal_detail(prediction_id:str):
    result=_evidence_read('journal',prediction_id=prediction_id)
    if not result['items']:
        raise HTTPException(status_code=404,detail='NO_PROSPECTIVE_EVIDENCE: old predictions are never backfilled')
    return {'available':True,'record':result['items'][0]}


@app.get("/internal/live/status", dependencies=[Depends(require_internal_token)])
def live_status() -> dict[str, Any]: return runtime.live_status()


@app.post("/internal/live/simulation/start", dependencies=[Depends(require_internal_token)])
def simulation_start() -> Any:
    if os.getenv('LIVE_MARKET_ENABLED', 'false').lower() == 'true' or runtime.live_worker.status()['connected']:
        return JSONResponse(status_code=409, content={'error': 'REAL_LIVE_ENABLED', 'message': 'Disable real live collection before starting simulation.'})
    if runtime.running:
        return JSONResponse(status_code=409, content={"error": "SIMULATION_ALREADY_RUNNING", "message": "Simulation is already running."})
    return runtime.start()


@app.post("/internal/live/simulation/stop", dependencies=[Depends(require_internal_token)])
def simulation_stop() -> Any:
    if not runtime.running:
        return JSONResponse(status_code=409, content={"error": "SIMULATION_NOT_RUNNING", "message": "Simulation is not running."})
    return runtime.stop()


@app.get("/internal/options", dependencies=[Depends(require_internal_token)])
def options(expiry: str | None = None) -> dict[str, Any]: return runtime.options(expiry)


@app.get("/internal/futures", dependencies=[Depends(require_internal_token)])
def futures() -> dict[str, Any]: return runtime.futures()


@app.get("/internal/data-quality", dependencies=[Depends(require_internal_token)])
def data_quality() -> dict[str, Any]: return runtime.quality()


@app.get("/internal/configuration", dependencies=[Depends(require_internal_token)])
def configuration() -> dict[str, Any]:
    from .system_readiness import runtime_configuration
    return runtime_configuration(runtime.running, runtime.live_worker)


@app.get("/internal/intelligence/current", dependencies=[Depends(require_internal_token)])
def intelligence_current() -> dict[str, Any]: return runtime.intelligence()


@app.get("/internal/intelligence/history", dependencies=[Depends(require_internal_token)])
def intelligence_history(
    page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
    date: str | None = None, horizon: int | None = Query(None, ge=10, le=30),
    direction: str | None = Query(None, pattern="^(UP|DOWN)$"), resolved: bool | None = None,
) -> dict[str, Any]:
    if horizon is not None and horizon not in {10, 20, 30}:
        raise HTTPException(status_code=422, detail="unsupported intelligence horizon")
    return ProductDataReader().intelligence_history(page=page, page_size=page_size,
        date_value=date, horizon=horizon, direction=direction, resolved=resolved)


@app.get('/internal/research/external/current', dependencies=[Depends(require_internal_token)])
def external_research_current(instrument_id: int | None = None, as_of: datetime | None = None):
    stored = ProductDataReader().current_intelligence(as_of=as_of, instrument_id=instrument_id)
    return _json_value({
        "available": bool(stored.get("kronosExperimental", {}).get("available")
                          or stored.get("timeMachineExperimental", {}).get("available")),
        "kronosExperimental": stored.get("kronosExperimental"),
        "timeMachineExperimental": stored.get("timeMachineExperimental"),
        "researchOnly": True, "usedByPriceCoreV2": False,
    })


@app.get('/internal/research/external/history', dependencies=[Depends(require_internal_token)])
def external_research_history(instrument_id: int, revision_id: str | None = None,
                              model: str | None = Query(None, pattern="^(KRONOS_MINI_ZERO_SHOT_V1|BANKNIFTY_TIME_MACHINE_V1)$"),
                              limit: int = Query(50, ge=1, le=200)):
    from .repository import PostgresRepository
    try:
        repository = PostgresRepository(os.getenv('DATABASE_URL', ''))
        rows = repository.experimental_outputs_for(revision_id, instrument_id,
            model_name=model, limit=limit)
    except Exception:
        raise HTTPException(status_code=503, detail='EXTERNAL_RESEARCH_STORAGE_UNAVAILABLE') from None
    items = [{
        "id": row["output_id"], "asOf": row["as_of"].isoformat(),
        "model": row["model_name"], "modelVersion": row["model_version"],
        "status": (row["summary"] or {}).get("researchStatus"),
        "referenceMinute": (row["summary"] or {}).get("referenceMinute"),
        "pointInTimeStatus": row["point_in_time_status"],
        "decisionAt": (row["summary"] or {}).get("decisionAt"),
        "predictionIds": row["prediction_ids"],
        "replayPoolRevisionId": row["replay_pool_revision_id"],
        "inputChecksum": row["input_checksum"],
        "horizons": (row["summary"] or {}).get("horizons", {}),
        "forecastPath": row["forecast_path"], "summary": row["summary"],
        "researchOnly": True, "usedByPriceCoreV2": False,
    } for row in rows]
    return {"available": bool(items), "items": items,
            "reason": None if items else "NO_EXTERNAL_RESEARCH_OUTPUTS",
            "researchOnly": True, "usedByPriceCoreV2": False}


@app.get('/internal/research/external/status', dependencies=[Depends(require_internal_token)])
def external_research_status():
    return _json_value({"kronos": KronosForecastAdapter().health(),
                        "timeMachine": _time_machine_availability(),
                        "researchOnly": True, "usedByPriceCoreV2": False})


@app.get('/internal/research/external/evaluation', dependencies=[Depends(require_internal_token)])
def external_research_evaluation(revision_id: str, instrument_id: int):
    from .external_research_evaluation import ExternalResearchEvaluationService
    from .repository import PostgresRepository
    try:
        service = ExternalResearchEvaluationService(
            PostgresRepository(os.getenv('DATABASE_URL', '')))
        return _json_value(service.evaluate(revision_id, instrument_id))
    except Exception:
        raise HTTPException(status_code=503, detail='EXTERNAL_RESEARCH_EVALUATION_UNAVAILABLE') from None


@app.get("/internal/memory/current-similarity", dependencies=[Depends(require_internal_token)])
def current_memory_similarity() -> dict[str, Any]:
    return runtime.current_similarity()


@app.get("/internal/memory/similar", dependencies=[Depends(require_internal_token)])
def similar_memory() -> dict[str, Any]:
    return runtime.current_similarity()


@app.get("/internal/memory/days", dependencies=[Depends(require_internal_token)])
def memory_days() -> dict[str, Any]: return runtime.memory_days()


@app.get("/internal/memory/day/{memory_date}", dependencies=[Depends(require_internal_token)])
def memory_day(memory_date: date) -> dict[str, Any]: return runtime.memory_day(memory_date)


@app.get("/internal/news/status", dependencies=[Depends(require_internal_token)])
def news_status() -> dict[str, Any]: return runtime.news.status()


@app.get("/internal/news/latest", dependencies=[Depends(require_internal_token)])
def news_latest(limit: int = Query(30, ge=1, le=100)) -> dict[str, Any]: return runtime.news.latest(limit)


@app.post("/internal/news/refresh", dependencies=[Depends(require_internal_token)])
def news_refresh() -> dict[str, Any]: return runtime.news.refresh()


@app.get("/internal/news/events", dependencies=[Depends(require_internal_token)])
def news_events(limit: int = Query(100, ge=1, le=250), category: str | None = None,
                relevance: str | None = None, date: str | None = None) -> dict[str, Any]:
    if date is not None:
        try: datetime.strptime(date, "%Y-%m-%d")
        except ValueError: raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")
    return runtime.news.events(limit, category, relevance, date)


@app.get("/internal/news/events/{event_id}", dependencies=[Depends(require_internal_token)])
def news_event(event_id: str) -> dict[str, Any]: return runtime.news.event(event_id)


@app.get("/internal/news/event-memory/current", dependencies=[Depends(require_internal_token)])
def current_news_memory() -> dict[str, Any]:
    return runtime.news.current_memory(runtime.current_feature_values())


@app.get("/internal/news/event-memory/{event_id}", dependencies=[Depends(require_internal_token)])
def news_event_memory(event_id: str) -> dict[str, Any]:
    return runtime.news.event_memory(event_id, runtime.current_feature_values())


@app.get('/internal/memory/coverage', dependencies=[Depends(require_internal_token)])
def memory_coverage():
    from .memory_enrichment import coverage
    return coverage()


@app.get('/internal/news/scheduled', dependencies=[Depends(require_internal_token)])
def news_scheduled():
    from .news.scheduled import scheduled_events
    return scheduled_events()
