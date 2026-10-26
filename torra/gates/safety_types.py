"""The value types SAFETY_FIRST speaks in: `RiskLevel`, `ResolvedCommand` and `Verdict`.

Depends on: nothing in torra (stdlib only); `torra.gates.safety` re-exports every name.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

__all__ = ["RiskLevel", "ORDER", "ResolvedCommand", "Verdict"]


class RiskLevel(StrEnum):
    """How much damage a command can do if it is wrong."""

    SAFE = "safe"
    CAUTION = "caution"
    DANGEROUS = "dangerous"
    CRITICAL = "critical"


ORDER: list[RiskLevel] = [RiskLevel.SAFE, RiskLevel.CAUTION,
                          RiskLevel.DANGEROUS, RiskLevel.CRITICAL]


@dataclass
class ResolvedCommand:
    """One concrete command extracted from a shell string."""

    argv: list[str]
    source: str
    unwrapped_from: str = ""

    @property
    def program(self) -> str:
        """Executable name with any directory prefix stripped; `python3.12` is `python3`."""
        name = self.argv[0].rsplit("/", 1)[-1] if self.argv else ""
        return "python3" if re.fullmatch(r"python3\.[0-9]+", name) else name

    def to_dict(self) -> dict[str, Any]:
        """Serializable dict form."""
        d = asdict(self)
        d["program"] = self.program
        return d


@dataclass
class Verdict:
    """Safety classification of one command."""

    command: str
    level: RiskLevel
    reasons: list[str] = field(default_factory=list)
    resolved: list[ResolvedCommand] = field(default_factory=list)
    approved: bool | None = None

    @property
    def needs_confirmation(self) -> bool:
        """True when a human must say yes before this runs."""
        return self.level in (RiskLevel.DANGEROUS, RiskLevel.CRITICAL)

    @property
    def blocked(self) -> bool:
        """True when the caller must not execute this command."""
        return self.needs_confirmation and self.approved is not True

    def to_dict(self) -> dict[str, Any]:
        """Serializable dict form."""
        return {
            "command": self.command,
            "level": self.level.value,
            "reasons": list(self.reasons),
            "resolved": [r.to_dict() for r in self.resolved],
            "approved": self.approved,
            "needs_confirmation": self.needs_confirmation,
            "blocked": self.blocked,
        }
