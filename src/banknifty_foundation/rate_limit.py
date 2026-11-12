from __future__ import annotations

import random
import time
from enum import Enum
from typing import Callable, TypeVar

T = TypeVar("T")


class RetryCategory(str, Enum):
    TRANSIENT = "TRANSIENT"
    RATE_LIMIT = "RATE_LIMIT"
    AUTHENTICATION = "AUTHENTICATION"
    PERMANENT_INPUT = "PERMANENT_INPUT"
    PERMANENT = "PERMANENT"


def classify_exception(exc: Exception) -> RetryCategory:
    """Classify Kite and standard exceptions without importing the optional SDK."""
    name = type(exc).__name__
    message = str(exc).lower()
    code = getattr(exc, "code", None)
    if name in {"TokenException", "PermissionException"}:
        return RetryCategory.AUTHENTICATION
    if name == "InputException" or isinstance(exc, (TypeError, ValueError)):
        return RetryCategory.PERMANENT_INPUT
    if code == 429 or "rate limit" in message or "too many requests" in message:
        return RetryCategory.RATE_LIMIT
    if name in {"NetworkException", "DataException"} or isinstance(
        exc, (TimeoutError, ConnectionError)
    ):
        return RetryCategory.TRANSIENT
    return RetryCategory.PERMANENT


class RateLimiter:
    def __init__(
        self,
        requests_per_second: float = 3.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        self._interval = 1.0 / requests_per_second
        self._clock = clock
        self._sleep = sleep
        self._last_request: float | None = None

    def wait(self) -> None:
        now = self._clock()
        if self._last_request is not None:
            delay = self._interval - (now - self._last_request)
            if delay > 0:
                self._sleep(delay)
                now = self._clock()
        self._last_request = now


def with_retries(
    operation: Callable[[], T],
    *,
    attempts: int,
    classifier: Callable[[Exception], RetryCategory] = classify_exception,
    sleep: Callable[[float], None] = time.sleep,
    jitter: Callable[[], float] = random.random,
) -> T:
    if attempts < 1:
        raise ValueError("attempts must be positive")
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except Exception as exc:
            category = classifier(exc)
            if category not in {RetryCategory.TRANSIENT, RetryCategory.RATE_LIMIT}:
                raise
            if attempt == attempts:
                raise
            base = 2 if category == RetryCategory.RATE_LIMIT else 1
            sleep(min(base * 2 ** (attempt - 1), 8) + jitter() * 0.1)
    raise AssertionError("unreachable")
