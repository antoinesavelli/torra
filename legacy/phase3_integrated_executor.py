"""
phase3_integrated_executor.py
Phase 3 pipeline. Same 8 stages, three more subsystems swapped underneath.

    stage 2  model_select    -> ScoringModelSelector
    stage 5  rollback_prep   -> UndoGenerator snapshots state and pushes
                                generated undos, on top of caller-supplied ones
    stage 6  risk_mitigation -> LockedCheckpointing takes the task lock
    on failure               -> VerifiedRollbackBehavior proves each undo worked

Stage 5 is where the real change lands. Phase 1 and 2 both pushed only what the
caller handed them, so a step whose author forgot an undo was unrecoverable.
Now the generator snapshots every file in `Step.touches`, every unit named in a
`systemctl` command, every `mkdir`, and the package set before an `apt install`,
before any of it runs.

Phase 1 and Phase 2 files are unmodified.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from harness_core import ContextCompressor, OllamaFallbackHandler, Step
from harness_core_v2 import FilesystemConflictDetector
from harness_core_v3 import LockBusy, LockedCheckpointing
from core_behaviors import AskClarifyingQuestionsBehavior, TransparencyBehavior
from core_behaviors_v2 import ContinuousVerifyState, ParsingSafetyClassifier
from core_behaviors_v3 import (
    ScoringModelSelector,
    UndoGenerator,
    VerifiedRollbackBehavior,
)
from phase1_integrated_executor import PipelineStatus, Stage
from phase2_integrated_executor import Phase2IntegratedExecutor, Phase2Result

__all__ = ["Phase3Result", "Phase3IntegratedExecutor"]


@dataclass
class Phase3Result(Phase2Result):
    """Phase2Result plus the Phase 3 telemetry."""

    undo_coverage: Dict[str, Any] = field(default_factory=dict)
    generated_undos: List[Dict[str, Any]] = field(default_factory=list)
    rollback_verified: Optional[bool] = None
    lock_holder: Optional[Dict[str, Any]] = None
    routing_scores: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form, for checkpoints and the audit trail."""
        base = super().to_dict()
        base.update({
            "phase": 3,
            "undo_coverage": self.undo_coverage,
            "generated_undos": self.generated_undos,
            "rollback_verified": self.rollback_verified,
            "lock_holder": self.lock_holder,
            "routing_scores": self.routing_scores,
        })
        return base


