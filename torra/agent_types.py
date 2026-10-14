"""The data model `Agent` produces: `Turn`, `Intent`, and the errors it raises.

Depends on: torra.core (TaskSpec).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .core import TaskSpec

__all__ = ["AgentError", "MalformedTaskError", "Intent", "Turn"]


class AgentError(RuntimeError):
    """Raised when the model cannot produce a usable proposal."""


class MalformedTaskError(AgentError):
    """The proposed task failed TaskSpec validation. The one AgentError a revision round can fix:
    the validator's message names the field and lists the valid ones."""


class Intent(str, Enum):
    """What the agent decided to do with a turn."""

    ANSWER = "answer"        # a question, no system change needed
    ASK = "ask"              # underspecified, needs the operator
    PLAN = "plan"            # enough information, here is a proposal
    REFUSE = "refuse"        # will not do this


@dataclass
class Turn:
    """One exchange: what the operator said and what the agent decided."""

    request: str
    intent: Intent
    message: str = ""
    questions: list[str] = field(default_factory=list)
    spec: TaskSpec | None = None
    model: str = ""
    routing_reason: str = ""
    clarifier_gaps: list[str] = field(default_factory=list)
    anchors: list[str] = field(default_factory=list)
    machine_observed: bool = False
    repairs: int = 0
    seconds: float = 0.0
    raw: str = ""
    #: Paths of previously-read files the model says no longer help --
    #: `Agent.converse()` acts on this against `_read_context` right after
    #: parsing (§2.46). Kept on `Turn`, not consumed and discarded, so the
    #: audit trail records what the model chose to forget, not just that
    #: something happened to `self.context`.
    forget: list[str] = field(default_factory=list)
    #: Advisory notes still standing once repairs ran out (a missing path); shown, never a gate.
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serializable dict form, for the audit trail."""
        return {
            "request": self.request,
            "intent": self.intent.value,
            "message": self.message,
            "questions": list(self.questions),
            "spec": self.spec.to_dict() if self.spec else None,
            "model": self.model,
            "routing_reason": self.routing_reason,
            "clarifier_gaps": list(self.clarifier_gaps),
            "anchors": list(self.anchors),
            "machine_observed": self.machine_observed,
            "repairs": self.repairs,
            "seconds": round(self.seconds, 3),
            "forget": list(self.forget),
            "warnings": list(self.warnings),
        }
