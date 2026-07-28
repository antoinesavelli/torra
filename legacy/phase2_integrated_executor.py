"""
phase2_integrated_executor.py
Phase 2 pipeline. Same 8 stages, five subsystems swapped underneath.

Phase1IntegratedExecutor is untouched and still works. This subclass changes
what stages 3, 4, 6 and 7 are wired to:

    stage 3  verify_preconditions  -> ContinuousVerifyState, rechecked mid-run
    stage 4  safety_analysis       -> ParsingSafetyClassifier
    stage 6  risk_mitigation       -> IncrementalCheckpointing + tree watching
    stage 7  execute               -> DynamicCoreExecutor with guards

`Phase2IntegratedExecutor.from_config()` builds one from CORE_RULES.yaml so the
v1/v2 switch is configuration, not a code edit.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from harness_core import (
    ContextCompressor,
    OllamaFallbackHandler,
    OllamaStatus,
    Step,
)
from harness_core_v2 import (
    DynamicCoreExecutor,
    FilesystemConflictDetector,
    IncrementalCheckpointing,
    PolicyRevision,
)
from core_behaviors import (
    AskClarifyingQuestionsBehavior,
    ModelSelector,
    RollbackOnFailBehavior,
    TransparencyBehavior,
)
from core_behaviors_v2 import ContinuousVerifyState, ParsingSafetyClassifier
from phase1_integrated_executor import (
    Phase1IntegratedExecutor,
    PipelineResult,
    PipelineStatus,
    Stage,
)

__all__ = ["Phase2Result", "Phase2IntegratedExecutor"]


@dataclass
class Phase2Result(PipelineResult):
    """PipelineResult plus the Phase 2 telemetry."""

    policy_revisions: List[Dict[str, Any]] = field(default_factory=list)
    precondition_stats: Dict[str, Any] = field(default_factory=dict)
    mid_run_abort: Optional[Dict[str, Any]] = None
    resolved_commands: List[Dict[str, Any]] = field(default_factory=list)
    watched_roots: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form, for checkpoints and the audit trail."""
        base = super().to_dict()
        base.update({
            "phase": 2,
            "policy_revisions": self.policy_revisions,
            "precondition_stats": self.precondition_stats,
            "mid_run_abort": self.mid_run_abort,
            "resolved_commands": self.resolved_commands,
            "watched_roots": self.watched_roots,
        })
        return base


