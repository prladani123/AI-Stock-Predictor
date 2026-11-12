from __future__ import annotations

from dataclasses import dataclass, field
import json
from decimal import Decimal
import os


def _positive_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _bounded_threads(name: str, default: int = 2) -> int:
    value = _positive_int(name, default)
    if value > 2:
        raise ValueError(f"{name} must not exceed 2 on the target machine")
    return value


def _positive_float(name: str, default: float) -> float:
    value = float(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, str(default)).strip().lower()
    if raw not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return raw == "true"


@dataclass(frozen=True)
class ResearchContextSettings:
    upstox_enabled: bool = False
    upstox_token: str = field(default='', repr=False)
    upstox_plus_enabled: bool = False
    dhan_enabled: bool = False
    dhan_client_id: str = field(default='', repr=False)
    dhan_token: str = field(default='', repr=False)
    dhan_data_enabled: bool = False
    provider: str = 'auto'
    option_width: int = 10
    staleness_version: str = 'context-staleness-v1'
    staleness_seconds: dict = field(default_factory=lambda: {
        'BANKNIFTY': 120, 'INDIA_VIX': 180, 'GIFT_NIFTY': 300,
        'BRENT': 900, 'USDINR': 900, 'OPTIONS': 120, 'MARKET_INFORMATION': 3600})
    global_instruments_file: str = ''
    bod_instruments_file: str = ''
    instrument_news_enabled: bool = False
    market_structure_enabled: bool = False
    market_structure_interval: int = 60
    event_impact_minimum_sample: int = 5

    @classmethod
    def from_env(cls):
        defaults = cls()
        width = int(os.getenv('RESEARCH_CONTEXT_OPTION_WIDTH', '10'))
        provider = os.getenv('RESEARCH_CONTEXT_PROVIDER', 'auto')
        limits = json.loads(os.getenv('RESEARCH_CONTEXT_STALENESS_SECONDS') or json.dumps(defaults.staleness_seconds))
        version = os.getenv('RESEARCH_CONTEXT_STALENESS_POLICY_VERSION', defaults.staleness_version)
        if not 0 <= width <= 10 or provider not in ('auto', 'dhan', 'upstox') or not version:
            raise ValueError('Invalid research-context configuration')
        if not isinstance(limits, dict) or set(limits) != set(defaults.staleness_seconds) or any(type(v) is not int or not 0 < v <= 86400 for v in limits.values()):
            raise ValueError('Research staleness requires positive bounded seconds for every source')
        return cls(_bool('UPSTOX_ANALYTICS_ENABLED'), os.getenv('UPSTOX_ANALYTICS_TOKEN', ''),
            _bool('UPSTOX_PLUS_EXPIRED_DATA_ENABLED'), _bool('DHAN_RESEARCH_ENABLED'),
            os.getenv('DHAN_CLIENT_ID', ''), os.getenv('DHAN_ACCESS_TOKEN', ''), _bool('DHAN_DATA_API_ENABLED'),
            provider, width, version, limits, os.getenv('UPSTOX_GLOBAL_INSTRUMENTS_FILE', ''),
            os.getenv('UPSTOX_BOD_INSTRUMENTS_FILE',''), _bool('NEWS_SOURCE_UPSTOX_INSTRUMENT_ENABLED'),
            _bool('MARKET_STRUCTURE_CONTEXT_ENABLED'), max(60,_positive_int('MARKET_STRUCTURE_CONTEXT_INTERVAL_SECONDS',60)),
            _positive_int('EVENT_IMPACT_MINIMUM_SAMPLE',5))

    def public_configuration(self):
        return {'provider': self.provider, 'optionWidth': self.option_width,
                'stalenessPolicyVersion': self.staleness_version, 'stalenessSeconds': self.staleness_seconds,
                'upstoxPlusEnabled': self.upstox_plus_enabled,
                'bodMetadataConfigured':bool(self.bod_instruments_file), 'instrumentNewsEnabled':self.instrument_news_enabled,
                'runtimeContextEnabled':self.market_structure_enabled,'runtimeContextIntervalSeconds':self.market_structure_interval,
                'eventImpactMinimumSample':self.event_impact_minimum_sample}


