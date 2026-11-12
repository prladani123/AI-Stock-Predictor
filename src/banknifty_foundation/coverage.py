from __future__ import annotations

from .calendar import TradingCalendar
from .models import CoverageReport, TradingSession
from .repository import Repository


class CoverageReporter:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def generate(self, instrument_id: int, session: TradingSession) -> CoverageReport:
        expected = set(TradingCalendar.expected_minutes(session))
        stored = self.repository.candle_times(instrument_id, session.open_at, session.close_at)
        missing = tuple(sorted(expected - stored))
        percent = round((len(stored & expected) / len(expected) * 100.0), 2) if expected else 100.0
        return CoverageReport(
            instrument_id=instrument_id,
            session_date=session.session_date,
            expected_minutes=len(expected),
            stored_minutes=len(stored & expected),
            missing_minutes=missing,
            coverage_percent=percent,
            issue_counts=self.repository.issue_counts(instrument_id, session.session_date),
        )

