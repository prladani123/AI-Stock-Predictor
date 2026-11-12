from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import TradingSession

try:
    IST = ZoneInfo("Asia/Kolkata")
except ZoneInfoNotFoundError:  # Windows without the optional tzdata wheel; India has no DST.
    IST = timezone(timedelta(hours=5, minutes=30), "Asia/Kolkata")
UTC = timezone.utc


def ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value


def to_utc(value: datetime) -> datetime:
    return ensure_aware(value).astimezone(UTC)


class TradingCalendar:
    def __init__(self, sessions: list[TradingSession] | None = None) -> None:
        self._sessions = {session.session_date: session for session in sessions or []}

    @staticmethod
    def regular_session(session_date: date) -> TradingSession:
        if session_date.weekday() >= 5:
            return TradingSession(
                session_date,
                datetime.combine(session_date, time(9, 15), IST).astimezone(UTC),
                datetime.combine(session_date, time(15, 30), IST).astimezone(UTC),
                "CLOSED",
            )
        return TradingSession(
            session_date,
            datetime.combine(session_date, time(9, 15), IST).astimezone(UTC),
            datetime.combine(session_date, time(15, 30), IST).astimezone(UTC),
        )

    def session(self, session_date: date) -> TradingSession | None:
        """Return only an explicitly stored session; unknown dates stay unknown."""
        stored = self._sessions.get(session_date)
        return stored if stored and stored.status != 'UNKNOWN' else None

    def sessions_between(self, start: date, end: date, *, require_complete: bool = True) -> list[TradingSession]:
        result: list[TradingSession] = []
        unknown: list[date] = []
        current = start
        while current <= end:
            session = self.session(current)
            if session is None:
                unknown.append(current)
            elif session.status in {"OPEN", "SPECIAL"}:
                result.append(session)
            current += timedelta(days=1)
        if require_complete and unknown:
            values = ", ".join(item.isoformat() for item in unknown)
            raise ValueError(f"Unknown trading calendar session(s): {values}")
        return result

    @staticmethod
    def expected_minutes(session: TradingSession) -> tuple[datetime, ...]:
        if session.status not in {"OPEN", "SPECIAL"}:
            return ()
        current = to_utc(session.open_at)
        close_at = to_utc(session.close_at)
        result: list[datetime] = []
        while current < close_at:
            result.append(current)
            current += timedelta(minutes=1)
        return tuple(result)
