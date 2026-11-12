from __future__ import annotations

from collections import Counter
from decimal import Decimal
from typing import Protocol

from .research_models import BaselineDefinition, FeatureRow
from .repository import digest

CLASSES = ("UP", "DOWN", "NO_TRADE")


class Baseline(Protocol):
    definition: BaselineDefinition

    def predict(self, row: FeatureRow) -> tuple[str, Decimal | None, str]: ...


class MajorityBaseline:
    def __init__(self, horizon: int, labels: list[str]) -> None:
        counts = Counter(item for item in labels if item in CLASSES)
        priority = {"NO_TRADE": 2, "UP": 1, "DOWN": 0}
        majority = max(CLASSES, key=lambda item: (counts[item], priority[item]))
        config = {"class_counts": dict(sorted(counts.items())), "majority_class": majority}
        checksum = digest(config)
        version = f"majority-v1-h{horizon}-{checksum[:8]}"
        self.definition = BaselineDefinition(version, "MAJORITY", horizon, config, checksum)
        self.majority = majority
        self.confidence = (
            Decimal(counts[majority]) / Decimal(sum(counts.values())) if counts else None
        )

    def predict(self, row: FeatureRow) -> tuple[str, Decimal | None, str]:
        if row.quality_status == "UNAVAILABLE":
            return "UNAVAILABLE", None, "feature row unavailable"
        return self.majority, self.confidence, "most common training class"


class PersistenceBaseline:
    def __init__(
        self,
        horizon: int,
        return_minutes: int = 5,
        threshold: Decimal = Decimal("0.001"),
    ) -> None:
        config = {"return_minutes": return_minutes, "threshold": str(threshold)}
        checksum = digest(config)
        version = f"persistence-v1-h{horizon}-{checksum[:8]}"
        self.definition = BaselineDefinition(version, "PERSISTENCE", horizon, config, checksum)
        self.feature_name = f"return_{return_minutes}m"
        self.threshold = threshold

    def predict(self, row: FeatureRow) -> tuple[str, Decimal | None, str]:
        value = row.values.get(self.feature_name)
        if value is None:
            return "UNAVAILABLE", None, f"{self.feature_name} unavailable"
        if value > self.threshold:
            return "UP", None, f"{self.feature_name} above threshold"
        if value < -self.threshold:
            return "DOWN", None, f"{self.feature_name} below negative threshold"
        return "NO_TRADE", None, f"{self.feature_name} within threshold"
