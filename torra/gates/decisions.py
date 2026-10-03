"""TRANSPARENCY: every branch torra takes, logged with its reasoning.

Depends on: stdlib only.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

__all__ = ["Decision", "DecisionLog"]


@dataclass
class Decision:
    """One logged decision with its reasoning."""

    stage: str
    decision: str
    reasoning: str
    evidence: dict[str, Any] = field(default_factory=dict)
    at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Serializable dict form."""
        return asdict(self)


class DecisionLog:
    """Every branch torra took, with why. Reasoning is not optional."""

    def __init__(self, clock: Callable[[], float] = time.time,
                 sink: Callable[[Decision], None] | None = None) -> None:
        """Store an injectable clock and an optional live sink for the TUI."""
        self.records: list[Decision] = []
        self._clock = clock
        self._sink = sink

    def log(self, stage: str, decision: str, reasoning: str,
            evidence: dict[str, Any] | None = None) -> Decision:
        """Record one decision."""
        record = Decision(
            stage, decision,
            reasoning.strip() or "NO REASONING SUPPLIED (transparency violation)",
            dict(evidence or {}), self._clock())
        self.records.append(record)
        if self._sink:
            try:
                self._sink(record)
            except Exception:
                pass  # a broken sink must not break the run
        return record

    def trail(self) -> list[dict[str, Any]]:
        """Serializable list of every decision, in order."""
        return [r.to_dict() for r in self.records]

    def violations(self) -> list[Decision]:
        """Decisions logged without real reasoning."""
        return [r for r in self.records if "transparency violation" in r.reasoning]

    def clear(self) -> None:
        """Start a fresh trail -- for a `Torra` reused across many tasks
        (a long-lived TUI tab), so one task's audit record doesn't
        silently accumulate every earlier task's decisions too."""
        self.records.clear()
