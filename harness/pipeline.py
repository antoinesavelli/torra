"""
harness.pipeline
Seven stages, with the model inside them rather than bolted on the front.

The old pipeline had eight stages and assumed a human had already written the
task. Its first stage, `clarify`, was a wall: it inspected the operator's
sentence and refused before anything else happened. That made sense when the
sentence was the whole input. It makes no sense once a model is sitting there
able to ask a better question than a regex can.

So the shape changed:

    converse   the model reads the request, consults the clarifier for what is
               structurally missing, and either answers, asks, or proposes
    observe    real machine state, read-only, gathered before any proposal
    propose    the model emits a TaskSpec, validated against the same schema a
               hand-written task file uses
    gate       safety classification, preconditions, undo generation, conflict
               baseline. Every command, whoever wrote it
    present    the operator sees the plan AND every gate verdict, and approves
               per step or not at all
    execute    with rollback armed and preconditions rechecked between steps
    report     back into the conversation and into telemetry

`observe` and `propose` live inside `Agent.converse`, which is why the stage
count went down rather than up: the model absorbed work the pipeline used to do
badly on its own.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from .agent import Agent, AgentError, Intent, Turn
from .core import (
    Conflict, ConflictDetector, ContextCompressor, OllamaClient, OllamaError,
    ResumePoint, Step, StepResult, TaskSpec,
)
from .gates import (
    Clarifier, DecisionLog, ModelRouter, PreconditionReport, RiskLevel,
    RollbackReport, SafetyClassifier, StateVerifier, UndoAction, UndoGenerator,
    Verdict, VerifiedRollback,
)

__all__ = ["Stage", "Outcome", "StepPlan", "Plan", "RunResult", "Harness"]


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
    """One proposed step with everything the gates decided about it.

    This is what the operator actually looks at. A plan the model wrote is not
    interesting on its own; a plan annotated with what each gate thinks of every
    line is the thing worth approving or rejecting.
    """

    step: Step
    verdict: Verdict
    undos: List[UndoAction] = field(default_factory=list)
    approved: Optional[bool] = None
    result: Optional[StepResult] = None

    @property
    def needs_confirmation(self) -> bool:
        """True when this step cannot run without an explicit yes."""
        return self.verdict.needs_confirmation

    @property
    def has_undo(self) -> bool:
        """True when failing this step can be reversed."""
        return bool(self.undos)

    def to_dict(self) -> Dict[str, Any]:
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
    """A proposal plus every gate verdict on it, ready to show a human."""

    spec: TaskSpec
    steps: List[StepPlan] = field(default_factory=list)
    preconditions: Optional[PreconditionReport] = None
    conflicts: List[Conflict] = field(default_factory=list)
    coverage: Dict[str, Any] = field(default_factory=dict)
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
    def needs_confirmation(self) -> List[StepPlan]:
        """Steps that cannot run without an explicit yes."""
        return [sp for sp in self.steps if sp.needs_confirmation]

    @property
    def uncovered(self) -> List[str]:
        """Step ids with no derivable undo."""
        return list(self.coverage.get("uncovered", []))

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form, for the audit trail."""
        return {
            "spec": self.spec.to_dict(),
            "steps": [s.to_dict() for s in self.steps],
            "preconditions": self.preconditions.to_dict() if self.preconditions else None,
            "conflicts": [c.to_dict() for c in self.conflicts],
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
    stages: List[str] = field(default_factory=list)
    turn: Optional[Turn] = None
    plan: Optional[Plan] = None
    results: List[StepResult] = field(default_factory=list)
    rollback: Optional[RollbackReport] = None
    abort_reason: str = ""
    decisions: List[Dict[str, Any]] = field(default_factory=list)
    audit_path: str = ""
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        """True only when the task completed."""
        return self.outcome is Outcome.COMPLETED

    @property
    def message(self) -> str:
        """What to show the operator as the headline."""
        if self.turn and self.turn.message:
            return self.turn.message
        return self.abort_reason or self.outcome.value

    def to_dict(self) -> Dict[str, Any]:
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
            "decisions": self.decisions,
            "seconds": round(self.seconds, 3),
        }


