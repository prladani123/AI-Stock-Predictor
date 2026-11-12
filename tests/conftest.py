from __future__ import annotations

from datetime import date, datetime, time, timezone
from pathlib import Path

import pytest

from banknifty_foundation.calendar import IST
from banknifty_foundation.models import TradingSession


@pytest.fixture
def fixture_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture
def short_session() -> TradingSession:
    session_date = date(2024, 7, 24)
    return TradingSession(
        session_date,
        datetime.combine(session_date, time(9, 15), IST).astimezone(timezone.utc),
        datetime.combine(session_date, time(9, 21), IST).astimezone(timezone.utc),
    )

