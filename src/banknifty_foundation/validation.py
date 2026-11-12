from __future__ import annotations

from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .calendar import IST, TradingCalendar, to_utc
from .models import Candle, QualityIssue, TradingSession


def parse_candle(row: dict[str, Any]) -> Candle:
    timestamp = row.get("date", row.get("timestamp"))
    if isinstance(timestamp, str):
        timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if not isinstance(timestamp, datetime):
        raise ValueError("candle timestamp is missing or invalid")
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("unexpected timezone: timestamp is naive")
    allowed_offsets = {0, int(IST.utcoffset(timestamp).total_seconds())}
    if int(timestamp.utcoffset().total_seconds()) not in allowed_offsets:
        raise ValueError("unexpected timezone: expected UTC or Asia/Kolkata")
    return Candle(
        timestamp=to_utc(timestamp).replace(second=0, microsecond=0),
        open=Decimal(str(row["open"])),
        high=Decimal(str(row["high"])),
        low=Decimal(str(row["low"])),
        close=Decimal(str(row["close"])),
        volume=int(row.get("volume", 0)),
        oi=int(row["oi"]) if row.get("oi") is not None else None,
    )


class CandleValidator:
    def validate(
        self, rows: list[dict[str, Any]], sessions: list[TradingSession]
    ) -> tuple[list[Candle], list[QualityIssue]]:
        issues: list[QualityIssue] = []
        parsed: list[Candle] = []
        previous: datetime | None = None
        seen: dict[datetime, Candle] = {}

        for index, row in enumerate(rows):
            try:
                candle = parse_candle(row)
            except (ValueError, KeyError, TypeError, InvalidOperation) as exc:
                code = "UNEXPECTED_TIMEZONE" if "timezone" in str(exc) else "INVALID_ROW"
                issues.append(
                    QualityIssue(code, "ERROR", f"Row {index} could not be parsed: {exc}", details={"row": index})
                )
                continue

            if previous is not None and candle.timestamp < previous:
                issues.append(
                    QualityIssue(
                        "OUT_OF_ORDER",
                        "WARNING",
                        "Candle timestamp is earlier than the preceding response row",
                        candle.timestamp,
                        {"previous": previous.isoformat()},
                    )
                )
            previous = candle.timestamp

            if candle.timestamp in seen:
                duplicate_code = (
                    "DUPLICATE_TIMESTAMP"
                    if seen[candle.timestamp] == candle
                    else "CONFLICTING_DUPLICATE_CANDLE"
                )
                issues.append(
                    QualityIssue(
                        duplicate_code,
                        "ERROR" if duplicate_code.startswith("CONFLICTING") else "WARNING",
                        "Duplicate candle in broker response",
                        candle.timestamp,
                    )
                )
                continue
            seen[candle.timestamp] = candle

            if min(candle.open, candle.high, candle.low, candle.close) <= 0:
                issues.append(QualityIssue("INVALID_OHLC", "ERROR", "OHLC values must be positive", candle.timestamp))
                continue
            if candle.high < max(candle.open, candle.low, candle.close) or candle.low > min(
                candle.open, candle.high, candle.close
            ):
                issues.append(
                    QualityIssue("INVALID_OHLC", "ERROR", "OHLC high/low invariants failed", candle.timestamp)
                )
                continue
            if candle.volume < 0 or (candle.oi is not None and candle.oi < 0):
                issues.append(
                    QualityIssue("INVALID_VOLUME_OI", "ERROR", "Volume and OI must be non-negative", candle.timestamp)
                )
                continue
            parsed.append(candle)

        parsed.sort(key=lambda item: item.timestamp)
        actual = {item.timestamp for item in parsed}
        for session in sessions:
            missing_count = 0
            for missing in TradingCalendar.expected_minutes(session):
                if missing not in actual:
                    missing_count += 1
                    issues.append(QualityIssue("MISSING_MINUTE", "ERROR", "Expected session candle is missing", missing))
            if missing_count:
                issues.append(
                    QualityIssue(
                        "INCOMPLETE_SESSION_COVERAGE",
                        "ERROR",
                        f"Session is missing {missing_count} expected minute(s)",
                        session.open_at,
                        {"missing_minutes": missing_count},
                    )
                )

        return parsed, issues

    @staticmethod
    def counts(issues: list[QualityIssue]) -> dict[str, int]:
        return dict(sorted(Counter(issue.code for issue in issues).items()))
