"""
phase1_integrated_executor.py
Phase 1C: the 8 stage pipeline that binds Phase 1A mitigations to Phase 1B gates.

Pipeline order, with early exit points marked:

    1. clarify              exit if ambiguity is unresolved
    2. model_select
    3. verify_preconditions exit if a blocking precondition fails
    4. safety_analysis      exit if any command is blocked
    5. rollback_prep
    6. risk_mitigation      exit if the backend is unreachable
    7. execute              rolls back on failure
    8. audit_log            always runs, even after an early exit

Stage 8 is the only stage that runs unconditionally. Everything the pipeline
decided lands in one JSON audit file, so a run can be reconstructed after the
fact without reading terminal scrollback.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from harness_core import (
    AdaptiveCheckpointing,
    CheckpointPolicy,
    ConflictDetector,
    ContextCompressor,
    CoreExecutor,
    ExecutionReport,
    OllamaFallbackHandler,
    OllamaStatus,
    RecoveryMenu,
    Step,
)
from core_behaviors import (
    AskClarifyingQuestionsBehavior,
    ClarificationReport,
    CommandVerdict,
    ModelChoice,
    ModelSelector,
    PreconditionReport,
    RollbackOnFailBehavior,
    RollbackReport,
    SafetyFirstBehavior,
    TransparencyBehavior,
    VerifyStateBehavior,
)

__all__ = ["Stage", "PipelineStatus", "StageRecord", "PipelineResult",
           "Phase1IntegratedExecutor"]


class Stage(str, Enum):
    """The 8 pipeline stages, in execution order."""

    CLARIFY = "clarify"
    MODEL_SELECT = "model_select"
    VERIFY_PRECONDITIONS = "verify_preconditions"
    SAFETY_ANALYSIS = "safety_analysis"
    ROLLBACK_PREP = "rollback_prep"
    RISK_MITIGATION = "risk_mitigation"
    EXECUTE = "execute"
    AUDIT_LOG = "audit_log"


#: Canonical order. Tests assert against this.
STAGE_ORDER: List[Stage] = [
    Stage.CLARIFY,
    Stage.MODEL_SELECT,
    Stage.VERIFY_PRECONDITIONS,
    Stage.SAFETY_ANALYSIS,
    Stage.ROLLBACK_PREP,
    Stage.RISK_MITIGATION,
    Stage.EXECUTE,
    Stage.AUDIT_LOG,
]


class PipelineStatus(str, Enum):
    """Terminal state of a pipeline run."""

    COMPLETED = "completed"
    NEEDS_CLARIFICATION = "needs_clarification"
    PRECONDITION_FAILED = "precondition_failed"
    SAFETY_CANCELLED = "safety_cancelled"
    BACKEND_UNAVAILABLE = "backend_unavailable"
    EXECUTION_FAILED = "execution_failed"


@dataclass
class StageRecord:
    """What one stage did and how long it took."""

    stage: Stage
    ok: bool
    seconds: float
    summary: str
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form, for checkpoints and the audit trail."""
        return {
            "stage": self.stage.value,
            "ok": self.ok,
            "seconds": round(self.seconds, 6),
            "summary": self.summary,
            "detail": self.detail,
        }


