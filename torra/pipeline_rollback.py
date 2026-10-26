"""Rollback bookkeeping for one execute() attempt: which undos run, in what order, and which are skipped.

Depends on: torra.core (Conflict, TaskSpec), torra.gates (DecisionLog, RiskLevel,
SafetyClassifier, UndoAction, VerifiedRollback), torra.pipeline_types (RunResult, Stage).
"""

from __future__ import annotations

from collections.abc import Sequence

from .core import Conflict, ConflictDetector, TaskSpec
from .gates import DecisionLog, RiskLevel, SafetyClassifier, UndoAction, VerifiedRollback
from .pipeline_types import RunResult, Stage


def declared_undos(
    spec: TaskSpec, safety: SafetyClassifier, decisions: DecisionLog
) -> list[UndoAction]:
    """The plan's declared rollback entries, each classified the way a step is."""
    held: list[UndoAction] = []
    for description, command, undo, declared_safe in spec.rollback_actions():
        # A declared rollback entry only gets to make a command *more*
        # cautious than SafetyClassifier would, never less: the command
        # runs through the same gate a regular step does, and either
        # side calling it risky is enough to require confirmation.
        verdict = safety.classify(command)
        risky = verdict.level in (RiskLevel.DANGEROUS, RiskLevel.CRITICAL)
        if declared_safe and risky:
            decisions.log(
                Stage.PRESENT.value,
                "rollback safety override",
                f"'{description}' was declared safe but classifies "
                f"{verdict.level.value}: {verdict.reasons[0]}; will ask "
                "before running it during rollback",
                {"command": command},
            )
        held.append(UndoAction(description, undo, declared_safe and not risky))
    return held


def drop_edited_undos(
    rollback: VerifiedRollback, decisions: DecisionLog, found: Sequence[Conflict]
) -> None:
    """Take off the stack the undo of every file changed outside torra, and log each."""
    changed = {c.path for c in found}
    for action in rollback.drop_where(
        lambda a: a.snapshot is not None and a.snapshot.target in changed
    ):
        decisions.log(
            Stage.EXECUTE.value,
            "undo skipped",
            f"{action.description}: that file changed outside torra, and restoring "
            "it would overwrite the change",
        )


def sweep(
    rollback: VerifiedRollback,
    decisions: DecisionLog,
    result: RunResult,
    held: Sequence[UndoAction],
    ran: bool,
    conflicts: ConflictDetector,
) -> None:
    """Unwind the stack and record the report.

    Whatever stopped the run, files changed outside torra since its last check lose their undo
    first, and then the declared entries go on top, to run first, only if a real step was armed
    in this attempt (resumed steps do not count) and nothing was found changed."""
    found = conflicts.detect() if (conflicts.hashes or conflicts.roots) else []
    if found:
        drop_edited_undos(rollback, decisions, found)
    if ran and not found:
        rollback.push_many(held)
    if rollback.depth:
        result.rollback = rollback.rollback()
        decisions.log(
            Stage.EXECUTE.value,
            "rollback clean" if result.rollback.ok else "rollback incomplete",
            f"{len(result.rollback.undone)} undone, "
            f"{len(result.rollback.unverified)} unverified, "
            f"{len(result.rollback.cancelled)} cancelled, "
            f"{len(result.rollback.failed)} failed",
            {"results": [r.to_dict() for r in result.rollback.results]},
        )
