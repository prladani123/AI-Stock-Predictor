from __future__ import annotations

from dataclasses import asdict, dataclass
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class TradingSession:
    session_date: date
    open_at: datetime
    close_at: datetime
    status: str = "OPEN"


@dataclass(frozen=True)
class Instrument:
    instrument_token: int
    exchange: str
    tradingsymbol: str
    name: str
    segment: str
    instrument_type: str
    expiry: date | None
    tick_size: Decimal
    lot_size: int
    strike: Decimal | None = None


@dataclass(frozen=True)
class Candle:
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    oi: int | None = None


@dataclass(frozen=True)
class QualityIssue:
    code: str
    severity: str
    message: str
    timestamp: datetime | None = None
    details: dict[str, Any] | None = None


@dataclass(frozen=True)
class ImportResult:
    job_id: str
    status: str
    rows_received: int
    rows_inserted: int
    duplicate_rows: int
    rejected_rows: int
    checkpoint_at: datetime | None
    issues: tuple[QualityIssue, ...]


@dataclass(frozen=True)
class CoverageReport:
    instrument_id: int
    session_date: date
    expected_minutes: int
    stored_minutes: int
    missing_minutes: tuple[datetime, ...]
    coverage_percent: float
    issue_counts: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["session_date"] = self.session_date.isoformat()
        result["missing_minutes"] = [item.isoformat() for item in self.missing_minutes]
        return result


@dataclass(frozen=True)
class RevisionCandle:
    instrument_id: int
    candle: Candle
    source_job_id: str


@dataclass(frozen=True)
class DatasetRevision:
    id: str
    created_at: datetime
    instrument_ids: tuple[int, ...]
    session_start: date
    session_end: date
    row_count: int
    quality_summary: dict[str, Any]
    source_job_ids: tuple[str, ...]
    checksum: str
    status: str = "CANDIDATE"
    decided_at: datetime | None = None
    decision_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision_id": self.id,
            "created_at": self.created_at.isoformat(),
            "instruments": list(self.instrument_ids),
            "session_range": {
                "from": self.session_start.isoformat(),
                "to": self.session_end.isoformat(),
            },
            "row_count": self.row_count,
            "quality_summary": deepcopy(self.quality_summary),
            "source_import_job_ids": list(self.source_job_ids),
            "deterministic_checksum": self.checksum,
            "status": self.status,
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
            "decision_reason": self.decision_reason,
        }


@dataclass(frozen=True)
class TargetLabel:
    dataset_revision_id: str
    instrument_id: int
    as_of: datetime
    horizon_minutes: int
    reference_price: Decimal
    expiry_at: datetime
    future_price: Decimal | None
    threshold_policy_version: str
    threshold_value: Decimal
    realised_return: Decimal | None
    label: str
    quality_status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_revision": self.dataset_revision_id,
            "instrument": self.instrument_id,
            "as_of": self.as_of.isoformat(),
            "horizon_minutes": self.horizon_minutes,
            "reference_price": str(self.reference_price),
            "expiry_timestamp": self.expiry_at.isoformat(),
            "future_price": str(self.future_price) if self.future_price is not None else None,
            "threshold_policy_version": self.threshold_policy_version,
            "threshold_value": str(self.threshold_value),
            "realised_return": str(self.realised_return) if self.realised_return is not None else None,
            "label": self.label,
            "quality_status": self.quality_status,
        }
