"""Best-effort live research capture; never on the production decision path."""
from __future__ import annotations

from datetime import datetime
import logging
import os
from threading import Lock, Thread
from typing import Any

from .banknifty_time_machine import BankniftyTimeMachine
from .external_research import PROSPECTIVE
from .kronos_experiment import KronosForecastAdapter
from .models import Candle

log = logging.getLogger(__name__)
_lock = Lock()
_last_reference: tuple[str, int, datetime] | None = None


def _enabled(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() == "true"


def _run(repository: Any, state: Any, reference: datetime, revision_id: str,
         instrument_id: int) -> None:
    try:
        minutes = state.minutes("INDEX")
        candles = [Candle(item.minute_at, item.open, item.high, item.low, item.close,
                          item.volume_delta or 0, item.oi_close) for item in minutes
                   if item.minute_at <= reference]
        if _enabled("TIME_MACHINE_ENABLED"):
            machine = BankniftyTimeMachine(repository,
                sample_paths=int(os.getenv("TIME_MACHINE_PATHS", "10000")),
                slot_window_minutes=int(os.getenv("TIME_MACHINE_SLOT_WINDOW_MINUTES", "2")),
                minimum_prior_sessions=int(os.getenv("TIME_MACHINE_MIN_PRIOR_SESSIONS", "20")),
                seed=int(os.getenv("TIME_MACHINE_SEED", "1729")))
            result = machine.run(revision_id=revision_id, instrument_id=instrument_id,
                                 reference_minute=reference, point_in_time_status=PROSPECTIVE)
            if result.status == "COMPLETED":
                repository.save_experimental_output(result, revision_id, instrument_id)
        if _enabled("KRONOS_RESEARCH_ENABLED"):
            adapter = KronosForecastAdapter(
                sample_paths=int(os.getenv("KRONOS_SAMPLE_PATHS", "8")),
                seed=int(os.getenv("KRONOS_SEED", "1729")),
                timeout_seconds=float(os.getenv("KRONOS_TIMEOUT_SECONDS", "90")))
            result = adapter.forecast(candles, as_of=reference,
                                      point_in_time_status=PROSPECTIVE)
            if result.status == "COMPLETED":
                repository.save_experimental_output(result, revision_id, instrument_id)
    except Exception:
        log.exception("EXTERNAL_RESEARCH_UNAVAILABLE")
    finally:
        _lock.release()


def capture_best_effort(repository: Any, state: Any, reference: datetime,
                        revision_id: str, instrument_id: int) -> bool:
    """Start one bounded background capture. Returns False when already running."""
    global _last_reference
    key = (revision_id, instrument_id, reference)
    if _last_reference == key:
        return False
    if not (_enabled("TIME_MACHINE_ENABLED") or _enabled("KRONOS_RESEARCH_ENABLED")):
        return False
    if not _lock.acquire(blocking=False):
        return False
    _last_reference = key
    try:
        Thread(target=_run, args=(repository, state, reference, revision_id, instrument_id),
               name="external-research", daemon=True).start()
    except Exception:
        _lock.release()
        raise
    return True
