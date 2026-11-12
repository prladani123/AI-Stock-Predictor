from __future__ import annotations

from datetime import timedelta

from banknifty_foundation.calendar import TradingCalendar
from banknifty_foundation.fixtures import FixtureZerodhaClient
from banknifty_foundation.importer import HistoricalImporter
from banknifty_foundation.rate_limit import (
    RateLimiter,
    RetryCategory,
    classify_exception,
    with_retries,
)
from banknifty_foundation.repository import MemoryRepository
from banknifty_foundation.zerodha import ReadOnlyZerodhaAdapter


class NoWaitLimiter:
    def wait(self):
        return None


def test_fixture_import_is_restartable_and_idempotent(fixture_dir, short_session):
    client = FixtureZerodhaClient(fixture_dir)
    repository = MemoryRepository()
    importer = HistoricalImporter(
        ReadOnlyZerodhaAdapter(client),
        repository,
        TradingCalendar([short_session]),
        rate_limiter=NoWaitLimiter(),
        chunk_days=1,
    )
    first = importer.run(
        instrument_id=1,
        instrument_token=1100001,
        start=short_session.open_at,
        end=short_session.close_at - timedelta(minutes=1),
    )
    calls_after_first = client.historical_calls
    second = importer.run(
        instrument_id=1,
        instrument_token=1100001,
        start=short_session.open_at,
        end=short_session.close_at - timedelta(minutes=1),
    )
    assert first.status == "COMPLETED"
    assert first.rows_inserted == 6
    assert second.rows_inserted == 0
    assert client.historical_calls == calls_after_first
    assert repository.jobs[first.job_id].checkpoint_at == short_session.close_at - timedelta(minutes=1)


def test_retry_policy_retries_transient_failure_without_real_sleep():
    calls = 0
    sleeps = []

    def operation():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise TimeoutError("temporary")
        return "ok"

    assert with_retries(operation, attempts=3, sleep=sleeps.append, jitter=lambda: 0.0) == "ok"
    assert calls == 3
    assert sleeps == [1.0, 2.0]


def test_rate_limiter_enforces_interval():
    times = iter([0.0, 0.1, 0.5])
    sleeps = []
    limiter = RateLimiter(2, clock=lambda: next(times), sleep=sleeps.append)
    limiter.wait()
    limiter.wait()
    assert sleeps == [0.4]


def test_retry_classification_distinguishes_broker_failure_types():
    TokenException = type("TokenException", (Exception,), {})
    InputException = type("InputException", (Exception,), {})
    NetworkException = type("NetworkException", (Exception,), {})
    RateLimitException = type("RateLimitException", (Exception,), {"code": 429})
    assert classify_exception(TokenException()) == RetryCategory.AUTHENTICATION
    assert classify_exception(InputException()) == RetryCategory.PERMANENT_INPUT
    assert classify_exception(NetworkException()) == RetryCategory.TRANSIENT
    assert classify_exception(RateLimitException()) == RetryCategory.RATE_LIMIT
    calls = 0

    def auth_failure():
        nonlocal calls
        calls += 1
        raise TokenException("expired")

    with __import__("pytest").raises(TokenException):
        with_retries(auth_failure, attempts=3)
    assert calls == 1
