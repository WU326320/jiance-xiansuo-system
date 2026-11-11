"""一次平台运行的计数器与错误摘要。"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field


@dataclass
class RunStats:
    pages_fetched: int = 0
    items_dropped: int = 0
    items_duplicate: int = 0
    drop_reasons: Counter[str] = field(default_factory=Counter)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def page_fetched(self) -> None:
        self.pages_fetched += 1

    def drop(self, reason: str, detail: str | None = None) -> None:
        self.items_dropped += 1
        self.drop_reasons[reason] += 1
        if detail:
            self.errors.append(f"{reason}: {detail}")

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warning(self, message: str) -> None:
        self.warnings.append(message)

    def merge(self, other: "RunStats") -> None:
        self.pages_fetched += other.pages_fetched
        self.items_dropped += other.items_dropped
        self.items_duplicate += other.items_duplicate
        self.drop_reasons.update(other.drop_reasons)
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)
