"""The data model Torra produces and consumes: stages, outcomes, plans, results.

Depends on: torra.agent (Turn), torra.core (Step, StepResult, TaskSpec),
torra.gates (PreconditionReport, RiskLevel, RollbackReport, UndoAction, Verdict).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .agent import Turn
from .core import Step, StepResult, TaskSpec
from .gates import PreconditionReport, RiskLevel, RollbackReport, UndoAction, Verdict

__all__ = ["Stage", "Outcome", "StepPlan", "Plan", "RunResult"]


class Stage(str, Enum):
    """The seven stages, in order."""

    CONVERSE = "converse"
    OBSERVE = "observe"
    PROPOSE = "propose"
    GATE = "gate"
    PRESENT = "present"
    EXECUTE = "execute"
    REPORT = "report"


class Outcome(str, Enum):
    """Terminal state of one request."""

    ANSWERED = "answered"                # a question, nothing to run
    NEEDS_ANSWER = "needs_answer"        # the model asked something
    REFUSED = "refused"                  # the model declined
    AWAITING_APPROVAL = "awaiting_approval"  # a plan is ready, nobody approved
    REJECTED = "rejected"                # the operator declined the plan
    BLOCKED = "blocked"                  # a gate stopped it
    COMPLETED = "completed"
    FAILED = "failed"
    BACKEND_DOWN = "backend_down"


@dataclass
class StepPlan:
    """One proposed step with everything the gates decided about it -- what
    the operator actually looks at, not the bare plan the model wrote."""

    step: Step
    verdict: Verdict
    undos: list[UndoAction] = field(default_factory=list)
    approved: bool | None = None
    result: StepResult | None = None

    @property
    def needs_confirmation(self) -> bool:
        """True when this step cannot run without an explicit yes."""
        return self.verdict.needs_confirmation

    @property
    def has_undo(self) -> bool:
        """True when failing this step can be reversed."""
        return bool(self.undos)

    def to_dict(self) -> dict[str, Any]:
        """Serializable dict form, for the audit trail."""
        return {
            "step": self.step.to_dict(),
            "verdict": self.verdict.to_dict(),
            "undos": [u.to_dict() for u in self.undos],
            "approved": self.approved,
            "result": self.result.to_dict() if self.result else None,
        }


@dataclass
class Plan:
    """A proposal plus every gate verdict on it, ready to show a human.
    Deliberately carries no `conflicts` field -- see `Torra._gate()`'s
    comment; real conflict detection is `execute()`'s per-step `Outcome.BLOCKED`."""

    spec: TaskSpec
    steps: list[StepPlan] = field(default_factory=list)
    preconditions: PreconditionReport | None = None
    coverage: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    repairs: int = 0

    @property
    def worst_level(self) -> RiskLevel:
        """The highest risk level anywhere in the plan."""
        order = [RiskLevel.SAFE, RiskLevel.CAUTION, RiskLevel.DANGEROUS,
                 RiskLevel.CRITICAL]
        levels = [sp.verdict.level for sp in self.steps] or [RiskLevel.SAFE]
        return max(levels, key=order.index)

    @property
    def needs_confirmation(self) -> list[StepPlan]:
        """Steps that cannot run without an explicit yes."""
        return [sp for sp in self.steps if sp.needs_confirmation]

    @property
    def uncovered(self) -> list[str]:
        """Step ids with no derivable undo."""
        return list(self.coverage.get("uncovered", []))

    def to_dict(self) -> dict[str, Any]:
        """Serializable dict form, for the audit trail."""
        return {
            "spec": self.spec.to_dict(),
            "steps": [s.to_dict() for s in self.steps],
            "preconditions": self.preconditions.to_dict() if self.preconditions else None,
            "coverage": self.coverage,
            "model": self.model,
            "repairs": self.repairs,
            "worst_level": self.worst_level.value,
        }


@dataclass
class RunResult:
    """Everything one request produced."""

    request: str
    outcome: Outcome
    stages: list[str] = field(default_factory=list)
    turn: Turn | None = None
    plan: Plan | None = None
    results: list[StepResult] = field(default_factory=list)
    rollback: RollbackReport | None = None
    abort_reason: str = ""
    decisions: list[dict[str, Any]] = field(default_factory=list)
    audit_path: str = ""
    seconds: float = 0.0
    # True once a real (non-dry-run) execution attempt has begun -- distinct
    # from `outcome`, which a rejected-for-lack-of-approval or dry-run-only
    # result can also reach without a single real command having run. See
    # execute()'s replay guard: `outcome != AWAITING_APPROVAL` alone was too
    # broad and blocked the ordinary "tick the box, click Run again" retry
    # after a rejection.
    executed: bool = False
    #: Set by `Torra._summarize()` once execution has produced at least one
    #: real result -- the model's own account of what its steps found, since
    #: nothing upstream of `_finish()` ever reads step output back. Empty when
    #: there was nothing to run, no model was in the loop, or the call failed.
    summary: str = ""

    @property
    def ok(self) -> bool:
        """True only when the task completed."""
        return self.outcome is Outcome.COMPLETED

    @property
    def message(self) -> str:
        """What to show the operator as the headline."""
        if self.summary:
            return self.summary
        if self.turn and self.turn.message:
            return self.turn.message
        return self.abort_reason or self.outcome.value

    def to_dict(self) -> dict[str, Any]:
        """Serializable dict form, the audit record."""
        return {
            "request": self.request,
            "outcome": self.outcome.value,
            "stages": list(self.stages),
            "turn": self.turn.to_dict() if self.turn else None,
            "plan": self.plan.to_dict() if self.plan else None,
            "results": [r.to_dict() for r in self.results],
            "rollback": self.rollback.to_dict() if self.rollback else None,
            "abort_reason": self.abort_reason,
            "summary": self.summary,
            "decisions": self.decisions,
            "seconds": round(self.seconds, 3),
        }
