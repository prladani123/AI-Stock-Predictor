from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterator

from .calendar import TradingCalendar, to_utc
from .models import ImportResult, QualityIssue
from .rate_limit import RateLimiter, with_retries
from .repository import Repository
from .validation import CandleValidator
from .zerodha import ReadOnlyZerodhaAdapter


def chunks(start: datetime, end: datetime, days: int) -> Iterator[tuple[datetime, datetime]]:
    current = start
    while current <= end:
        chunk_end = min(current + timedelta(days=days) - timedelta(minutes=1), end)
        yield current, chunk_end
        current = chunk_end + timedelta(minutes=1)


class HistoricalImporter:
    def __init__(
        self,
        adapter: ReadOnlyZerodhaAdapter,
        repository: Repository,
        calendar: TradingCalendar,
        *,
        validator: CandleValidator | None = None,
        rate_limiter: RateLimiter | None = None,
        chunk_days: int = 60,
        max_retries: int = 3,
        retry_sleep=lambda _: None,
        retry_jitter=lambda: 0.0,
    ) -> None:
        self.adapter = adapter
        self.repository = repository
        self.calendar = calendar
        self.validator = validator or CandleValidator()
        self.rate_limiter = rate_limiter or RateLimiter()
        self.chunk_days = chunk_days
        self.max_retries = max_retries
        self.retry_sleep = retry_sleep
        self.retry_jitter = retry_jitter

    def run(
        self,
        *,
        instrument_id: int,
        instrument_token: int,
        start: datetime,
        end: datetime,
    ) -> ImportResult:
        start = to_utc(start)
        end = to_utc(end)
        if end < start:
            raise ValueError("end must not precede start")
        job = self.repository.get_or_create_job(instrument_id, start, end)
        if job.status == "COMPLETED":
            return ImportResult(job.id, job.status, 0, 0, 0, 0, job.checkpoint_at, ())

        self.repository.mark_job_running(job.id)
        resume_at = max(start, job.checkpoint_at + timedelta(minutes=1)) if job.checkpoint_at else start
        all_issues = []
        received_total = inserted_total = duplicate_total = rejected_total = 0

        try:
            for chunk_start, chunk_end in chunks(resume_at, end, self.chunk_days):
                self.rate_limiter.wait()
                rows = with_retries(
                    lambda: self.adapter.historical_data(
                        instrument_token,
                        chunk_start,
                        chunk_end,
                        interval="minute",
                        continuous=False,
                        oi=True,
                    ),
                    attempts=self.max_retries,
                    sleep=self.retry_sleep,
                    jitter=self.retry_jitter,
                )
                sessions = self.calendar.sessions_between(chunk_start.date(), chunk_end.date())
                candles, issues = self.validator.validate(rows, sessions)
                inserted, existing = self.repository.insert_candles(instrument_id, job.id, candles)
                if existing:
                    issues.extend(
                        [
                            # Existing rows indicate a safe idempotent replay, not conflicting replacement.
                            # A count-only issue avoids one noisy row per minute.
                            QualityIssue(
                                "IDEMPOTENT_DUPLICATE",
                                "INFO",
                                f"{existing} candles already existed and were not overwritten",
                            )
                        ]
                    )
                self.repository.store_issues(job.id, instrument_id, issues)
                received_total += len(rows)
                inserted_total += inserted
                duplicate_total += existing + sum(issue.code == "DUPLICATE_TIMESTAMP" for issue in issues)
                rejected_total += sum(issue.severity == "ERROR" and issue.code != "MISSING_MINUTE" for issue in issues)
                all_issues.extend(issues)
                self.repository.update_checkpoint(job.id, chunk_end, len(rows), inserted)
            self.repository.complete_job(job.id)
            return ImportResult(
                job.id,
                "COMPLETED",
                received_total,
                inserted_total,
                duplicate_total,
                rejected_total,
                end,
                tuple(all_issues),
            )
        except Exception as exc:
            self.repository.fail_job(job.id, type(exc).__name__, str(exc))
            raise