@dataclass(frozen=True)
class Settings:
    database_url: str
    requests_per_second: float = 3.0
    chunk_days: int = 60
    max_retries: int = 3
    allow_real_zerodha: bool = False
    zerodha_api_key: str | None = None
    zerodha_access_token: str | None = None
    threshold_policy_version: str = "fixed-v1"
    label_threshold_5: Decimal = Decimal("0.0010")
    label_threshold_10: Decimal = Decimal("0.0015")
    label_threshold_15: Decimal = Decimal("0.0020")
    label_threshold_20: Decimal = Decimal("0.0025")
    ml_threads: int = 2
    ml_random_seed: int = 1729
    live_lateness_seconds: int = 3
    live_stale_seconds: int = 10
    live_queue_limit: int = 2048
    live_option_strike_width: int = 5
    raw_tick_retention_days: int = 14
    live_market_enabled: bool = False
    live_universe_refresh_seconds: int = 300

    @classmethod
    def from_env(cls, require_database: bool = True) -> "Settings":
        database_url = os.getenv("DATABASE_URL", "")
        if require_database and not database_url:
            raise ValueError("DATABASE_URL is required")
        return cls(
            database_url=database_url,
            requests_per_second=_positive_float("HISTORICAL_REQUESTS_PER_SECOND", 3.0),
            chunk_days=_positive_int("HISTORICAL_CHUNK_DAYS", 60),
            max_retries=_positive_int("HISTORICAL_MAX_RETRIES", 3),
            allow_real_zerodha=_bool("ALLOW_REAL_ZERODHA", False),
            zerodha_api_key=os.getenv("ZERODHA_API_KEY") or None,
            zerodha_access_token=os.getenv("ZERODHA_ACCESS_TOKEN") or None,
            threshold_policy_version=os.getenv("LABEL_THRESHOLD_POLICY_VERSION", "fixed-v1"),
            label_threshold_5=Decimal(os.getenv("LABEL_THRESHOLD_5_MIN", "0.0010")),
            label_threshold_10=Decimal(os.getenv("LABEL_THRESHOLD_10_MIN", "0.0015")),
            label_threshold_15=Decimal(os.getenv("LABEL_THRESHOLD_15_MIN", "0.0020")),
            label_threshold_20=Decimal(os.getenv("LABEL_THRESHOLD_20_MIN", "0.0025")),
            ml_threads=_bounded_threads("ML_THREADS", 2),
            ml_random_seed=int(os.getenv("ML_RANDOM_SEED", "1729")),
            live_lateness_seconds=_positive_int("LIVE_LATENESS_SECONDS", 3),
            live_stale_seconds=_positive_int("LIVE_STALE_SECONDS", 10),
            live_queue_limit=_positive_int("LIVE_QUEUE_LIMIT", 2048),
            live_option_strike_width=_positive_int("LIVE_OPTION_STRIKE_WIDTH", 5),
            raw_tick_retention_days=_positive_int("RAW_TICK_RETENTION_DAYS", 14),
            live_market_enabled=_bool('LIVE_MARKET_ENABLED',False),
            live_universe_refresh_seconds=_positive_int('LIVE_UNIVERSE_REFRESH_SECONDS',300),
        )

    def label_thresholds(self) -> dict[int, Decimal]:
        return {
            5: self.label_threshold_5,
            10: self.label_threshold_10,
            15: self.label_threshold_15,
            20: self.label_threshold_20,
        }

    def require_live_credentials(self) -> tuple[str, str]:
        if not self.allow_real_zerodha:
            raise RuntimeError("Real Zerodha access is disabled; set ALLOW_REAL_ZERODHA=true only after approval")
        if not self.zerodha_api_key or not self.zerodha_access_token:
            raise ValueError("ZERODHA_API_KEY and ZERODHA_ACCESS_TOKEN are required for an approved real import")
        return self.zerodha_api_key, self.zerodha_access_token
