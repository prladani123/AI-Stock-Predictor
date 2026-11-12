"""Best-effort live research capture; never on the production decision path."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import logging
import os
from threading import Lock, Thread
from typing import Any

from .banknifty_time_machine import BankniftyTimeMachine
from .external_research import PROSPECTIVE, prospective_status
from .kronos_experiment import KronosForecastAdapter
from .models import Candle

log = logging.getLogger(__name__)
_locks = {"timeMachine": Lock(), "kronos": Lock()}
_last_success: dict[str, Any] = {}


def _enabled(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() == "true"


def _candles(state: Any, reference: datetime) -> list[Candle]:
    return [Candle(item.minute_at, item.open, item.high, item.low, item.close,
                   item.volume_delta or 0, item.oi_close)
            for item in state.minutes("INDEX") if item.minute_at <= reference]


def _classify(result: Any, decision_at: datetime | None,
              prediction_ids: tuple[str, ...]) -> Any:
    if decision_at is None:
        status = PROSPECTIVE
    else:
        status = prospective_status(result.available_at, decision_at)
    return replace(result, point_in_time_status=status, decision_at=decision_at,
                   prediction_ids=tuple(sorted(prediction_ids)))


def _run(repository: Any, state: Any, reference: datetime,
         replay_pool_revision_id: str | None, instrument_id: int,
         decision_at: datetime | None, prediction_ids: tuple[str, ...],
         key: Any, channel: str) -> None:
    outcomes: dict[str, str] = {}
    try:
        candles = _candles(state, reference)
    except Exception:
        log.exception("EXTERNAL_RESEARCH_MARKET_INPUT_UNAVAILABLE")
        candles = []
    if channel == "timeMachine":
        if not replay_pool_revision_id:
            outcomes["timeMachine"] = "REPLAY_POOL_NOT_CONFIGURED"
            log.warning("TIME_MACHINE_REPLAY_POOL_NOT_CONFIGURED")
        else:
            try:
                machine = BankniftyTimeMachine(repository,
                    sample_paths=int(os.getenv("TIME_MACHINE_PATHS", "10000")),
                    slot_window_minutes=int(os.getenv("TIME_MACHINE_SLOT_WINDOW_MINUTES", "2")),
                    minimum_prior_sessions=int(os.getenv("TIME_MACHINE_MIN_PRIOR_SESSIONS", "20")),
                    seed=int(os.getenv("TIME_MACHINE_SEED", "1729")))
                result = machine.run(revision_id=replay_pool_revision_id,
                    instrument_id=instrument_id, reference_minute=reference,
                    point_in_time_status=PROSPECTIVE, current_candles=candles,
                    current_session=state.session)
                result = _classify(result, decision_at, prediction_ids)
                outcomes["timeMachine"] = result.status
                if result.status == "COMPLETED":
                    repository.save_experimental_output(result, None, instrument_id,
                        replay_pool_revision_id=replay_pool_revision_id)
            except Exception:
                outcomes["timeMachine"] = "FAILED"
                log.exception("TIME_MACHINE_RESEARCH_CAPTURE_FAILED")
    if channel == "kronos":
        try:
            adapter = KronosForecastAdapter(
                sample_paths=int(os.getenv("KRONOS_SAMPLE_PATHS", "8")),
                seed=int(os.getenv("KRONOS_SEED", "1729")),
                timeout_seconds=float(os.getenv("KRONOS_TIMEOUT_SECONDS", "90")))
            result = adapter.forecast(candles, as_of=reference,
                point_in_time_status=PROSPECTIVE,
                session_open=state.session.open_at if state.session else None,
                session_close=state.session.close_at if state.session else None)
            result = _classify(result, decision_at, prediction_ids)
            outcomes["kronos"] = result.status
            if result.status == "COMPLETED":
                repository.save_experimental_output(result, None, instrument_id)
        except Exception:
            outcomes["kronos"] = "FAILED"
            log.exception("KRONOS_RESEARCH_CAPTURE_FAILED")
    log.info("EXTERNAL_RESEARCH_CAPTURE reference=%s outcomes=%s",
             reference.isoformat(), outcomes)
    if outcomes.get(channel) == "COMPLETED":
        _last_success[channel] = key


def _worker(*args, channel: str) -> None:
    try:
        _run(*args, channel)
    except Exception:
        log.exception("EXTERNAL_RESEARCH_WORKER_FAILED channel=%s", channel)
    finally:
        _locks[channel].release()


def capture_best_effort(repository: Any, state: Any, reference: datetime,
                        revision_id: str | None, instrument_id: int,
                        *, decision_at: datetime | None = None,
                        prediction_ids: tuple[str, ...] = (),
                        replay_pool_revision_id: str | None = None) -> bool:
    """One bounded worker per channel; a slow/failing peer cannot block retries."""
    if decision_at is None or not prediction_ids:
        return False
    prediction_ids = tuple(sorted(str(value) for value in prediction_ids))
    started = False
    for channel, prefix in (("timeMachine", "TIME_MACHINE"), ("kronos", "KRONOS")):
        if not _enabled("TIME_MACHINE_ENABLED" if channel == "timeMachine" else "KRONOS_RESEARCH_ENABLED"):
            continue
        config = tuple(sorted((k, v) for k, v in os.environ.items() if k.startswith(prefix)))
        key = (instrument_id, reference, decision_at, prediction_ids,
               replay_pool_revision_id if channel == "timeMachine" else None, config)
        if _last_success.get(channel) == key or not _locks[channel].acquire(blocking=False):
            continue
        try:
            Thread(target=_worker, args=(repository, state, reference, replay_pool_revision_id,
                   instrument_id, decision_at, prediction_ids, key), kwargs={"channel": channel},
                   name="external-" + channel, daemon=True).start()
            started = True
        except Exception:
            _locks[channel].release()
            log.exception("EXTERNAL_RESEARCH_THREAD_FAILED channel=%s", channel)
    return started


def retry_committed(repository: Any, state: Any, reference: datetime,
                    instrument_id: int, prediction_ids: tuple[str, ...]) -> bool:
    """Retry using the ORIGINAL journal decision, never a retry wall clock."""
    if not (_enabled("TIME_MACHINE_ENABLED") or _enabled("KRONOS_RESEARCH_ENABLED")):
        return False
    with repository._connection() as db:
        rows = db.execute("""
            SELECT prediction_id, decision_at FROM big_move_evidence_journal
            WHERE prediction_id = ANY(%s) AND instrument_id=%s AND market_as_of=%s
            ORDER BY decision_at, prediction_id
        """, (list(prediction_ids), instrument_id, reference)).fetchall()
    groups: dict[datetime, list[str]] = {}
    for prediction_id, decision_at in rows:
        groups.setdefault(decision_at, []).append(prediction_id)
    started = False
    for decision_at, ids in groups.items():
        started = capture_best_effort(repository, state, reference, None, instrument_id,
            decision_at=decision_at, prediction_ids=tuple(ids),
            replay_pool_revision_id=os.getenv("TIME_MACHINE_REPLAY_POOL_REVISION_ID") or None) or started
    return started
