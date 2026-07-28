"""
phase4_integrated_executor.py
Phase 4 pipeline. Same 8 stages, three more subsystems swapped underneath.

    stage 1  clarify         -> StructuralClarifier
    stage 5  rollback_prep   -> ReflinkUndoGenerator
    stage 6  risk_mitigation -> EventDrivenConflictDetector
    on start                 -> ShadowReaper ages out orphaned shadow dirs

Stage 1 is the one that matters. It was the only gate that never got a
validation pass of its own, and measurement showed why that was a problem: an
83% false negative rate on a blind corpus.

Phases 1 through 3 are unmodified.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from harness_core import ContextCompressor, OllamaFallbackHandler, Step
from harness_core_v3 import LockedCheckpointing
from harness_core_v4 import EventDrivenConflictDetector
from core_behaviors import TransparencyBehavior
from core_behaviors_v2 import ContinuousVerifyState, ParsingSafetyClassifier
from core_behaviors_v3 import ScoringModelSelector, VerifiedRollbackBehavior
from core_behaviors_v4 import ReapReport, ReflinkUndoGenerator, ShadowReaper, StructuralClarifier
from phase3_integrated_executor import Phase3IntegratedExecutor, Phase3Result

__all__ = ["Phase4Result", "Phase4IntegratedExecutor"]


@dataclass
class Phase4Result(Phase3Result):
    """Phase3Result plus the Phase 4 telemetry."""

    shadow_stats: Dict[str, Any] = field(default_factory=dict)
    reap_report: Dict[str, Any] = field(default_factory=dict)
    conflict_stats: Dict[str, Any] = field(default_factory=dict)
    clarifier_anchors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form, for checkpoints and the audit trail."""
        base = super().to_dict()
        base.update({
            "phase": 4,
            "shadow_stats": self.shadow_stats,
            "reap_report": self.reap_report,
            "conflict_stats": self.conflict_stats,
            "clarifier_anchors": self.clarifier_anchors,
        })
        return base


class Phase4IntegratedExecutor(Phase3IntegratedExecutor):
    """The Phase 3 pipeline with a measured clarifier and cheaper mechanics.

    Everything Phase 3 guaranteed still holds. What changes is that stage 1 now
    has a number attached to it, shadow snapshots are close to free on a
    copy-on-write filesystem, orphaned shadows get reaped, and tree watching
    stops paying O(tree) on every check.
    """

    def __init__(
        self,
        workdir: Path | str,
        watch_roots: Optional[Iterable[Path | str]] = None,
        clarifier: Optional[StructuralClarifier] = None,
        selector: Optional[ScoringModelSelector] = None,
        verifier: Optional[ContinuousVerifyState] = None,
        safety: Optional[ParsingSafetyClassifier] = None,
        rollback: Optional[VerifiedRollbackBehavior] = None,
        transparency: Optional[TransparencyBehavior] = None,
        checkpointer: Optional[LockedCheckpointing] = None,
        conflicts: Optional[EventDrivenConflictDetector] = None,
        compressor: Optional[ContextCompressor] = None,
        ollama: Optional[OllamaFallbackHandler] = None,
        undo_generator: Optional[ReflinkUndoGenerator] = None,
        reaper: Optional[ShadowReaper] = None,
        recheck_between_steps: bool = True,
        compact_every: int = 25,
        step_timer: Optional[Callable[[], float]] = None,
        generate_undos: bool = True,
        lock_block: bool = False,
        lock_timeout: float = 0.0,
        reap_on_start: bool = True,
        reconcile_every: int = 20,
    ) -> None:
        """
        Args:
            reaper: Ages out orphaned shadow directories. Omit for a default
                scoped to this workdir.
            reap_on_start: Run one reap pass before the first stage.
            reconcile_every: Fast event-driven checks between full tree walks.
            (Other arguments as Phase3IntegratedExecutor, Phase 4 defaults.)
        """
        workdir = Path(workdir)
        super().__init__(
            workdir=workdir,
            watch_roots=watch_roots,
            clarifier=clarifier or StructuralClarifier(),
            selector=selector,
            verifier=verifier,
            safety=safety,
            rollback=rollback,
            transparency=transparency,
            checkpointer=checkpointer,
            conflicts=conflicts or EventDrivenConflictDetector(
                reconcile_every=reconcile_every),
            compressor=compressor,
            ollama=ollama,
            undo_generator=undo_generator or ReflinkUndoGenerator(
                shadow_dir=workdir / f"harness-shadow-{os.getpid()}"),
            recheck_between_steps=recheck_between_steps,
            compact_every=compact_every,
            step_timer=step_timer,
            generate_undos=generate_undos,
            lock_block=lock_block,
            lock_timeout=lock_timeout,
        )
        self.reaper = reaper or ShadowReaper(roots=[workdir])
        self.reap_on_start = bool(reap_on_start)

    def run(
        self,
        task_id: str,
        task: str,
        steps: Sequence[Step],
        commands: Optional[Sequence[str]] = None,
        rollback_actions: Optional[Sequence[tuple[str, Callable[[], Any], bool]]] = None,
        rollback_on_fail: bool = True,
    ) -> Phase4Result:
        """Run one task through all 8 stages with the Phase 4 guarantees.

        Returns:
            A Phase4Result. Adds shadow copy mode counters, the reap report,
            event-versus-walk conflict stats, and the anchors the clarifier
            found in the request.
        """
        reap: Optional[ReapReport] = None
        if self.reap_on_start:
            try:
                reap = self.reaper.reap()
            except Exception as exc:
                self.transparency.log_decision(
                    "shadow_reap", "reap pass failed",
                    f"{type(exc).__name__}: {exc}")

        base = super().run(task_id, task, steps, commands,
                           rollback_actions, rollback_on_fail)
        result = self._upgrade4(base)

        if reap is not None:
            result.reap_report = reap.to_dict()
            if reap.removed:
                self.transparency.log_decision(
                    "shadow_reap",
                    f"removed {reap.removed} orphaned shadow director(ies)",
                    f"freed {reap.bytes_freed / 1024 / 1024:.1f} MB; "
                    f"{reap.kept_live} kept because they are younger than "
                    f"{self.reaper.min_age_seconds:.0f}s and may be live",
                    reap.to_dict(),
                )

        if isinstance(self.undo_generator, ReflinkUndoGenerator):
            result.shadow_stats = self.undo_generator.stats()
        if isinstance(self.conflicts, EventDrivenConflictDetector):
            result.conflict_stats = self.conflicts.stats_dict()
        if isinstance(self.clarifier, StructuralClarifier):
            result.clarifier_anchors = self.clarifier.anchors_in(task)

        self.transparency.log_decision(
            "phase4_telemetry",
            "shadow, reap, and conflict counters recorded",
            f"shadow mode "
            f"{'reflink' if result.shadow_stats.get('reflink_supported') else 'byte copy'}; "
            f"conflict fast path ratio "
            f"{result.conflict_stats.get('fast_path_ratio', 0):.0%}; "
            f"clarifier anchors {result.clarifier_anchors or 'none'}",
            {"shadow": result.shadow_stats, "conflicts": result.conflict_stats},
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
                    "audit_log", "phase 4 audit rewrite failed", repr(exc))
        return result

    def _upgrade4(self, base: Phase3Result) -> Phase4Result:
        """Copy a Phase3Result into a Phase4Result with the extra telemetry."""
        result = Phase4Result(task_id=base.task_id, task=base.task,
                              status=base.status)
        for name, value in vars(base).items():
            setattr(result, name, value)
        return result
