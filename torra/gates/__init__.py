"""The five behavioural guardrails, one submodule each.

Depends on: torra.gates.decisions, .safety, .state, .rollback, .undo,
.clarifier, .router (this package only re-exports; each submodule is
otherwise self-contained with no cross-references between the five gates,
except .rollback -> .undo, ROLLBACK_ON_FAIL's own two halves).
"""

from __future__ import annotations

from .clarifier import Ambiguity, Clarification, Clarifier
from .decisions import Decision, DecisionLog
from .rollback import RollbackReport, RollbackResult, VerifiedRollback
from .router import ModelChoice, ModelRouter, model_available
from .safety import ResolvedCommand, RiskLevel, SafetyClassifier, Verdict
from .state import Precondition, PreconditionReport, PreconditionResult, StateVerifier
from .undo import Snapshot, SnapshotKind, UndoAction, UndoGenerator

__all__ = [
    "RiskLevel",
    "ResolvedCommand",
    "Verdict",
    "SafetyClassifier",
    "Precondition",
    "PreconditionResult",
    "PreconditionReport",
    "StateVerifier",
    "SnapshotKind",
    "Snapshot",
    "UndoAction",
    "UndoGenerator",
    "RollbackResult",
    "RollbackReport",
    "VerifiedRollback",
    "Ambiguity",
    "Clarification",
    "Clarifier",
    "ModelChoice",
    "ModelRouter",
    "model_available",
    "Decision",
    "DecisionLog",
]
