from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any


class FixtureZerodhaClient:
    """Offline stand-in matching only the two safe Kite methods."""

    def __init__(self, fixture_dir: Path) -> None:
        self.fixture_dir = fixture_dir
        self.historical_calls = 0
        self.instrument_calls = 0

    def _load(self, name: str) -> list[dict[str, Any]]:
        return json.loads((self.fixture_dir / name).read_text(encoding="utf-8"))

    def instruments(self, exchange: str | None = None) -> list[dict[str, Any]]:
        self.instrument_calls += 1
        rows = self._load("instruments.json")
        if exchange:
            return [row for row in rows if row["exchange"] == exchange]
        return rows

    def historical_data(
        self,
        instrument_token: int,
        from_date: datetime,
        to_date: datetime,
        interval: str,
        continuous: bool = False,
        oi: bool = False,
    ) -> list[dict[str, Any]]:
        self.historical_calls += 1
        if interval != "minute" or continuous or not oi:
            raise AssertionError("fixture client received an unsafe or unsupported historical request")
        return self._load(f"candles_{instrument_token}.json")