@dataclass
class PipelineResult:
    """Everything one pipeline run produced."""

    task_id: str
    task: str
    status: PipelineStatus
    stages: List[StageRecord] = field(default_factory=list)
    clarification: Optional[ClarificationReport] = None
    model_choice: Optional[ModelChoice] = None
    preconditions: Optional[PreconditionReport] = None
    safety_verdicts: List[CommandVerdict] = field(default_factory=list)
    checkpoint_policy: Optional[CheckpointPolicy] = None
    recovery_menu: Optional[RecoveryMenu] = None
    execution: Optional[ExecutionReport] = None
    rollback: Optional[RollbackReport] = None
    decisions: List[Dict[str, Any]] = field(default_factory=list)
    audit_path: Optional[str] = None
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        """True only when the pipeline reached the end without an early exit."""
        return self.status is PipelineStatus.COMPLETED

    @property
    def stages_completed(self) -> List[str]:
        """Stage names that actually ran, in order."""
        return [s.stage.value for s in self.stages]

    def stopped_at_stage_number(self) -> int:
        """1-based index of the last non-audit stage that ran."""
        ran = [s.stage for s in self.stages if s.stage is not Stage.AUDIT_LOG]
        return STAGE_ORDER.index(ran[-1]) + 1 if ran else 0

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form, for checkpoints and the audit trail."""
        return {
            "task_id": self.task_id,
            "task": self.task,
            "status": self.status.value,
            "ok": self.ok,
            "stages": [s.to_dict() for s in self.stages],
            "stages_completed": self.stages_completed,
            "stopped_at_stage_number": self.stopped_at_stage_number(),
            "clarification": self.clarification.to_dict() if self.clarification else None,
            "model_choice": self.model_choice.to_dict() if self.model_choice else None,
            "preconditions": self.preconditions.to_dict() if self.preconditions else None,
            "safety_verdicts": [v.to_dict() for v in self.safety_verdicts],
            "checkpoint_policy": (
                {**asdict(self.checkpoint_policy),
                 "length": self.checkpoint_policy.length.value}
                if self.checkpoint_policy else None
            ),
            "recovery_menu": (
                {
                    "status": self.recovery_menu.status.value,
                    "reason": self.recovery_menu.reason,
                    "options": [asdict(o) for o in self.recovery_menu.options],
                    "built_in_seconds": round(self.recovery_menu.built_in_seconds, 6),
                }
                if self.recovery_menu else None
            ),
            "execution": self.execution.to_dict() if self.execution else None,
            "rollback": self.rollback.to_dict() if self.rollback else None,
            "decisions": self.decisions,
            "seconds": round(self.seconds, 6),
        }


class Phase1IntegratedExecutor:
    """The full Phase 1 pipeline.

    Every collaborator is injected so a test can drive the whole 8 stage flow
    with no live Ollama, no systemd, and no network.
    """

    def __init__(
        self,
        workdir: Path | str,
        clarifier: Optional[AskClarifyingQuestionsBehavior] = None,
        selector: Optional[ModelSelector] = None,
        verifier: Optional[VerifyStateBehavior] = None,
        safety: Optional[SafetyFirstBehavior] = None,
        rollback: Optional[RollbackOnFailBehavior] = None,
        transparency: Optional[TransparencyBehavior] = None,
        checkpointer: Optional[AdaptiveCheckpointing] = None,
        conflicts: Optional[ConflictDetector] = None,
        compressor: Optional[ContextCompressor] = None,
        ollama: Optional[OllamaFallbackHandler] = None,
    ) -> None:
        """Args: workdir: root for checkpoints and audit files. Every other
        argument is an injectable collaborator; omit to get the default.
        """
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.audit_dir = self.workdir / "audit"
        self.audit_dir.mkdir(parents=True, exist_ok=True)

        self.clarifier = clarifier or AskClarifyingQuestionsBehavior()
        self.selector = selector or ModelSelector()
        self.verifier = verifier or VerifyStateBehavior()
        self.safety = safety or SafetyFirstBehavior()
        self.rollback = rollback or RollbackOnFailBehavior()
        self.transparency = transparency or TransparencyBehavior()
        self.checkpointer = checkpointer or AdaptiveCheckpointing(
            self.workdir / "checkpoints"
        )
        self.conflicts = conflicts or ConflictDetector()
        self.compressor = compressor or ContextCompressor()
        self.ollama = ollama or OllamaFallbackHandler()

    # -- helpers ----------------------------------------------------------- #

    def _record(
        self,
        result: PipelineResult,
        stage: Stage,
        ok: bool,
        started: float,
        summary: str,
        detail: Optional[Dict[str, Any]] = None,
    ) -> StageRecord:
        rec = StageRecord(
            stage=stage,
            ok=ok,
            seconds=time.perf_counter() - started,
            summary=summary,
            detail=detail or {},
        )
        result.stages.append(rec)
        return rec

    def _finish(self, result: PipelineResult, started: float) -> PipelineResult:
        """Stage 8. Always runs. Writes the JSON audit trail."""
        stage_started = time.perf_counter()
        result.decisions = self.transparency.audit_trail()
        result.seconds = time.perf_counter() - started

        path = self.audit_dir / f"{result.task_id}.audit.json"
        payload: Dict[str, Any]
        try:
            payload = result.to_dict()
            path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
            result.audit_path = str(path)
            ok, summary = True, f"audit written to {path.name}"
        except Exception as exc:
            ok = False
            summary = f"audit write FAILED: {type(exc).__name__}: {exc}"
            self.transparency.log_decision(
                "audit_log", "audit write failed", summary,
            )

        self._record(result, Stage.AUDIT_LOG, ok, stage_started, summary,
                     {"decision_count": len(result.decisions)})
        # Re-serialize so the file also contains stage 8 itself.
        if result.audit_path:
            try:
                path.write_text(
                    json.dumps(result.to_dict(), indent=2, default=str), encoding="utf-8"
                )
            except Exception:
                pass
        return result

    # -- pipeline ---------------------------------------------------------- #

    def run(
        self,
        task_id: str,
        task: str,
        steps: Sequence[Step],
        commands: Optional[Sequence[str]] = None,
        rollback_actions: Optional[Sequence[tuple[str, Callable[[], Any], bool]]] = None,
        rollback_on_fail: bool = True,
    ) -> PipelineResult:
        """Run one task through all 8 stages.

        Args:
            task_id: Stable id used for checkpoint and audit filenames.
            task: Natural language task description.
            steps: Ordered executable steps.
            commands: Shell commands to run through safety analysis. Defaults
                to the `command` field of each step.
            rollback_actions: (description, undo, safe) tuples pushed in stage 5.
            rollback_on_fail: Unwind the stack when execution fails.

        Returns:
            A PipelineResult. This method does not raise for task level faults.
        """
        run_started = time.perf_counter()
        result = PipelineResult(task_id=task_id, task=task,
                                status=PipelineStatus.COMPLETED)

        # ---- Stage 1: clarify -------------------------------------------- #
        t = time.perf_counter()
        clar = self.clarifier.clarify(task)
        result.clarification = clar
        blocked = clar.needs_clarification and not clar.resolved
        self.transparency.log_decision(
            Stage.CLARIFY.value,
            "blocked pending answers" if blocked else "task is actionable",
            f"{len(clar.ambiguities)} ambiguity marker(s): "
            + (", ".join(a.code for a in clar.ambiguities) or "none"),
            {"questions": [a.question for a in clar.ambiguities]},
        )
        self._record(result, Stage.CLARIFY, not blocked, t,
                     f"{len(clar.ambiguities)} ambiguity marker(s)",
                     {"codes": [a.code for a in clar.ambiguities],
                      "resolved": clar.resolved})
        if blocked:
            result.status = PipelineStatus.NEEDS_CLARIFICATION
            return self._finish(result, run_started)

        effective_task = clar.refined_task or task

        # ---- Stage 2: model select --------------------------------------- #
        t = time.perf_counter()
        choice = self.selector.select(effective_task)
        result.model_choice = choice
        self.transparency.explain_model_choice(effective_task, choice)
        self._record(result, Stage.MODEL_SELECT, True, t,
                     f"routed to {choice.model}",
                     {"rule": choice.matched_rule, "confidence": choice.confidence})

        # ---- Stage 3: verify preconditions ------------------------------- #
        t = time.perf_counter()
        pre = self.verifier.run_all()
        result.preconditions = pre
        self.transparency.log_decision(
            Stage.VERIFY_PRECONDITIONS.value,
            "preconditions passed" if pre.passed else "preconditions FAILED",
            "; ".join(f"{r.name}={'ok' if r.passed else 'FAIL'}" for r in pre.results)
            or "no preconditions registered",
            {"failures": [r.name for r in pre.blocking_failures]},
        )
        self._record(result, Stage.VERIFY_PRECONDITIONS, pre.passed, t,
                     f"{len(pre.results) - len(pre.failures)}/{len(pre.results)} passed",
                     {"failures": [r.name for r in pre.failures]})
        if not pre.passed:
            result.status = PipelineStatus.PRECONDITION_FAILED
            return self._finish(result, run_started)

        # ---- Stage 4: safety analysis ------------------------------------ #
        t = time.perf_counter()
        to_check = list(commands) if commands is not None else [
            s.command for s in steps if s.command
        ]
        cancelled = False
        for command in to_check:
            verdict = self.safety.check(command)
            result.safety_verdicts.append(verdict)
            self.transparency.log_decision(
                Stage.SAFETY_ANALYSIS.value,
                f"{verdict.level.value}: {'BLOCKED' if verdict.blocked else 'allowed'}",
                "; ".join(verdict.reasons),
                {"command": command, "approved": verdict.approved},
            )
            if verdict.blocked:
                cancelled = True
                break
        self._record(result, Stage.SAFETY_ANALYSIS, not cancelled, t,
                     f"{len(result.safety_verdicts)} command(s) classified"
                     + (", 1 BLOCKED" if cancelled else ""),
                     {"levels": [v.level.value for v in result.safety_verdicts]})
        if cancelled:
            result.status = PipelineStatus.SAFETY_CANCELLED
            return self._finish(result, run_started)

        # ---- Stage 5: rollback prep -------------------------------------- #
        t = time.perf_counter()
        for description, undo, safe in (rollback_actions or []):
            self.rollback.push(description, undo, safe=safe)
        self.transparency.log_decision(
            Stage.ROLLBACK_PREP.value,
            f"rollback stack depth {self.rollback.depth}",
            "undo actions registered before any forward change is made",
            {"depth": self.rollback.depth},
        )
        self._record(result, Stage.ROLLBACK_PREP, True, t,
                     f"stack depth {self.rollback.depth}",
                     {"depth": self.rollback.depth})

        # ---- Stage 6: risk mitigation ------------------------------------ #
        t = time.perf_counter()
        policy = self.checkpointer.decide_frequency(steps)
        result.checkpoint_policy = policy

        tracked = sorted({p for s in steps for p in s.touches})
        if tracked:
            self.conflicts.baseline_files(tracked)

        status = self.ollama.health_check()
        self.transparency.log_decision(
            Stage.RISK_MITIGATION.value,
            f"checkpoint {policy.length.value}, backend {status.value}",
            f"estimated {policy.estimated_minutes} min over {len(steps)} step(s); "
            f"checkpoint every {policy.every_n_steps or 'never'}; "
            f"{len(tracked)} path(s) hashed; context budget "
            f"{self.compressor.max_tokens} tokens",
            {"tracked_paths": tracked, "backend": status.value},
        )
        backend_down = status is OllamaStatus.UNREACHABLE
        if backend_down:
            result.recovery_menu = self.ollama.build_recovery_menu(
                "health probe failed before execution",
                checkpoint_path=str(self.checkpointer.path_for(task_id)),
                current_model=choice.model,
            )
        self._record(result, Stage.RISK_MITIGATION, not backend_down, t,
                     f"{policy.length.value} policy, backend {status.value}",
                     {"every_n_steps": policy.every_n_steps,
                      "estimated_minutes": policy.estimated_minutes,
                      "tracked_paths": len(tracked)})
        if backend_down:
            result.status = PipelineStatus.BACKEND_UNAVAILABLE
            return self._finish(result, run_started)

        # ---- Stage 7: execute -------------------------------------------- #
        t = time.perf_counter()
        executor = CoreExecutor(
            checkpointer=self.checkpointer,
            conflicts=self.conflicts,
            compressor=self.compressor,
            ollama=self.ollama,
            model=choice.model,
        )
        execution = executor.execute(task_id, steps)
        result.execution = execution
        self.transparency.log_decision(
            Stage.EXECUTE.value,
            "execution succeeded" if execution.ok else "execution FAILED",
            execution.abort_reason or
            f"{len(execution.results)} step(s) completed in "
            f"{execution.seconds:.3f}s",
            {"checkpoints": execution.checkpoint_stats,
             "conflicts": len(execution.conflicts)},
        )
        self._record(result, Stage.EXECUTE, execution.ok, t,
                     f"{len(execution.results)} step(s), "
                     f"{'ok' if execution.ok else 'failed at ' + str(execution.aborted_at)}",
                     {"abort_reason": execution.abort_reason})

        if not execution.ok:
            result.status = PipelineStatus.EXECUTION_FAILED
            if rollback_on_fail and self.rollback.depth:
                report = self.rollback.rollback()
                result.rollback = report
                self.transparency.log_decision(
                    "rollback",
                    "rollback clean" if report.ok else "rollback incomplete",
                    f"{len(report.undone)} undone, {len(report.cancelled)} cancelled, "
                    f"{len(report.failed)} failed",
                    {"results": [r.to_dict() for r in report.results]},
                )
        else:
            self.rollback.clear()

        # ---- Stage 8: audit log ------------------------------------------ #
        return self._finish(result, run_started)
