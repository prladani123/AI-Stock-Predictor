from __future__ import annotations

from datetime import date, datetime
from hashlib import sha256
from pathlib import Path
import json

from .calendar import IST
from .memory_builder import _chart
from .news.storage import news_repository
from .news.backfill import StoredMarketData
from .rollup_facts import derivatives, expiry_context
from .live_session_archive import LiveSessionArchiveService, previous_close
from .paths import memory_root
import logging
import os
from threading import Event, Thread


def finalize_day(root: str | Path, session_date: date, *, force: bool = False) -> Path:
    root = Path(root)
    target = root / "daily" / f"{session_date.isoformat()}.md"
    if target.exists():
        raise FileExistsError(f"{target} already exists; daily memories are never overwritten")
    market = StoredMarketData()
    minutes = market.minutes(session_date)
    raw = root / "raw" / "index_history" / "nsebank_daily_yahoo.json"
    rows = _chart(raw) if raw.exists() else []
    index = next((index for index, row in enumerate(rows) if row["at"].date() == session_date), None)
    if not minutes and index is None:
        raise ValueError("no stored daily BANKNIFTY candle for requested session")
    if minutes:
        session = market.session(session_date)
        if session is None:
            raise ValueError('Session calendar unknown or closed')
        from datetime import timedelta
        expected = {session[0] + timedelta(minutes=i) for i in range(int((session[1]-session[0]).total_seconds()/60))}
        minutes = [r for r in minutes if r['at'] in expected]
        if {r['at'] for r in minutes} != expected or len(minutes) != len(expected):
            raise ValueError('Incomplete finalized BANKNIFTY session; rollup pending')
        row = {'open':minutes[0]['open'],'close':minutes[-1]['close'],
               'high':max(r['high'] for r in minutes),'low':min(r['low'] for r in minutes)}
    else:
        row = rows[index]
    previous = previous_close(root, session_date, market, rows)
    session_type = 'SPECIAL' if minutes and session[2]=='SPECIAL' else 'NORMAL'
    close_change = (row["close"] / previous - 1) * 100 if previous else None
    gap = (row["open"] / previous - 1) * 100 if previous else None
    open_close = (row["close"] / row["open"] - 1) * 100
    intraday_range = (row["high"] / row["low"] - 1) * 100
    direction = "UP" if row["close"] > row["open"] else "DOWN" if row["close"] < row["open"] else "FLAT"
    events = [item for item in news_repository().events(include_historical=False)
              if item.session_date == session_date]
    facts = derivatives(market, session_date, minutes)
    expiry = expiry_context(facts, session_date)
    facts_text = '\n'.join(f"### {kind.upper()}\n\n" + '\n'.join(
        f"- {key}: {json.dumps(value, ensure_ascii=False) if isinstance(value, (dict,list)) else value if value is not None else 'NOT_VERIFIED'}"
        for key,value in values.items()) for kind,values in facts.items())
    associations = "\n".join(f"- {item.first_seen_at.isoformat() if item.first_seen_at else 'UNKNOWN'} — {item.headline_or_summary} ({item.source_name})" for item in events) or "- No stored relevant event association."
    tags = list(dict.fromkeys(tag for item in events for tag in item.event_tags))
    payload = {"date": session_date.isoformat(), "banknifty_close": round(row["close"], 2),
        "close_change_pct": round(close_change, 3) if close_change is not None else None,
        "gap_pct": round(gap, 3) if gap is not None else None,
        "intraday_range_pct": round(intraday_range, 3), "open_to_close_pct": round(open_close, 3),
        "direction": direction, "expiry_context": expiry, "evidence_grade": "OBSERVED_ASSOCIATION",
        "timing_precision": "EXACT_OR_NARROW_WINDOW" if events else "SESSION_LEVEL_ONLY",
        "tags": tags, "special_flags": ["AUTOMATED_FACTUAL_ROLLUP"]}
    payload['derivatives'] = facts
    payload['eventReactions'] = {item.event_id:item.reaction for item in events}
    text = f"""---
date: {session_date.isoformat()}
market: NSE
instrument: BANKNIFTY
session_type: {session_type}
expiry_context: {expiry}
data_quality: VERIFIED_DAILY_OHLC_OBSERVED_EVENTS
causal_evidence_grade: OBSERVED_ASSOCIATION
timing_precision: {payload['timing_precision']}
source_quality: MEDIUM
generated_at: {datetime.now(IST).isoformat()}
---

# BANKNIFTY Daily Market Memory — {session_date.isoformat()}

## 1. Verified BANKNIFTY price record

| Metric | Value |
|---|---:|
| Previous close | {previous if previous is not None else 'UNKNOWN'} |
| Open | {row['open']:.2f} |
| High | {row['high']:.2f} |
| Low | {row['low']:.2f} |
| Close | {row['close']:.2f} |
| Close change | {close_change if close_change is not None else 'UNKNOWN'}% |
| Opening gap | {gap if gap is not None else 'UNKNOWN'}% |
| Open → close | {open_close:.3f}% |
| Intraday range | {intraday_range:.3f}% |
| Session classification | **{direction}** |

## 2. What happened — BANKNIFTY-specific read

BANKNIFTY closed {direction.lower()} with a {intraday_range:.3f}% verified intraday range.

**Intraday structure / timing:** Refer to stored point-in-time candles; no unsupported narrative was generated.

**Interpretation note:** Automated factual rollup only.

## 3. Why it moved — causal map

Observed event associations, not causal claims:
{associations}

## 4. News / event timeline for memory retrieval

{associations}

## 5. Pattern lesson for the future engine

Requires human review; the automated rollup does not infer causality.

## 6. Option / futures context

{facts_text}

## 7. Machine-readable memory block

```json
{json.dumps(payload, indent=2)}
```

## 8. Sources

1. {'Stored PASS finalized BANKNIFTY minutes, checked against the explicit session calendar.' if minutes else 'Stored Yahoo Finance ^NSEBANK daily chart response under raw/index_history.'}
2. Stored live-news records with immutable first_seen_at timestamps, where listed above.
"""
    target.parent.mkdir(parents=True, exist_ok=True)
    if minutes:
        LiveSessionArchiveService(root).archive(session_date, session, minutes)
    with target.open('x', encoding='utf-8', newline='\n') as output:
        output.write(text)
    manifest_path = root / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        rollups = [item for item in manifest.get("generated_rollups", []) if item.get("date") != session_date.isoformat()]
        rollups.append({"date": session_date.isoformat(), "file": f"daily/{target.name}",
                        "checksum_sha256": sha256(target.read_bytes()).hexdigest(),
                        "data_quality": "VERIFIED_DAILY_OHLC_OBSERVED_EVENTS"})
        manifest["generated_rollups"] = sorted(rollups, key=lambda item: item["date"])
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                                 encoding="utf-8", newline="\n")
    return target