class Harness:
    """The whole thing: model, gates, execution, audit.

    Construction is deliberately boring. Every collaborator has a working
    default, and every one is injectable, so the TUI, the CLI and the test suite
    all drive the same object rather than three parallel assemblies.
    """

    def __init__(
        self,
        workdir: Path | str = Path.home() / ".harness",
        ollama: Optional[OllamaClient] = None,
        agent: Optional[Agent] = None,
        safety: Optional[SafetyClassifier] = None,
        verifier: Optional[StateVerifier] = None,
        undo: Optional[UndoGenerator] = None,
        rollback: Optional[VerifiedRollback] = None,
        conflicts: Optional[ConflictDetector] = None,
        clarifier: Optional[Clarifier] = None,
        router: Optional[ModelRouter] = None,
        decisions: Optional[DecisionLog] = None,
        recheck_between_steps: bool = True,
    ) -> None:
        """
        Args:
            workdir: Holds audit records, resume points and shadow copies.
            ollama: Model backend. A real client by default.
            agent: Override the whole planning half.
            recheck_between_steps: Re-run preconditions between steps.
            (Everything else: the gate implementations, all injectable.)
        """
        self.workdir = Path(workdir)
        self.audit_dir = self.workdir / "audit"
        self.audit_dir.mkdir(parents=True, exist_ok=True)

        self.ollama = ollama or OllamaClient()
        self.safety = safety or SafetyClassifier()
        self.clarifier = clarifier or Clarifier()
        self.router = router or ModelRouter()
        self.verifier = verifier or StateVerifier()
        self.undo = undo or UndoGenerator(
            shadow_dir=self.workdir / f"harness-shadow-{__import__('os').getpid()}")
        self.rollback = rollback or VerifiedRollback()
        self.conflicts = conflicts or ConflictDetector()
        self.decisions = decisions or DecisionLog()
        self.recheck_between_steps = bool(recheck_between_steps)

        self.agent = agent or Agent(
            ollama=self.ollama, router=self.router,
            clarifier=self.clarifier, safety=self.safety)

    # -- stage 1 to 3: the model ------------------------------------------- #

    def plan(self, request: str, model: Optional[str] = None) -> RunResult:
        """Stages converse, observe and propose. Runs nothing.

        Returns:
            A RunResult whose outcome is AWAITING_APPROVAL when a gated plan is
            attached, or ANSWERED / NEEDS_ANSWER / REFUSED / BACKEND_DOWN.
        """
        started = time.perf_counter()
        result = RunResult(request=request, outcome=Outcome.AWAITING_APPROVAL)

        try:
            turn = self.agent.converse(request, model=model)
        except OllamaError as exc:
            result.outcome = Outcome.BACKEND_DOWN
            result.abort_reason = str(exc)
            self.decisions.log(
                Stage.CONVERSE.value, "backend unreachable", str(exc),
                {"recovery": [o.to_dict() for o in
                              self.ollama.recovery_options(model or "unknown")]})
            return self._finish(result, started)
        except AgentError as exc:
            result.outcome = Outcome.FAILED
            result.abort_reason = f"the model did not produce a usable plan: {exc}"
            self.decisions.log(Stage.PROPOSE.value, "unusable proposal", str(exc))
            return self._finish(result, started)

        result.turn = turn
        result.stages = [Stage.CONVERSE.value, Stage.OBSERVE.value]
        self.decisions.log(
            Stage.CONVERSE.value, f"intent {turn.intent.value}",
            turn.message or f"{turn.model} decided this needs "
                            f"{turn.intent.value}; structural gaps found: "
                            f"{turn.clarifier_gaps or 'none'}",
            {"model": turn.model, "routing": turn.routing_reason,
             "repairs": turn.repairs})

        if turn.intent is Intent.ANSWER:
            result.outcome = Outcome.ANSWERED
            return self._finish(result, started)
        if turn.intent is Intent.ASK:
            result.outcome = Outcome.NEEDS_ANSWER
            return self._finish(result, started)
        if turn.intent is Intent.REFUSE:
            result.outcome = Outcome.REFUSED
            return self._finish(result, started)

        result.stages.append(Stage.PROPOSE.value)
        assert turn.spec is not None  # guaranteed by Agent._parse
        result.plan = self._gate(turn.spec, turn)
        result.stages.append(Stage.GATE.value)

        if result.plan.preconditions and not result.plan.preconditions.passed:
            result.outcome = Outcome.BLOCKED
            result.abort_reason = "preconditions failed: " + ", ".join(
                r.name for r in result.plan.preconditions.blocking_failures)
            return self._finish(result, started)

        result.stages.append(Stage.PRESENT.value)
        return self._finish(result, started, write_audit=False)

    # -- stage 4: gate ----------------------------------------------------- #

    def _gate(self, spec: TaskSpec, turn: Optional[Turn] = None) -> Plan:
        """Classify every command, snapshot state, baseline the filesystem.

        Runs on every command regardless of who wrote it. The agent pre-screens
        its own proposals, but that is a courtesy to save the operator's
        attention, not a substitute for this.
        """
        steps = spec.to_steps()
        plan = Plan(spec=spec, model=turn.model if turn else "",
                    repairs=turn.repairs if turn else 0)

        for step in steps:
            verdict = self.safety.classify(step.command)
            undos = self.undo.for_step(step)
            plan.steps.append(StepPlan(step=step, verdict=verdict, undos=undos))
            self.decisions.log(
                Stage.GATE.value, f"{step.step_id}: {verdict.level.value}",
                "; ".join(verdict.reasons[:3]),
                {"command": step.command, "undos": len(undos)})

        plan.coverage = self.undo.coverage(steps)
        plan.preconditions = self.verifier.run()
        self.decisions.log(
            Stage.GATE.value,
            "preconditions passed" if plan.preconditions.passed else "preconditions FAILED",
            "; ".join(f"{r.name}={'ok' if r.passed else 'FAIL'}"
                      for r in plan.preconditions.results) or "none registered")

        tracked = sorted({t for s in steps for t in s.touches})
        self.conflicts.baseline(tracked, roots=spec.watch_roots or None)
        if tracked or spec.watch_roots:
            plan.conflicts = self.conflicts.detect()

        self.decisions.log(
            Stage.GATE.value, f"plan worst level {plan.worst_level.value}",
            f"{len(plan.needs_confirmation)} step(s) need confirmation; "
            f"undo coverage {plan.coverage.get('coverage_ratio', 0):.0%}; "
            f"uncovered: {plan.uncovered or 'none'}",
            {"tracked_paths": tracked})
        return plan

    # -- stage 6: execute -------------------------------------------------- #

    def execute(self, result: RunResult, approvals: Optional[Dict[str, bool]] = None,
                dry_run: bool = False) -> RunResult:
        """Run an approved plan.

        Args:
            result: The RunResult from `plan()`, carrying the gated Plan.
            approvals: Per-step-id yes or no. A step needing confirmation and
                absent from this map is treated as refused, because defaulting
                an unanswered dangerous command to yes is how accidents happen.
            dry_run: Execute nothing, but run every guard and record it.

        Returns:
            The same RunResult, updated in place and written to the audit trail.
        """
        started = time.perf_counter() - result.seconds
        plan = result.plan
        if plan is None:
            result.outcome = Outcome.FAILED
            result.abort_reason = "nothing to execute"
            return self._finish(result, started)

        decisions = approvals or {}
        for sp in plan.steps:
            sp.approved = (True if not sp.needs_confirmation
                           else bool(decisions.get(sp.step.step_id, False)))

        refused = [sp for sp in plan.steps if sp.approved is False]
        if refused:
            result.outcome = Outcome.REJECTED
            result.abort_reason = ("not approved: "
                                   + ", ".join(sp.step.step_id for sp in refused))
            self.decisions.log(Stage.PRESENT.value, "plan rejected",
                               result.abort_reason)
            return self._finish(result, started)

        for sp in plan.steps:
            self.rollback.push_many(sp.undos)
        for description, undo, safe in plan.spec.rollback_actions():
            self.rollback.add(description, undo, safe=safe)
        self.decisions.log(
            Stage.PRESENT.value, "approved",
            f"{len(plan.steps)} step(s) approved, rollback depth "
            f"{self.rollback.depth}")

        result.stages.append(Stage.EXECUTE.value)
        resume = ResumePoint(task_id=plan.spec.task_id, total=len(plan.steps))
        resume_path = self.workdir / "resume" / f"{plan.spec.task_id}.json"
        failed = False

        for index, sp in enumerate(plan.steps, start=1):
            if self.recheck_between_steps:
                veto = self.verifier.guard()
                if veto:
                    result.outcome = Outcome.BLOCKED
                    result.abort_reason = veto
                    failed = True
                    self.decisions.log(Stage.EXECUTE.value, "stopped mid-run", veto)
                    break

            found = self.conflicts.detect() if (
                self.conflicts.hashes or self.conflicts.roots) else []
            if found:
                result.outcome = Outcome.BLOCKED
                result.abort_reason = (
                    f"{len(found)} external file change(s) before {sp.step.step_id}: "
                    + ", ".join(f"{c.path}={c.kind.value}" for c in found[:4]))
                failed = True
                self.decisions.log(Stage.EXECUTE.value, "external conflict",
                                   result.abort_reason)
                break

            step_started = time.perf_counter()
            try:
                output = f"[dry-run] {sp.step.command}" if dry_run else sp.step.run()
                sp.result = StepResult(sp.step.step_id, True, str(output or ""),
                                       seconds=time.perf_counter() - step_started)
            except Exception as exc:
                sp.result = StepResult(sp.step.step_id, False, "",
                                       f"{type(exc).__name__}: {exc}",
                                       time.perf_counter() - step_started)
            result.results.append(sp.result)

            if not sp.result.ok:
                result.outcome = Outcome.FAILED
                result.abort_reason = f"{sp.step.step_id} failed: {sp.result.error}"
                failed = True
                self.decisions.log(Stage.EXECUTE.value, "step failed",
                                   result.abort_reason)
                break

            resume.completed.append(sp.step.step_id)
            self.conflicts.refresh(sp.step.touches)
            try:
                resume.save(resume_path)
            except Exception:
                pass  # a resume point is a nicety, not a reason to stop

        if failed:
            if self.rollback.depth:
                result.rollback = self.rollback.rollback()
                self.decisions.log(
                    Stage.EXECUTE.value,
                    "rollback clean" if result.rollback.ok else "rollback incomplete",
                    f"{len(result.rollback.undone)} undone, "
                    f"{len(result.rollback.unverified)} unverified, "
                    f"{len(result.rollback.cancelled)} cancelled, "
                    f"{len(result.rollback.failed)} failed",
                    {"results": [r.to_dict() for r in result.rollback.results]})
        else:
            result.outcome = Outcome.COMPLETED
            self.rollback.clear()
            try:
                resume_path.unlink(missing_ok=True)
            except OSError:
                pass
            self.decisions.log(Stage.EXECUTE.value, "completed",
                               f"{len(result.results)} step(s) ran")

        return self._finish(result, started)

    # -- convenience ------------------------------------------------------- #

    def run(self, request: str, approve: Optional[Callable[[Plan], Dict[str, bool]]] = None,
            model: Optional[str] = None, dry_run: bool = False) -> RunResult:
        """Plan and execute in one call.

        Args:
            request: What the operator asked for, in plain language.
            approve: Shown the gated plan, returns per-step approvals. When
                omitted, anything needing confirmation is refused.
            model: Override the routed model.
            dry_run: Run every gate and guard, execute nothing.
        """
        result = self.plan(request, model=model)
        if result.outcome is not Outcome.AWAITING_APPROVAL or result.plan is None:
            return result
        approvals = approve(result.plan) if approve else {}
        return self.execute(result, approvals, dry_run=dry_run)

    def answer(self, result: RunResult, answers: Sequence[str],
               model: Optional[str] = None) -> RunResult:
        """Answer a NEEDS_ANSWER result and re-plan."""
        if result.turn is None:
            return result
        combined = "; ".join(f"{q} -> {a}" for q, a in
                             zip(result.turn.questions, answers) if a)
        return self.plan(f"{result.request} [clarified: {combined}]", model=model)

    # -- stage 7: report --------------------------------------------------- #

    def _finish(self, result: RunResult, started: float,
                write_audit: bool = True) -> RunResult:
        """Stage report. Always runs, even after an early exit.

        `write_audit=False` is the awaiting-approval case: the plan is complete
        but the run is not, so the stage list is left open for execute() to
        continue and no audit record is written for a run that has not happened.
        """
        result.seconds = time.perf_counter() - started
        result.decisions = self.decisions.trail()
        if not write_audit:
            return result
        if Stage.REPORT.value not in result.stages:
            result.stages.append(Stage.REPORT.value)

        task_id = (result.plan.spec.task_id if result.plan
                   else f"turn-{len(self.agent.turns)}")
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in task_id)
        path = self.audit_dir / f"{safe}.audit.json"
        try:
            path.write_text(json.dumps(result.to_dict(), indent=2, default=str),
                            encoding="utf-8")
            result.audit_path = str(path)
        except Exception as exc:
            self.decisions.log("report", "audit write failed",
                               f"{type(exc).__name__}: {exc}")
        return result

    def stats(self) -> Dict[str, Any]:
        """Everything the harness knows about this session."""
        return {
            "agent": self.agent.stats(),
            "conflicts": self.conflicts.stats(),
            "verifier": self.verifier.stats(),
            "undo": self.undo.stats(),
            "decisions": len(self.decisions.records),
        }

    def close(self) -> None:
        """Release watches. Shadow copies are kept for post-mortem."""
        self.conflicts.close()