class Phase3IntegratedExecutor(Phase2IntegratedExecutor):
    """The Phase 2 pipeline with automatic undo, verification, and locking.

    Everything Phase 2 guaranteed still holds. What changes is that
    ROLLBACK_ON_FAIL now means what its name claims: undos are derived rather
    than hoped for, and each one has to prove it worked.
    """

    def __init__(
        self,
        workdir: Path | str,
        watch_roots: Optional[Iterable[Path | str]] = None,
        clarifier: Optional[AskClarifyingQuestionsBehavior] = None,
        selector: Optional[ScoringModelSelector] = None,
        verifier: Optional[ContinuousVerifyState] = None,
        safety: Optional[ParsingSafetyClassifier] = None,
        rollback: Optional[VerifiedRollbackBehavior] = None,
        transparency: Optional[TransparencyBehavior] = None,
        checkpointer: Optional[LockedCheckpointing] = None,
        conflicts: Optional[FilesystemConflictDetector] = None,
        compressor: Optional[ContextCompressor] = None,
        ollama: Optional[OllamaFallbackHandler] = None,
        undo_generator: Optional[UndoGenerator] = None,
        recheck_between_steps: bool = True,
        compact_every: int = 25,
        step_timer: Optional[Callable[[], float]] = None,
        generate_undos: bool = True,
        lock_block: bool = False,
        lock_timeout: float = 0.0,
    ) -> None:
        """
        Args:
            undo_generator: Snapshots state and derives undo actions. Omit for
                a default with a temp shadow directory.
            generate_undos: Derive undo actions in stage 5. Turn off only if the
                caller genuinely pushes an undo for every change.
            lock_block: Wait for a contended task lock instead of failing fast.
            lock_timeout: Maximum wait when lock_block is True.
            (Other arguments as Phase2IntegratedExecutor, with Phase 3 defaults.)
        """
        workdir = Path(workdir)
        super().__init__(
            workdir=workdir,
            watch_roots=watch_roots,
            clarifier=clarifier,
            selector=selector or ScoringModelSelector(),
            verifier=verifier,
            safety=safety,
            rollback=rollback or VerifiedRollbackBehavior(),
            transparency=transparency,
            checkpointer=checkpointer or LockedCheckpointing(
                workdir / "checkpoints", compact_every=compact_every,
                block=lock_block, lock_timeout=lock_timeout,
            ),
            conflicts=conflicts,
            compressor=compressor,
            ollama=ollama,
            recheck_between_steps=recheck_between_steps,
            compact_every=compact_every,
            step_timer=step_timer,
        )
        self.undo_generator = undo_generator or UndoGenerator(
            shadow_dir=workdir / "shadow")
        self.generate_undos = bool(generate_undos)
        self._generated_actions: List[Any] = []
        self._undo_coverage: Dict[str, Any] = {}

    def run(
        self,
        task_id: str,
        task: str,
        steps: Sequence[Step],
        commands: Optional[Sequence[str]] = None,
        rollback_actions: Optional[Sequence[tuple[str, Callable[[], Any], bool]]] = None,
        rollback_on_fail: bool = True,
    ) -> Phase3Result:
        """Run one task through all 8 stages with the Phase 3 guarantees.

        Returns:
            A Phase3Result. Adds undo coverage, the generated undo list, the
            rollback verification verdict, lock holder identity on contention,
            and the full routing score table.
        """
        run_started = time.perf_counter()

        # Take the task lock before anything else. A contended task id is not a
        # stage failure, it is a refusal to start, and it must say who holds it.
        try:
            self.checkpointer.acquire(task_id)
        except LockBusy as exc:
            result = Phase3Result(task_id=task_id, task=task,
                                  status=PipelineStatus.PRECONDITION_FAILED)
            result.lock_holder = exc.holder.to_dict() if exc.holder else None
            self.transparency.log_decision(
                "task_lock", "refused to start",
                str(exc),
                {"holder": result.lock_holder},
            )
            self._record(result, Stage.CLARIFY, False, run_started,
                         "blocked: task id already running",
                         {"holder": result.lock_holder})
            return self._finish3(result, run_started)

        try:
            base = super().run(task_id, task, steps, commands,
                               rollback_actions, rollback_on_fail)
        finally:
            self.checkpointer.release(task_id)

        result = self._upgrade(base)
        result.undo_coverage = dict(self._undo_coverage)
        result.generated_undos = [
            {"description": a.description,
             "safe": a.safe,
             "snapshot": a.snapshot.to_dict() if a.snapshot else None}
            for a in self._generated_actions
        ]
        if result.rollback is not None:
            verdicts = [getattr(r, "verified", None) for r in result.rollback.results]
            result.rollback_verified = (
                None if not verdicts or all(v is None for v in verdicts)
                else all(v is not False for v in verdicts)
            )
        if isinstance(self.selector, ScoringModelSelector):
            effective = (result.clarification.refined_task
                         if result.clarification else task) or task
            result.routing_scores = [
                {"rule": n, "model": m, "score": s,
                 "terms": sorted(set(x for x in te if x))}
                for n, m, s, te in self.selector.score(effective)
            ]
        self.transparency.log_decision(
            "undo_generation",
            f"{len(result.generated_undos)} undo action(s) derived from step state",
            f"coverage {result.undo_coverage.get('coverage_ratio', 0):.0%}; "
            f"uncovered steps: {result.undo_coverage.get('uncovered') or 'none'}"
            + (f"; skipped: {result.undo_coverage.get('skipped')}"
               if result.undo_coverage.get('skipped') else ""),
            result.undo_coverage,
        )
        result.decisions = self.transparency.audit_trail()
        if result.audit_path:
            try:
                import json
                Path(result.audit_path).write_text(
                    json.dumps(result.to_dict(), indent=2, default=str),
                    encoding="utf-8")
            except Exception as exc:
                self.transparency.log_decision(
                    "audit_log", "phase 3 audit rewrite failed", repr(exc))
        return result

    # -- stage 5 override -------------------------------------------------- #

    def _rollback_prep(
        self,
        steps: Sequence[Step],
        rollback_actions: Optional[Sequence[tuple[str, Callable[[], Any], bool]]],
    ) -> Dict[str, Any]:
        """Snapshot state and push generated undos, then caller-supplied ones.

        Generated undos go on first so they unwind last. The caller's undo for a
        high level operation should run before the low level file restore that
        would otherwise fight it.
        """
        detail: Dict[str, Any] = {"generated": 0, "caller": 0}
        self._generated_actions = []
        if self.generate_undos and isinstance(self.rollback, VerifiedRollbackBehavior):
            for step in steps:
                actions = self.undo_generator.for_step(step)
                self._generated_actions.extend(actions)
                detail["generated"] += self.rollback.push_generated(actions)
        for description, undo, safe in (rollback_actions or []):
            self.rollback.push(description, undo, safe=safe)
            detail["caller"] += 1
        self._undo_coverage = self.undo_generator.coverage(steps)
        detail["undo_coverage_ratio"] = self._undo_coverage["coverage_ratio"]
        detail["uncovered_steps"] = self._undo_coverage["uncovered"]
        return detail

    def _upgrade(self, base: Phase2Result) -> Phase3Result:
        """Copy a Phase2Result into a Phase3Result with the extra telemetry."""
        result = Phase3Result(task_id=base.task_id, task=base.task,
                              status=base.status)
        for name, value in vars(base).items():
            setattr(result, name, value)
        return result

    def _finish3(self, result: Phase3Result, started: float) -> Phase3Result:
        """Stage 8 with the Phase 3 telemetry attached."""
        self._finish(result, started)
        return result
