from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time, timedelta
from decimal import Decimal
from hashlib import sha256
import json
import math
from pathlib import Path
from statistics import pstdev
from typing import Any

from .big_move_labels import BIG_MOVE_HORIZONS, calculate_big_move_label, default_big_move_policy
from .calendar import IST
from .market_memory import FileMarketMemoryRepository
from .models import Candle, TradingSession
from .news.event_memory import build_historical_events, write_structured_events


def _chart(path: Path) -> list[dict[str, Any]]:
    result = json.loads(path.read_text(encoding="utf-8"))["chart"]["result"][0]
    quote = result["indicators"]["quote"][0]
    rows = []
    for index, stamp in enumerate(result.get("timestamp") or []):
        values = {name: series[index] for name, series in quote.items()}
        if all(values.get(name) is not None for name in ("open", "high", "low", "close")):
            rows.append({"at": datetime.fromtimestamp(stamp, tz=IST), **values})
    return rows


def load_intraday(root: str | Path) -> dict[str, list[dict[str, Any]]]:
    raw = Path(root) / "raw" / "index_history"
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for path in sorted(raw.glob("nsebank_intraday_yahoo_*.json")):
        for row in _chart(path):
            grouped[row["at"].date().isoformat()].append(row)
    for rows in grouped.values():
        unique = {row["at"]: row for row in rows}
        rows[:] = sorted(unique.values(), key=lambda item: item["at"])
    for path in sorted(raw.glob('zerodha_*.json')):
        payload=json.loads(path.read_text(encoding='utf-8'))
        if not payload.get('quality',{}).get('complete'):continue
        grouped[payload['session_date']]=[{**item,'at':datetime.fromisoformat(item['at'])} for item in payload['rows']]
    from .live_session_archive import load_live_archives
    for day, rows in load_live_archives(root).items():
        # Curated provider evidence remains authoritative for its existing dates.
        if day not in grouped:
            grouped[day] = rows
    return grouped


def _ret(rows: list[dict[str, Any]], index: int, minutes: int) -> float | None:
    lookup = {row["at"]: row for row in rows}
    prior = lookup.get(rows[index]["at"] - timedelta(minutes=minutes))
    return rows[index]["close"] / prior["close"] - 1 if prior and prior["close"] else None


def _candle(row: dict[str, Any]) -> Candle:
    return Candle(row["at"], *(Decimal(str(row[key])) for key in ("open", "high", "low", "close")),
                  int(row.get("volume") or 0), None)


def build_episodes(root: str | Path) -> dict[str, Any]:
    root = Path(root)
    grouped = load_intraday(root)
    days = {item.trading_date.isoformat(): item for item in FileMarketMemoryRepository(root).days()}
    policy = default_big_move_policy()
    output: list[dict[str, Any]] = []
    for day, rows in sorted(grouped.items()):
        if day not in days:
            continue
        candles = [_candle(row) for row in rows]
        session_date = candles[0].timestamp.astimezone(IST).date()
        session = TradingSession(session_date, datetime.combine(session_date, time(9, 15), IST),
                                 datetime.combine(session_date, time(15, 30), IST))
        if rows[0].get('session_open'):
            session = TradingSession(session_date, rows[0]['session_open'], rows[0]['session_close'], rows[0]['session_status'])
        for index in range(20, len(rows), 15):
            as_of = rows[index]["at"]
            labels = {}
            for horizon in BIG_MOVE_HORIZONS:
                label = calculate_big_move_label(dataset_revision_id="market-memory-v2", instrument_id=0,
                    as_of_candle=candles[index], horizon=horizon, session=session,
                    candles_known_at_as_of=candles[:index + 1], future_candles=candles[index + 1:index + 31],
                    policy=policy)
                labels[str(horizon)] = label.to_dict() if label else None
            if any(value is None for value in labels.values()):
                continue
            recent = [math.log(rows[pos]["close"] / rows[pos - 1]["close"])
                      for pos in range(max(1, index - 20), index + 1)]
            recent5 = rows[max(0, index - 4):index + 1]
            prior20 = rows[max(0, index - 39):max(1, index - 19)]
            range5 = max(item["high"] for item in recent5) / min(item["low"] for item in recent5) - 1
            prior_range = max(item["high"] for item in prior20) / min(item["low"] for item in prior20) - 1 if prior20 else None
            return20 = _ret(rows, index, 20)
            regime = "TREND_UP" if return20 is not None and return20 > .002 else "TREND_DOWN" if return20 is not None and return20 < -.002 else "RANGE"
            state = {f"return_{h}m": _ret(rows, index, h) for h in (1, 5, 10, 20)}
            state.update({f"realized_volatility_{h}m": pstdev(recent[-h:]) if len(recent) >= h else None for h in (5, 10, 20)})
            state.update({"range_expansion_ratio": range5 / prior_range if prior_range else None,
                "session_progress": (as_of-session.open_at).total_seconds()/(session.close_at-session.open_at).total_seconds(), "regime": regime,
                "source_max_timestamp": as_of.isoformat(), "futures_premium_pct": None,
                "futures_oi_change_5m": None, "futures_buildup": "UNKNOWN", "pcr_oi": None,
                "oi_change_imbalance": None, "atm_straddle_change": None,
                "ce_oi_concentration": None, "pe_oi_concentration": None,
                "expiry_context": days[day].expiry_context})
            output.append({"episode_id": sha256(f"v2|{day}|{as_of.isoformat()}".encode()).hexdigest()[:24],
                "session_date": day, "as_of": as_of.isoformat(), "state": state, "outcome": labels,
                "regime": regime, "expiry_context": days[day].expiry_context,
                "source_quality": "INDEX_MINUTE_VERIFIED_BIG_MOVE_V2"})
    derived = root / "derived"
    derived.mkdir(exist_ok=True)
    (derived / "episodes.json").write_text(json.dumps(output, indent=2), encoding="utf-8", newline="\n")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    manifest["derived"] = {**manifest.get("derived", {}), "numerical_episode_count": len(output),
        "episode_policy_version": policy.version, "episode_checksum_sha256": sha256((derived / "episodes.json").read_bytes()).hexdigest()}
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8", newline="\n")
    return manifest["derived"]


def build_events(root: str | Path) -> dict[str, Any]:
    root = Path(root)
    repository = FileMarketMemoryRepository(root)
    events = build_historical_events(repository.days(), load_intraday(root))
    target = root / "derived" / "structured_events.json"
    target.parent.mkdir(exist_ok=True)
    write_structured_events(target, events)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    measurable = sum(item.reaction.get("status") == "REACTION_AVAILABLE" for item in events)
    manifest["derived"] = {**manifest.get("derived", {}), "structured_event_count": len(events),
        "reaction_measurable_event_count": measurable,
        "event_checksum_sha256": sha256(target.read_bytes()).hexdigest()}
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8", newline="\n")
    return manifest["derived"]
