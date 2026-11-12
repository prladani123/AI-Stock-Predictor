from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from .models import Instrument
from .repository import Repository
from .zerodha import ReadOnlyZerodhaAdapter
from .calendar import IST


def _date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def is_banknifty_index(row: dict[str, Any]) -> bool:
    symbol = str(row.get("tradingsymbol", "")).upper().replace("-", " ").strip()
    name = str(row.get("name", "")).upper().replace("-", " ").strip()
    exchange = str(row.get("exchange", "")).upper()
    return exchange == "NSE" and (symbol in {"NIFTY BANK", "BANKNIFTY"} or name in {"NIFTY BANK", "BANKNIFTY"})


def is_banknifty_future(row: dict[str, Any]) -> bool:
    return (
        str(row.get("exchange", "")).upper() == "NFO"
        and str(row.get("instrument_type", "")).upper() == "FUT"
        and (
            str(row.get("name", "")).upper() == "BANKNIFTY"
            or str(row.get("tradingsymbol", "")).upper().startswith("BANKNIFTY")
        )
    )


def select_banknifty_instruments(rows: list[dict[str, Any]], as_of: date | None = None) -> list[Instrument]:
    from .market_structure_context import ConstituentUniverseService
    members = ConstituentUniverseService().symbols(as_of) if as_of else ()
    selected: list[Instrument] = []
    for row in rows:
        option = (str(row.get('exchange','')).upper()=='NFO'
            and str(row.get('instrument_type','')).upper() in {'CE','PE'}
            and (str(row.get('name','')).upper()=='BANKNIFTY' or str(row.get('tradingsymbol','')).upper().startswith('BANKNIFTY')))
        expiry = _date(row.get('expiry'))
        constituent = row.get('exchange') == 'NSE' and row.get('instrument_type') == 'EQ' and row.get('tradingsymbol') in members
        if not (is_banknifty_index(row) or is_banknifty_future(row) or option or constituent):
            continue
        if option and (expiry is None or (as_of is not None and expiry<as_of)): continue
        strike = Decimal(str(row['strike'])) if option and row.get('strike') is not None else None
        if option and (strike is None or not strike.is_finite() or strike<=0): continue
        selected.append(
            Instrument(
                instrument_token=int(row["instrument_token"]),
                exchange=str(row["exchange"]).upper(),
                tradingsymbol=str(row["tradingsymbol"]),
                name=str(row.get("name") or ""),
                segment=str(row.get("segment") or ""),
                instrument_type=str(row.get("instrument_type") or ""),
                expiry=_date(row.get("expiry")),
                tick_size=Decimal(str(row.get("tick_size") or "0.05")),
                lot_size=int(row.get("lot_size") or 1),
                strike=strike,
            )
        )
    selected.sort(key=lambda item: (item.exchange, item.expiry or date.max, item.tradingsymbol))
    return selected


class InstrumentSnapshotService:
    def __init__(self, adapter: ReadOnlyZerodhaAdapter, repository: Repository) -> None:
        self.adapter = adapter
        self.repository = repository

    def capture(self, captured_at: datetime) -> tuple[int, list[Instrument], dict[int, int]]:
        rows = self.adapter.instruments()
        selected = select_banknifty_instruments(rows, captured_at.astimezone(IST).date())
        if not any(item.exchange == "NSE" and item.tradingsymbol in ('NIFTY BANK','BANKNIFTY') for item in selected):
            raise ValueError("BANKNIFTY index was not found in instrument snapshot")
        if not any(item.exchange == "NFO" and item.instrument_type == "FUT" for item in selected):
            raise ValueError("BANKNIFTY futures were not found in instrument snapshot")
        snapshot_id, mapping = self.repository.store_snapshot(rows, selected, captured_at)
        return snapshot_id, selected, mapping
