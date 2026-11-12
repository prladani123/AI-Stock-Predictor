from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from typing import Any, Callable, Sequence

import numpy as np

from .big_move_labels import BigMoveThresholdPolicy, default_big_move_policy
from .calendar import IST, TradingCalendar
from .external_research import (ExternalForecastResult, HISTORICAL_COUNTERFACTUAL,
                                TIME_MACHINE_MODEL_NAME, aggregate_paths, checksum_payload,
                                median_path, path_fan_quantiles)
from .models import Candle, TradingSession


INDIA_TRADER_UPSTREAM_SHA = "cd9767f40241ac914cd13e2a06fd3a60b3af42d4"
NORMAL_SESSION_MINUTES = 375


class BankniftyTimeMachine:
    """Time-of-day Monte Carlo research using only complete prior sessions.

    The adapted concept is from india-trader-skill/bharat-timemachine. It uses
    our accepted BANKNIFTY candles only; no Yahoo data, broker, order, budget,
    position sizing, stop, target, or execution code is imported.
    """

    def __init__(
        self, repository: Any, *, policy: BigMoveThresholdPolicy | None = None,
        sample_paths: int = 10_000, slot_window_minutes: int = 2,
        minimum_prior_sessions: int = 20, seed: int = 1729,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if sample_paths < 1 or sample_paths > 200_000:
            raise ValueError("sample paths must be between 1 and 200000")
        if slot_window_minutes < 0 or slot_window_minutes > 10:
            raise ValueError("slot window must be between 0 and 10 minutes")
        self.repository = repository
        self.policy = policy or default_big_move_policy()
        self.sample_paths = sample_paths
        self.slot_window_minutes = slot_window_minutes
        # Product policy: environment/CLI may increase but never reduce this.
        self.minimum_prior_sessions = max(20, int(minimum_prior_sessions))
        self.seed = seed
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _configuration(self) -> dict[str, Any]:
        return {
            "samplePaths": self.sample_paths,
            "slotWindowMinutes": self.slot_window_minutes,
            "minimumPriorSessions": self.minimum_prior_sessions,
            "seed": self.seed,
            "thresholdPolicyVersion": self.policy.version,
            "futureBars": 30,
            "mode": "OHLC_ONLY",
        }

    def _status(self, status: str, reference: datetime, *, detail: str | None = None,
                prior_sessions: int = 0, checksum: str | None = None,
                point_in_time_status: str = HISTORICAL_COUNTERFACTUAL,
                replay_pool_revision_id: str | None = None,
                input_checksum: str | None = None) -> ExternalForecastResult:
        now = self.clock()
        summary = {
            "researchStatus": status,
            "statusDetail": detail,
            "priorSessionCount": prior_sessions,
            "samplingPoolChecksum": checksum,
            "seed": self.seed,
            "slotWindowMinutes": self.slot_window_minutes,
            "pathCount": 0,
            "futureBars": 30,
            "mode": "OHLC_ONLY",
            "volumeStatus": "INDEX_VOLUME_NOT_USED",
            "usedByPriceCoreV2": False,
        }
        return ExternalForecastResult(TIME_MACHINE_MODEL_NAME, "time-of-day-v1", reference,
            reference, now, now, reference, point_in_time_status, status, {}, (), summary,
            replay_pool_revision_id=replay_pool_revision_id,
            input_checksum=input_checksum, configuration=self._configuration(),
            upstream_revision=INDIA_TRADER_UPSTREAM_SHA, seed=self.seed,
            threshold_policy_version=self.policy.version,
            sampling_pool_checksum=checksum)

    def run(self, *, revision_id: str, instrument_id: int, reference_minute: datetime,
            point_in_time_status: str = HISTORICAL_COUNTERFACTUAL,
            current_candles: Sequence[Candle] | None = None,
            current_session: TradingSession | None = None) -> ExternalForecastResult:
        revision = self.repository.get_revision(revision_id)
        if revision.status != "ACCEPTED":
            raise ValueError("Time Machine requires an ACCEPTED dataset revision")
        if instrument_id not in revision.instrument_ids:
            raise ValueError("instrument is not part of the accepted revision")
        instrument = self.repository.get_instrument(instrument_id)
        if not (instrument.exchange == "NSE" and instrument.instrument_type == "EQ"
                and "BANK" in instrument.name.upper()):
            raise ValueError("Time Machine requires the BANKNIFTY index")
        reference = reference_minute
        if reference.tzinfo is None or reference.utcoffset() is None:
            raise ValueError("reference_minute must be timezone-aware")
        reference = reference.astimezone(timezone.utc).replace(second=0, microsecond=0)
        sessions = {item.session_date: item for item in self.repository.sessions_between(
            revision.session_start, revision.session_end)
            if item.status == "OPEN"}
        reference_date = reference.astimezone(IST).date()
        session = current_session or sessions.get(reference_date)
        if session is None:
            return self._status("SESSION_BOUNDARY", reference,
                                detail="reference date has no normal OPEN session",
                                point_in_time_status=point_in_time_status,
                                replay_pool_revision_id=revision_id)
        if session.session_date != reference_date or session.status != "OPEN":
            return self._status("SESSION_BOUNDARY", reference,
                                detail="special or mismatched session is not replayed",
                                point_in_time_status=point_in_time_status,
                                replay_pool_revision_id=revision_id)
        if reference < session.open_at or reference + timedelta(minutes=30) >= session.close_at:
            return self._status("SESSION_BOUNDARY", reference,
                                detail="reference minute leaves fewer than 30 session minutes",
                                point_in_time_status=point_in_time_status,
                                replay_pool_revision_id=revision_id)
        expected = TradingCalendar.expected_minutes(session)
        if len(expected) != NORMAL_SESSION_MINUTES:
            return self._status("SESSION_BOUNDARY", reference,
                                detail="rectangular replay requires a normal 375-minute session",
                                point_in_time_status=point_in_time_status,
                                replay_pool_revision_id=revision_id)
        merged: dict[datetime, Candle] = {
            item.timestamp: item for item in
            self.repository.revision_candles(revision_id, instrument_id)
            if current_candles is None or item.timestamp.astimezone(IST).date() < reference_date}
        merged.update({item.timestamp: item for item in (current_candles or ())})
        candles = sorted(merged.values(), key=lambda item: item.timestamp)
        by_date: dict[Any, dict[datetime, Candle]] = {}
        for candle in candles:
            by_date.setdefault(candle.timestamp.astimezone(IST).date(), {})[candle.timestamp] = candle
        current = by_date.get(reference_date, {})
        known = [current[item] for item in expected if item in current and item <= reference]
        if len(known) != sum(item <= reference for item in expected):
            return self._status("INPUT_GAP", reference, detail="incomplete current-session input",
                point_in_time_status=point_in_time_status, replay_pool_revision_id=revision_id)
        if reference not in current:
            return self._status("INSUFFICIENT_HISTORY", reference,
                                detail="reference candle is missing",
                                point_in_time_status=point_in_time_status,
                                replay_pool_revision_id=revision_id)
        required_history = self.policy.volatility_lookback_minutes + 1
        if len(known) < required_history:
            return self._status("INSUFFICIENT_HISTORY", reference,
                                detail=f"at least {required_history} known closes are required",
                                point_in_time_status=point_in_time_status,
                                replay_pool_revision_id=revision_id)
        input_checksum = checksum_payload({
            "model": TIME_MACHINE_MODEL_NAME,
            "instrument": instrument_id,
            "referenceMinute": reference.isoformat(),
            "session": {"open": session.open_at.isoformat(), "close": session.close_at.isoformat()},
            "known": [{"timestamp": item.timestamp.isoformat(), "open": str(item.open),
                       "high": str(item.high), "low": str(item.low), "close": str(item.close)}
                      for item in known],
        })
        prior_dates = sorted(day for day in by_date if day < reference_date and day in sessions)
        complete: list[tuple[Any, list[Candle]]] = []
        for day in prior_dates:
            session_for_day = sessions[day]
            day_expected = TradingCalendar.expected_minutes(session_for_day)
            stored = by_date.get(day, {})
            if (len(day_expected) == NORMAL_SESSION_MINUTES
                    and session_for_day.open_at.astimezone(IST).time() == session.open_at.astimezone(IST).time()
                    and session_for_day.close_at.astimezone(IST).time() == session.close_at.astimezone(IST).time()
                    and all(item in stored for item in day_expected)):
                complete.append((day, [stored[item] for item in day_expected]))
        if len(complete) < self.minimum_prior_sessions:
            return self._status("INSUFFICIENT_PRIOR_SESSIONS", reference,
                                detail=f"found {len(complete)} complete normal prior sessions, "
                                       f"require {self.minimum_prior_sessions}",
                                prior_sessions=len(complete),
                                point_in_time_status=point_in_time_status,
                                replay_pool_revision_id=revision_id,
                                input_checksum=input_checksum)
        values = np.asarray([
            [[float(candle.open), float(candle.high), float(candle.low), float(candle.close)]
             for candle in session_candles]
            for _, session_candles in complete
        ], dtype=np.float64)
        if np.any(values <= 0) or not np.all(np.isfinite(values)):
            return self._status("FAILED", reference, detail="invalid OHLC in prior-session pool",
                                prior_sessions=len(complete), point_in_time_status=point_in_time_status,
                                replay_pool_revision_id=revision_id, input_checksum=input_checksum)
        previous_close = values[:, :-1, 3]
        pool = values[:, 1:, :] / previous_close[:, :, None]
        if np.any(pool <= 0) or not np.all(np.isfinite(pool)):
            return self._status("FAILED", reference, detail="invalid ratio in prior-session pool",
                                prior_sessions=len(complete), point_in_time_status=point_in_time_status,
                                replay_pool_revision_id=revision_id, input_checksum=input_checksum)
        checksum = hashlib.sha256(
            b"BANKNIFTY_TIME_MACHINE_V1|" +
            "|".join(day.isoformat() for day, _ in complete).encode("ascii") + b"|" +
            np.round(pool, 10).astype("<f8").tobytes()
        ).hexdigest()
        path_count = int(self.sample_paths)
        bar_count = len(expected)
        reference_slot = int((reference - session.open_at).total_seconds() // 60)
        last_slot = bar_count - 1
        steps = 30
        future_slots = np.arange(reference_slot + 1, reference_slot + steps + 1)
        if future_slots[-1] > last_slot:
            return self._status("SESSION_BOUNDARY", reference,
                                detail="forecast crosses the session close",
                                prior_sessions=len(complete), checksum=checksum,
                                point_in_time_status=point_in_time_status,
                                replay_pool_revision_id=revision_id,
                                input_checksum=input_checksum)
        rng = np.random.default_rng(self.seed)
        offsets = rng.integers(-self.slot_window_minutes, self.slot_window_minutes + 1,
                               size=(path_count, steps))
        slots = np.clip(future_slots[None, :] + offsets, 1, last_slot)
        session_indices = rng.integers(0, len(complete), size=(path_count, steps))
        ratios = pool[session_indices, slots - 1]
        reference_price = float(current[reference].close)
        close_ratios = ratios[:, :, 3]
        closes = reference_price * np.cumprod(close_ratios, axis=1)
        previous = np.concatenate(
            [np.full((path_count, 1), reference_price, dtype=np.float64), closes[:, :-1]], axis=1)
        opens = previous * ratios[:, :, 0]
        highs = previous * ratios[:, :, 1]
        lows = previous * ratios[:, :, 2]
        highs = np.maximum.reduce([highs, opens, closes])
        lows = np.minimum.reduce([lows, opens, closes])
        timestamps = [reference + timedelta(minutes=offset) for offset in range(1, steps + 1)]
        paths: list[list[dict[str, Any]]] = []
        for path_index in range(path_count):
            paths.append([
                {"timestamp": timestamps[offset].isoformat(), "open": float(opens[path_index, offset]),
                 "high": float(highs[path_index, offset]), "low": float(lows[path_index, offset]),
                 "close": float(closes[path_index, offset])}
                for offset in range(steps)
            ])
        horizons = aggregate_paths(paths, reference_price=reference_price,
            known_closes=[float(item.close) for item in known], policy=self.policy)
        now = self.clock()
        fan = path_fan_quantiles(paths)
        summary = {
            "researchStatus": "COMPLETED",
            "priorSessionCount": len(complete),
            "firstPriorSession": complete[0][0].isoformat(),
            "lastPriorSession": complete[-1][0].isoformat(),
            "samplingPoolChecksum": checksum,
            "seed": self.seed,
            "seedPolicy": "NUMPY_PCG64_DEFAULT_RNG",
            "slotWindowMinutes": self.slot_window_minutes,
            "minimumPriorSessions": self.minimum_prior_sessions,
            "referenceSlot": reference_slot,
            "pathCount": path_count,
            "futureBars": steps,
            "mode": "OHLC_ONLY",
            "volumeStatus": "INDEX_VOLUME_NOT_USED",
            "pathFanQuantiles": fan,
            "upstreamRevision": INDIA_TRADER_UPSTREAM_SHA,
            "replayPoolRevisionId": revision_id,
            "inputChecksum": input_checksum,
            "sampling": {"method": "TIME_OF_DAY_BAR_RATIO_REPLAY",
                          "windowMinutes": self.slot_window_minutes,
                          "strictlyPriorSessions": True,
                          "normalSessionOnly": True},
            "usedByPriceCoreV2": False,
        }
        return ExternalForecastResult(TIME_MACHINE_MODEL_NAME, "time-of-day-v1", reference,
            reference, now, now, reference, point_in_time_status, "COMPLETED", horizons,
            median_path(paths), summary, replay_pool_revision_id=revision_id,
            input_checksum=input_checksum,
            reference_state_checksum=checksum_payload({
                "reference": {"timestamp": reference.isoformat(),
                              "open": str(current[reference].open),
                              "high": str(current[reference].high),
                              "low": str(current[reference].low),
                              "close": str(current[reference].close)},
                "session": {"open": session.open_at.isoformat(), "close": session.close_at.isoformat()},
            }),
            configuration=self._configuration(), upstream_revision=INDIA_TRADER_UPSTREAM_SHA,
            seed=self.seed, threshold_policy_version=self.policy.version,
            sampling_pool_checksum=checksum)
