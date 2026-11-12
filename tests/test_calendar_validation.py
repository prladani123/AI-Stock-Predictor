from __future__ import annotations

import json

from banknifty_foundation.calendar import TradingCalendar
from banknifty_foundation.validation import CandleValidator


def test_regular_nse_session_has_375_minutes():
    session = TradingCalendar.regular_session(__import__("datetime").date(2024, 7, 24))
    assert len(TradingCalendar.expected_minutes(session)) == 375
    assert session.open_at.tzinfo is not None
    assert session.open_at.utcoffset().total_seconds() == 0


def test_validator_detects_duplicate_missing_invalid_and_out_of_order(fixture_dir, short_session):
    rows = json.loads((fixture_dir / "candles_260105.json").read_text(encoding="utf-8"))
    candles, issues = CandleValidator().validate(rows, [short_session])
    codes = [issue.code for issue in issues]
    assert len(candles) == 4
    assert "DUPLICATE_TIMESTAMP" in codes
    assert "OUT_OF_ORDER" in codes
    assert "INVALID_OHLC" in codes
    assert codes.count("MISSING_MINUTE") == 2
    assert "INCOMPLETE_SESSION_COVERAGE" in codes
    assert [item.timestamp for item in candles] == sorted(item.timestamp for item in candles)


def test_validator_detects_conflicting_duplicate_and_naive_timestamp(short_session):
    rows = [
        {"date": "2024-07-24T09:15:00+05:30", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1},
        {"date": "2024-07-24T09:15:00+05:30", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1},
        {"date": "2024-07-24T09:16:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1},
    ]
    _, issues = CandleValidator().validate(rows, [short_session])
    codes = [item.code for item in issues]
    assert "CONFLICTING_DUPLICATE_CANDLE" in codes
    assert "UNEXPECTED_TIMEZONE" in codes
