"""Harness: the seven-stage orchestrator, the only thing that decides what runs.

Depends on: harness.agent (Agent), harness.core (task/step primitives, OllamaClient,
ConflictDetector, ResumePoint), harness.gates (all five guardrails).

converse/observe/propose/gate/present/execute/report. `observe` and `propose`
live inside `Agent.converse` -- the old pipeline had a `clarify` stage that
was a wall in front of the operator, which made sense before a model could ask
a better question than a regex, and makes no sense after.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .agent import Agent, AgentError, Intent, Turn
from .core import (
    ConflictDetector,
    HarnessError,
    OllamaClient,
    OllamaError,
    ResumePoint,
    Step,
    StepResult,
    TaskSpec,
)
from .gates import (
    Clarifier,
    DecisionLog,
    ModelRouter,
    PreconditionReport,
    RiskLevel,
    RollbackReport,
    SafetyClassifier,
    StateVerifier,
    UndoAction,
    UndoGenerator,
    Verdict,
    VerifiedRollback,
)
from .telemetry import TaskLogger

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

    Deliberately carries no `conflicts` field: `_gate()` used to call
    `conflicts.detect()` immediately after `conflicts.baseline()` on the
    same object, which always returned `[]` (detect() re-reads exactly
    the state baseline() had just captured), so a `Plan.conflicts` field
    could never hold anything either -- and the TUI still rendered a dead
    "changed outside the harness" banner that could never fire. Found in
    a full-file audit. Real conflict detection happens where it can
    actually see something: `Harness.execute()`'s per-step check, which
    surfaces as `Outcome.BLOCKED`.
    """

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
        workdir: Path | str | None = None,
        ollama: OllamaClient | None = None,
        agent: Agent | None = None,
        safety: SafetyClassifier | None = None,
        verifier: StateVerifier | None = None,
        undo: UndoGenerator | None = None,
        rollback: VerifiedRollback | None = None,
        conflicts: ConflictDetector | None = None,
        clarifier: Clarifier | None = None,
        router: ModelRouter | None = None,
        decisions: DecisionLog | None = None,
        telemetry: TaskLogger | None = None,
        recheck_between_steps: bool = True,
    ) -> None:
        """Wire the workdir, model backend, and gate implementations (all injectable)."""
        self.workdir = Path(workdir) if workdir is not None else Path.home() / ".harness"
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
        # Same path `harness label`/`harness evidence` (cli.py) already read
        # from -- found in a full-file audit that nothing ever wrote to it:
        # TaskLogger.record() had no caller anywhere in pipeline.py, agent.py,
        # or tui.py, so those commands could only ever report zero tasks,
        # no matter how much the harness was actually used. `_finish()` is
        # the one place every RunResult concludes, matching the audit
        # file's own choke point.
        self.telemetry = telemetry or TaskLogger(self.workdir / "telemetry" / "tasks.jsonl")
        self.recheck_between_steps = bool(recheck_between_steps)

        self.agent = agent or Agent(
            ollama=self.ollama, router=self.router,
            clarifier=self.clarifier, safety=self.safety)

    # -- stage 1 to 3: the model ------------------------------------------- #

    def plan(self, request: str, model: str | None = None) -> RunResult:
        """Stages converse, observe and propose. Runs nothing."""
        # Returns a RunResult whose outcome is AWAITING_APPROVAL when a gated
        # plan is attached, or ANSWERED / NEEDS_ANSWER / REFUSED / BACKEND_DOWN.
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

    def _gate(self, spec: TaskSpec, turn: Turn | None = None) -> Plan:
        """Classify every command, snapshot state, baseline the filesystem."""
        # Runs on every command regardless of who wrote it. The agent
        # pre-screens its own proposals, but that is a courtesy to save the
        # operator's attention, not a substitute for this.
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

        # baseline() captures the reference point execute()'s own per-step
        # checks compare against once real time (operator review) has
        # passed -- that's the actual enforcement, already covered by
        # test_an_external_edit_stops_the_run. Calling detect() again here,
        # immediately after the baseline it would compare against was just
        # captured on the line above, cannot ever find anything: it always
        # returned [], the same as plan.conflicts' own default. Found in a
        # full-file audit -- worse than merely dead, it cost a second full
        # filesystem walk of watch_roots on every single plan proposal
        # whenever inotify isn't available (ConflictDetector._detect_tree
        # falls straight to a full _reconcile() walk with no event queue to
        # check first).
        tracked = sorted({t for s in steps for t in s.touches})
        self.conflicts.baseline(tracked, roots=spec.watch_roots or None)

        self.decisions.log(
            Stage.GATE.value, f"plan worst level {plan.worst_level.value}",
            f"{len(plan.needs_confirmation)} step(s) need confirmation; "
            f"undo coverage {plan.coverage.get('coverage_ratio', 0):.0%}; "
            f"uncovered: {plan.uncovered or 'none'}",
            {"tracked_paths": tracked})
        return plan

    # -- stage 6: execute -------------------------------------------------- #

    def execute(self, result: RunResult, approvals: dict[str, bool] | None = None,
                dry_run: bool = False) -> RunResult:
        """Run an approved plan, updating and returning the same RunResult."""
        # A step needing confirmation and absent from `approvals` is treated
        # as refused -- defaulting an unanswered dangerous command to yes is
        # how accidents happen.
        started = time.perf_counter() - result.seconds
        plan = result.plan
        if plan is None:
            result.outcome = Outcome.FAILED
            result.abort_reason = "nothing to execute"
            return self._finish(result, started)

        if result.executed:
            # A second execute() call after a real (non-dry-run) attempt
            # already began would replay every step's command again --
            # harmless for a read-only `du`, but a real double-execution
            # risk for anything that mutates state. Nothing upstream
            # previously guarded against this: the TUI's Run/Dry-run
            # buttons stay enabled while a plan runs, so a double-click (or
            # a click landing just after completion) dispatches
            # execute_plan() twice on the same self.current. Found live in
            # an audit log with stages [...,"execute","report","execute"]
            # and two result entries for one step. Refusing here, once, in
            # the pipeline itself covers every caller (TUI, CLI, direct API
            # use) instead of patching each one's click handler.
            #
            # Deliberately keyed on `executed`, not `outcome`: a REJECTED
            # result (missing approval) or a dry-run-only result never ran
            # a real command, so ticking the box and clicking Run again
            # must still work. An outcome-based check blocked exactly that
            # ordinary retry-after-rejection flow -- found live right after
            # this guard first shipped.
            self.decisions.log(
                Stage.EXECUTE.value, "refused: already executed",
                f"this plan already reached outcome '{result.outcome.value}'; "
                "ignoring the repeat execute() call rather than re-running "
                "its commands")
            result.decisions = self.decisions.trail()
            return result

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
        for description, command, undo, declared_safe in plan.spec.rollback_actions():
            # A declared rollback entry only gets to make a command *more*
            # cautious than SafetyClassifier would, never less: the command
            # runs through the same gate a regular step does, and either
            # side calling it risky is enough to require confirmation.
            verdict = self.safety.classify(command)
            risky = verdict.level in (RiskLevel.DANGEROUS, RiskLevel.CRITICAL)
            effective_safe = declared_safe and not risky
            if declared_safe and risky:
                self.decisions.log(
                    Stage.PRESENT.value, "rollback safety override",
                    f"'{description}' was declared safe but classifies "
                    f"{verdict.level.value}: {verdict.reasons[0]}; will ask "
                    "before running it during rollback",
                    {"command": command})
            self.rollback.add(description, undo, safe=effective_safe)
        self.decisions.log(
            Stage.PRESENT.value, "approved",
            f"{len(plan.steps)} step(s) approved, rollback depth "
            f"{self.rollback.depth}")

        result.stages.append(Stage.EXECUTE.value)
        # A fresh attempt reports fresh results -- discard whatever a prior
        # dry run (or, now that the guard above permits it, a prior rejected
        # attempt -- though that one never populated results in the first
        # place) left behind, or a second dry run would append duplicate
        # stub entries alongside the first.
        result.results = []
        resume = ResumePoint(task_id=plan.spec.task_id, total=len(plan.steps))
        resume_path = self.workdir / "resume" / f"{plan.spec.task_id}.json"
        failed = False

        # Precondition and conflict checks run inside the loop, not once
        # before it: a service that was up when the operator approved the
        # plan can be dead by step two, and a file the plan depends on can
        # change under it mid-run. Either one aborts the whole run rather
        # than skipping the affected step, because a partially-run plan is
        # not the plan that was approved.
        for sp in plan.steps:
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
                if dry_run:
                    output: Any = f"[dry-run] {sp.step.command}"
                else:
                    # Set only here, not before the loop: a plan blocked by
                    # the precondition/conflict checks above -- before any
                    # step ever ran -- must stay retryable the same way a
                    # REJECTED plan does. Setting this unconditionally
                    # before the loop marked that case "already executed"
                    # too, the same bug §2.8 fixed for REJECTED, just
                    # missed for a BLOCKED-with-zero-results plan. Found in
                    # a full-file audit, not live.
                    result.executed = True
                    if sp.step.run is None:
                        raise HarnessError(
                            f"step {sp.step.step_id!r} has no run action attached")
                    output = sp.step.run()
                sp.result = StepResult(sp.step.step_id, True, str(output or ""),
                                       seconds=time.perf_counter() - step_started)
            except Exception as exc:
                # Broad on purpose: whatever a step's `run` raises becomes a
                # failed StepResult instead of propagating out of execute(),
                # so the loop below still reaches the rollback sweep. An
                # uncaught exception here would skip rollback entirely.
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
            if not dry_run:
                # Nothing failed, so nothing will ever roll back to these
                # snapshots -- `UndoGenerator.cleanup()` existed for exactly
                # this since it was written, but had no caller anywhere in
                # the codebase, which is how 14 shadow directories ended up
                # sitting on one real machine's disk. Skipped for a dry
                # run: no real step executed, but capture_file() already
                # ran during _gate()'s propose-time snapshot, so a dry run
                # blows away the very shadow copies the real run (if the
                # operator clicks Run next) would need for rollback.
                self.undo.cleanup()
            try:
                resume_path.unlink(missing_ok=True)
            except OSError:
                pass
            self.decisions.log(Stage.EXECUTE.value, "completed",
                               f"{len(result.results)} step(s) ran")

        return self._finish(result, started)

    def reject(self, result: RunResult) -> RunResult:
        """Explicitly decline a plan without running anything.

        Distinct from execute()'s implicit refusal, which only fires for
        steps that actually need confirmation: `execute(result, {})` on an
        all-safe plan (nothing needs a tick) does not refuse it, it *runs*
        it, since every step's `approved` defaults to True when
        `needs_confirmation` is False. An operator who clicks Reject means
        "don't run this," full stop, regardless of what it would have
        needed a checkbox for -- this is the only correct way to honor
        that for every plan, not just the ones with something to tick.
        Found in a full-file audit: the TUI's Reject button used to bypass
        the pipeline entirely (no audit record, no telemetry entry), which
        also meant "how often a human rejected a plan outright"
        (HANDOFF.md §6.1) could never be measured for the most direct
        expression of a rejection there is.
        """
        started = time.perf_counter() - result.seconds
        result.outcome = Outcome.REJECTED
        result.abort_reason = "operator rejected the plan"
        self.decisions.log(Stage.PRESENT.value, "plan rejected", result.abort_reason)
        return self._finish(result, started)

    # -- convenience ------------------------------------------------------- #

    def run(self, request: str, approve: Callable[[Plan], dict[str, bool]] | None = None,
            model: str | None = None, dry_run: bool = False) -> RunResult:
        """Plan and execute in one call; omitting `approve` refuses anything gated."""
        result = self.plan(request, model=model)
        if result.outcome is not Outcome.AWAITING_APPROVAL or result.plan is None:
            return result
        approvals = approve(result.plan) if approve else {}
        return self.execute(result, approvals, dry_run=dry_run)

    def answer(self, result: RunResult, answers: Sequence[str],
               model: str | None = None) -> RunResult:
        """Answer a NEEDS_ANSWER result and re-plan."""
        if result.turn is None:
            return result
        combined = "; ".join(f"{q} -> {a}" for q, a in
                             zip(result.turn.questions, answers, strict=True) if a)
        return self.plan(f"{result.request} [clarified: {combined}]", model=model)

    # -- stage 7: report --------------------------------------------------- #

    def _finish(self, result: RunResult, started: float,
                write_audit: bool = True) -> RunResult:
        """Stage report. Always runs, even after an early exit."""
        # write_audit=False is the awaiting-approval case: the plan is
        # complete but the run is not, so the stage list is left open for
        # execute() to continue and no audit record is written yet.
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

        record = self.telemetry.from_result(result, request=result.request)
        if record is not None:
            self.telemetry.record(record)
        return result

    def stats(self) -> dict[str, Any]:
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