class DailyRollupScheduler:
    def __init__(self):
        self.stop_event = Event()
        self.thread = None
        self.completed_day = None

    def start(self):
        if self.thread and self.thread.is_alive(): return
        self.stop_event.clear()
        self.thread = Thread(target=self._run, name='daily-memory-rollup', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread: self.thread.join(timeout=10)

    def tick(self, now=None):
        now = (now or datetime.now(IST)).astimezone(IST)
        from datetime import time
        cutoff = time.fromisoformat(os.getenv('MEMORY_ROLLUP_AFTER','15:45'))
        root = memory_root()
        target = root/'daily'/f'{now.date()}.md'
        if now.time()<cutoff or self.completed_day == now.date(): return
        market = StoredMarketData()
        session = market.session(now.date())
        if not session or now<session[1] or not market.minutes(now.date()): return
        if target.exists():
            # Resume after a crash between immutable daily write and derived-memory sync.
            LiveSessionArchiveService(root).archive(now.date(), session, market.minutes(now.date()))
            path = target
        else:
            path = finalize_day(root,now.date())
        if market.database_url:
            from .bootstrap import prepare_memory
            prepare_memory(market.database_url)
        self.completed_day = now.date()
        logging.getLogger(__name__).info('daily_memory_created path=%s',path)

    def _run(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception:
                logging.getLogger(__name__).exception('daily_memory_rollup_pending_or_failed')
            self.stop_event.wait(60)