class Phase2IntegratedExecutor(Phase1IntegratedExecutor):
    """The Phase 1 pipeline with the Phase 2 subsystems wired in.

    Stage order, early exits, and the audit contract are all unchanged. What
    changes is that the guarantees behind them now hold mid-run rather than only
    at the moment each gate fired.
    """

    def __init__(
        self,
        workdir: Path | str,
        watch_roots: Optional[Iterable[Path | str]] = None,
        clarifier: Optional[AskClarifyingQuestionsBehavior] = None,
        selector: Optional[ModelSelector] = None,
        verifier: Optional[ContinuousVerifyState] = None,
        safety: Optional[ParsingSafetyClassifier] = None,
        rollback: Optional[RollbackOnFailBehavior] = None,
        transparency: Optional[TransparencyBehavior] = None,
        checkpointer: Optional[IncrementalCheckpointing] = None,
        conflicts: Optional[FilesystemConflictDetector] = None,
        compressor: Optional[ContextCompressor] = None,
        ollama: Optional[OllamaFallbackHandler] = None,
        recheck_between_steps: bool = True,
        compact_every: int = 25,
        step_timer: Optional[Callable[[], float]] = None,
    ) -> None:
        """
        Args:
            watch_roots: Directories watched in full by the conflict detector.
            recheck_between_steps: Re-run preconditions between steps.
            step_timer: Injectable monotonic timer for step duration, which
                is what drives policy revision. Omit for time.perf_counter.
            compact_every: Delta count before a checkpoint base is rewritten.
            (Other arguments as Phase1IntegratedExecutor, with Phase 2 defaults.)
        """
        workdir = Path(workdir)
        super().__init__(
            workdir=workdir,
            clarifier=clarifier,
            selector=selector,
            verifier=verifier or ContinuousVerifyState(),
            safety=safety or ParsingSafetyClassifier(),
            rollback=rollback,
            transparency=transparency,
            checkpointer=checkpointer or IncrementalCheckpointing(
                workdir / "checkpoints", compact_every=compact_every
            ),
            conflicts=conflicts or FilesystemConflictDetector(),
            compressor=compressor,
            ollama=ollama,
        )
        self.watch_roots: List[Path] = [Path(r) for r in (watch_roots or [])]
        self.recheck_between_steps = bool(recheck_between_steps)
        self.step_timer = step_timer

    # -- construction ------------------------------------------------------ #

    @classmethod
    def from_config(
        cls,
        config_path: Path | str,
        workdir: Path | str,
        **overrides: Any,
    ) -> "Phase2IntegratedExecutor":
        """Build a Phase 2 pipeline from CORE_RULES.yaml.

        Reads `risk_mitigations`, `core_behaviors`, and `model_routing`. Missing
        keys fall back to the Phase 2 defaults rather than raising, but a missing
        file is an error, because silently running with defaults you did not ask
        for is exactly the sort of thing this harness exists to prevent.
        """
        import yaml

        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"config not found: {path}")
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

        risks = data.get("risk_mitigations") or {}
        behaviors = data.get("core_behaviors") or {}
        cp_cfg = risks.get("adaptive_checkpointing") or {}
        ctx_cfg = risks.get("context_compression") or {}
        ollama_cfg = risks.get("ollama_fallback") or {}
        verify_cfg = behaviors.get("verify_state") or {}

        workdir = Path(workdir)
        checkpointer = IncrementalCheckpointing(
            workdir / "checkpoints",
            compact_every=int(cp_cfg.get("compact_every", 25)),
        )
        checkpointer.SHORT_MAX_MINUTES = float(cp_cfg.get("short_max_minutes", 5))
        checkpointer.MEDIUM_MAX_MINUTES = float(cp_cfg.get("medium_max_minutes", 30))
        checkpointer.MEDIUM_TARGET_CHECKPOINTS = int(
            cp_cfg.get("medium_target_checkpoints", 5))

        compressor = ContextCompressor(
            max_tokens=int(ctx_cfg.get("max_tokens", 32_000)),
            trigger_ratio=float(ctx_cfg.get("trigger_ratio", 0.90)),
            target_ratio=float(ctx_cfg.get("target_ratio", 0.45)),
            keep_recent=int(ctx_cfg.get("keep_recent", 3)),
        )
        ollama = OllamaFallbackHandler(
            host=ollama_cfg.get("host", OllamaFallbackHandler.DEFAULT_HOST),
            timeout=float(ollama_cfg.get("probe_timeout_seconds", 0.5)),
            fallback_model=ollama_cfg.get("fallback_model", "qwen3:8b"),
        )

        kwargs: Dict[str, Any] = dict(
            workdir=workdir,
            checkpointer=checkpointer,
            compressor=compressor,
            ollama=ollama,
            selector=ModelSelector.from_yaml(path),
            recheck_between_steps=bool(verify_cfg.get("recheck_between_steps", True)),
        )
        kwargs.update(overrides)
        return cls(**kwargs)

    # -- pipeline ---------------------------------------------------------- #

    def run(
        self,
        task_id: str,
        task: str,
        steps: Sequence[Step],
        commands: Optional[Sequence[str]] = None,
        rollback_actions: Optional[Sequence[tuple[str, Callable[[], Any], bool]]] = None,
        rollback_on_fail: bool = True,
    ) -> Phase2Result:
        """Run one task through all 8 stages with the Phase 2 guarantees.

        Returns:
            A Phase2Result. Same stage contract as Phase 1, plus policy
            revisions, precondition cache stats, resolved command traces, and
            the mid-run abort record when a guard fired.
        """
        run_started = time.perf_counter()
        result = Phase2Result(task_id=task_id, task=task,
                              status=PipelineStatus.COMPLETED)
        result.watched_roots = [str(r) for r in self.watch_roots]

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
            return self._finish2(result, run_started)

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
            {"failures": [r.name for r in pre.blocking_failures],
             "continuous": self.recheck_between_steps},
        )
        self._record(result, Stage.VERIFY_PRECONDITIONS, pre.passed, t,
                     f"{len(pre.results) - len(pre.failures)}/{len(pre.results)} passed"
                     + (", rechecked between steps" if self.recheck_between_steps else ""),
                     {"failures": [r.name for r in pre.failures]})
        if not pre.passed:
            result.status = PipelineStatus.PRECONDITION_FAILED
            return self._finish2(result, run_started)

        # ---- Stage 4: safety analysis ------------------------------------ #
        t = time.perf_counter()
        to_check = list(commands) if commands is not None else [
            s.command for s in steps if s.command
        ]
        cancelled = False
        for command in to_check:
            verdict = self.safety.check(command)
            result.safety_verdicts.append(verdict)
            if isinstance(self.safety, ParsingSafetyClassifier):
                result.resolved_commands.append({
                    "command": command,
                    "level": verdict.level.value,
                    "resolved": [rc.to_dict() for rc in self.safety.last_resolved],
                })
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
                     f"{len(result.safety_verdicts)} command(s) resolved and classified"
                     + (", 1 BLOCKED" if cancelled else ""),
                     {"levels": [v.level.value for v in result.safety_verdicts],
                      "parse_failures": len(
                          getattr(self.safety, "parse_failures", []))})
        if cancelled:
            result.status = PipelineStatus.SAFETY_CANCELLED
            return self._finish2(result, run_started)

        # ---- Stage 5: rollback prep -------------------------------------- #
        t = time.perf_counter()
        detail = self._rollback_prep(steps, rollback_actions)
        self.transparency.log_decision(
            Stage.ROLLBACK_PREP.value,
            f"rollback stack depth {self.rollback.depth}",
            "undo actions registered before any forward change is made"
            + (f"; {detail['generated']} generated from step state"
               if detail.get("generated") else ""),
            {"depth": self.rollback.depth, **detail},
        )
        self._record(result, Stage.ROLLBACK_PREP, True, t,
                     f"stack depth {self.rollback.depth}",
                     {"depth": self.rollback.depth, **detail})

        # ---- Stage 6: risk mitigation ------------------------------------ #
        t = time.perf_counter()
        policy = self.checkpointer.decide_frequency(steps)
        result.checkpoint_policy = policy

        tracked = sorted({p for s in steps for p in s.touches})
        if tracked:
            self.conflicts.baseline_files(tracked)
        if self.watch_roots and isinstance(self.conflicts, FilesystemConflictDetector):
            self.conflicts.baseline_tree(self.watch_roots)

        status = self.ollama.health_check()
        self.transparency.log_decision(
            Stage.RISK_MITIGATION.value,
            f"checkpoint {policy.length.value} (revisable), backend {status.value}",
            f"opening estimate {policy.estimated_minutes} min over {len(steps)} step(s); "
            f"checkpoint every {policy.every_n_steps or 'never'} and the policy will "
            f"be re-decided from observed timings after every step; "
            f"{len(tracked)} declared path(s) hashed, "
            f"{len(self.watch_roots)} tree(s) watched in full; context budget "
            f"{self.compressor.max_tokens} tokens",
            {"tracked_paths": tracked,
             "watched_roots": [str(r) for r in self.watch_roots],
             "backend": status.value},
        )
        backend_down = status is OllamaStatus.UNREACHABLE
        if backend_down:
            result.recovery_menu = self.ollama.build_recovery_menu(
                "health probe failed before execution",
                checkpoint_path=str(self.checkpointer.path_for(task_id)),
                current_model=choice.model,
            )
        self._record(result, Stage.RISK_MITIGATION, not backend_down, t,
                     f"{policy.length.value} opening policy, backend {status.value}",
                     {"every_n_steps": policy.every_n_steps,
                      "estimated_minutes": policy.estimated_minutes,
                      "tracked_paths": len(tracked),
                      "watched_roots": len(self.watch_roots)})
        if backend_down:
            result.status = PipelineStatus.BACKEND_UNAVAILABLE
            return self._finish2(result, run_started)

        # ---- Stage 7: execute -------------------------------------------- #
        t = time.perf_counter()
        guard = None
        if self.recheck_between_steps and isinstance(self.verifier, ContinuousVerifyState):
            guard = self.verifier.between_steps

        executor = DynamicCoreExecutor(
            checkpointer=self.checkpointer,
            conflicts=self.conflicts,
            compressor=self.compressor,
            ollama=self.ollama,
            model=choice.model,
            between_steps=guard,
            watch_roots=self.watch_roots,
            **({"timer": self.step_timer} if self.step_timer else {}),
        )
        execution = executor.execute(task_id, steps)
        result.execution = execution
        result.policy_revisions = [r.to_dict() for r in executor.policy_revisions]
        result.mid_run_abort = (
            executor.mid_run_abort.to_dict() if executor.mid_run_abort else None
        )
        if isinstance(self.verifier, ContinuousVerifyState):
            result.precondition_stats = self.verifier.stats()

        for revision in executor.policy_revisions:
            self.transparency.log_decision(
                "policy_revision",
                f"{revision.old_length.value} -> {revision.new_length.value} "
                f"at step {revision.at_step}",
                revision.reason,
                revision.to_dict(),
            )
        if executor.mid_run_abort:
            self.transparency.log_decision(
                "mid_run_guard",
                f"aborted at {executor.mid_run_abort.at_step}",
                executor.mid_run_abort.detail,
                executor.mid_run_abort.to_dict(),
            )

        self.transparency.log_decision(
            Stage.EXECUTE.value,
            "execution succeeded" if execution.ok else "execution FAILED",
            execution.abort_reason or
            f"{len(execution.results)} step(s) completed in {execution.seconds:.3f}s"
            + (f", policy revised {len(executor.policy_revisions)} time(s)"
               if executor.policy_revisions else ""),
            {"checkpoints": execution.checkpoint_stats,
             "conflicts": len(execution.conflicts)},
        )
        self._record(result, Stage.EXECUTE, execution.ok, t,
                     f"{len(execution.results)} step(s), "
                     f"{'ok' if execution.ok else 'failed at ' + str(execution.aborted_at)}"
                     + (f", {len(executor.policy_revisions)} policy revision(s)"
                        if executor.policy_revisions else ""),
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
        return self._finish2(result, run_started)

    def _rollback_prep(
        self,
        steps: Sequence[Step],
        rollback_actions: Optional[Sequence[tuple[str, Callable[[], Any], bool]]],
    ) -> Dict[str, Any]:
        """Stage 5 body. Pushes caller-supplied undo actions onto the stack.

        Split out as a hook so Phase 3 can derive undo actions from step state
        without reimplementing the whole pipeline.

        Returns:
            A detail dict merged into the stage record and the audit decision.
        """
        count = 0
        for description, undo, safe in (rollback_actions or []):
            self.rollback.push(description, undo, safe=safe)
            count += 1
        return {"caller": count, "generated": 0}

    def _finish2(self, result: Phase2Result, started: float) -> Phase2Result:
        """Stage 8 with the Phase 2 telemetry attached before serialization."""
        if isinstance(self.verifier, ContinuousVerifyState) and not result.precondition_stats:
            result.precondition_stats = self.verifier.stats()
        if isinstance(self.conflicts, FilesystemConflictDetector):
            try:
                self.conflicts.close()
            except Exception:
                pass
        self._finish(result, started)
        return result
