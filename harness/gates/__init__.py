"""The five behavioural guardrails, one submodule each.

Depends on: harness.gates.decisions, .safety, .state, .rollback, .clarifier,
.router (this package only re-exports; each submodule is otherwise
self-contained with no cross-references between the five gates).
"""

from __future__ import annotations

from .clarifier import Ambiguity, Clarification, Clarifier
from .decisions import Decision, DecisionLog
from .rollback import (
    RollbackReport,
    RollbackResult,
    Snapshot,
    SnapshotKind,
    UndoAction,
    UndoGenerator,
    VerifiedRollback,
)
from .router import ModelChoice, ModelRouter, model_available
from .safety import ResolvedCommand, RiskLevel, SafetyClassifier, Verdict
from .state import Precondition, PreconditionReport, PreconditionResult, StateVerifier

__all__ = [
    "RiskLevel", "ResolvedCommand", "Verdict", "SafetyClassifier",
    "Precondition", "PreconditionResult", "PreconditionReport", "StateVerifier",
    "SnapshotKind", "Snapshot", "UndoAction", "UndoGenerator",
    "RollbackResult", "RollbackReport", "VerifiedRollback",
    "Ambiguity", "Clarification", "Clarifier",
    "ModelChoice", "ModelRouter", "model_available",
    "Decision", "DecisionLog",
]
