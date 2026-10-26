"""Torra: the seven-stage orchestrator, the only thing that decides what runs.

Depends on: torra.agent (Agent), torra.core (task/step primitives, ModelBackend,
OllamaClient, ConflictDetector, ResumePoint, build_ssh_runner, resolve_executor_config --
same optional second-machine config Agent resolves for observation, §2.52, reused here
for remote_pull/remote_push and their undo, §2.53), torra.core.remote_edit/.tasks
(escalate_remote_push, shell_output_sink -- neither re-exported from torra.core),
torra.gates (all five guardrails), torra.pipeline_steps (the step loop `execute` drives),
torra.pipeline_types (Stage, Outcome, StepPlan,
Plan, RunResult -- the data model this module produces), torra.pipeline_rollback
(which undos run, when, and which are skipped), torra.pipeline_report (stage 7: summary,
audit file, telemetry record), torra.dependency_warnings
(the advisory-only check `Torra.dependency_warnings` delegates to), torra.read_memory
(remember_step_reads, feeding a completed read step's real output into the model's context).

converse/observe/propose/gate/present/execute/report. `observe`/`propose`
live inside `Agent.converse` -- the old `clarify` stage (a wall in front of
the operator) stopped making sense once a model could ask a better question.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .agent import Agent, AgentError, Intent, Turn
from .core import (
    ConflictDetector,
    ModelBackend,
    OllamaClient,
    OllamaError,
    TaskSpec,
    build_ssh_runner,
    resolve_executor_config,
    resolve_resume,
)
from .core.remote_edit import escalate_remote_push, settle_remote_pull
from .core.resume import command_digest
from .dependency_warnings import dependency_warnings as _dependency_warnings
from .gates import (
    Clarifier,
    DecisionLog,
    ModelRouter,
    SafetyClassifier,
    StateVerifier,
    UndoAction,
    UndoGenerator,
    VerifiedRollback,
)
from .pipeline_report import finish, safe_filename
from .pipeline_rollback import declared_undos
from .pipeline_steps import StepRunner
from .pipeline_types import DEFAULT_WORKDIR, Outcome, Plan, RunResult, Stage, StepPlan
from .telemetry import TaskLogger

__all__ = ["DEFAULT_WORKDIR", "Stage", "Outcome", "StepPlan", "Plan", "RunResult", "Torra"]


class Torra(StepRunner):
    """The whole thing: model, gates, execution, audit. Construction is
    deliberately boring -- every collaborator has a working, injectable
    default, so the TUI, CLI and test suite drive one object, not three."""

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
        knowledge_dir: str | None = None,
        backends: dict[str, ModelBackend] | None = None,
        path_exists: Callable[[str], bool] | None = None,
    ) -> None:
        """Wire the workdir, model backend, and gate implementations (all injectable)."""
        # `path_exists` switches on the agent's missing-path check (`path_warnings`); None, the
        # default, leaves it off, so a test plan may name a path that only exists in the test.
        self.workdir = Path(workdir) if workdir is not None else DEFAULT_WORKDIR
        self.audit_dir = self.workdir / "audit"
        self.audit_dir.mkdir(parents=True, exist_ok=True)

        self.ollama = ollama or OllamaClient()
        self.safety = safety or SafetyClassifier()
        self.clarifier = clarifier or Clarifier()
        self.router = router or ModelRouter()
        self.verifier = verifier or StateVerifier()
        # None (the common case: TORRA_EXECUTOR_HOST unset) means every
        # remote_pull/remote_push step fails cleanly and every remote_push
        # step is honestly "uncovered" -- same config Agent already resolves
        # for read-only observation (§2.52), reused here, not re-resolved
        # independently.
        executor_config = resolve_executor_config()
        self._executor_runner = (
            build_ssh_runner(*executor_config) if executor_config is not None else None)
        self.undo = undo or UndoGenerator(
            shadow_dir=self.workdir / f"torra-shadow-{os.getpid()}",
            executor_runner=self._executor_runner)
        self.rollback = rollback or VerifiedRollback()
        self.conflicts = conflicts or ConflictDetector()
        self.decisions = decisions or DecisionLog()
        # Same path `torra label`/`torra evidence` (cli.py) already read
        # from -- found in a full-file audit that nothing ever wrote to it:
        # TaskLogger.record() had no caller anywhere in pipeline.py, agent.py,
        # or tui.py, so those commands could only ever report zero tasks,
        # no matter how much torra was actually used. `_finish()` is
        # the one place every RunResult concludes, matching the audit
        # file's own choke point.
        self.telemetry = telemetry or TaskLogger(self.workdir / "telemetry" / "tasks.jsonl")
        self.recheck_between_steps = bool(recheck_between_steps)
        self.knowledge_dir = self._resolve_knowledge_dir(knowledge_dir)

        self.agent = agent or Agent(
            ollama=self.ollama, router=self.router,
            clarifier=self.clarifier, safety=self.safety, knowledge_dir=self.knowledge_dir,
            backends=backends, path_exists=path_exists)

    # Live wire: HANDOFF.md §5.21
    @staticmethod
    def _resolve_knowledge_dir(explicit: str | None) -> str | None:
        """Explicit value (incl. `""` to disable) wins; else `TORRA_KNOWLEDGE_DIR`."""
        return (explicit if explicit is not None else os.environ.get("TORRA_KNOWLEDGE_DIR")) or None

    # -- stage 1 to 3: the model ------------------------------------------- #

    def plan(self, request: str, model: str | None = None,
            on_thinking: Callable[[str], None] | None = None) -> RunResult:
        """Stages converse, observe and propose. Runs nothing."""
        # Returns a RunResult whose outcome is AWAITING_APPROVAL when a gated
        # plan is attached, or ANSWERED / NEEDS_ANSWER / REFUSED / BACKEND_DOWN.
        #
        # A fresh task starts a fresh decision trail. Before the TUI could
        # keep one Torra alive across many turns (one tab, many
        # requests), this didn't need saying: a new Torra meant a new
        # DecisionLog for free. Found live once tabs made that assumption
        # false: without this, turn 2's audit record silently included
        # turn 1's decisions too, growing without bound for the life of
        # the tab. plan() -> execute() of the *same* task still correctly
        # share one trail (execute() never clears it) -- only a new plan()
        # call is a new task boundary.
        self.decisions.clear()
        started = time.perf_counter()
        result = RunResult(request=request, outcome=Outcome.AWAITING_APPROVAL)

        try:
            turn = self.agent.converse(request, model=model, on_thinking=on_thinking)
        except OllamaError as exc:
            return self._backend_down(result, started, model, exc)
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

    def _backend_down(self, result: RunResult, started: float,
                      model: str | None, exc: OllamaError) -> RunResult:
        """`plan()`'s OllamaError handler, split out to keep `plan()` itself
        under the CCN ceiling. Two different fallbacks for two different
        purposes: `model or ""` picks the backend that actually failed (an
        empty string matches no registered prefix, so it resolves to
        self.ollama -- correct, since a None model means routing was going
        to decide locally anyway); `model or "unknown"` is only the display
        string inside the logged RecoveryOption. "Restart Ollama" is exactly
        the wrong advice for an OpenRouter auth failure."""
        result.outcome = Outcome.BACKEND_DOWN
        result.abort_reason = str(exc)
        backend, _ = self.agent.resolve_backend(model or "")
        self.decisions.log(
            Stage.CONVERSE.value, "backend unreachable", str(exc),
            {"recovery": [o.to_dict() for o in
                          backend.recovery_options(model or "unknown")]})
        return self._finish(result, started)

    # -- stage 4: gate ----------------------------------------------------- #

    def _gate(self, spec: TaskSpec, turn: Turn | None = None) -> Plan:
        """Classify every command, snapshot state, baseline the filesystem."""
        # Runs on every command regardless of who wrote it. The agent
        # pre-screens its own proposals, but that is a courtesy to save the
        # operator's attention, not a substitute for this.
        steps = spec.to_steps(executor_runner=self._executor_runner)
        plan = Plan(spec=spec, model=turn.model if turn else "",
                    repairs=turn.repairs if turn else 0, warnings=list(turn.warnings) if turn else [])

        for step in steps:
            verdict = self.safety.classify(step.command)
            # Remote steps are torra-authored: their command text names no real
            # program, so their verdicts come from what they do instead.
            escalate_remote_push(verdict, step.kind)
            settle_remote_pull(verdict, step, self.safety.protects)
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

        # baseline() alone is the enforcement point -- execute()'s per-step
        # checks compare against it once real time has passed. A detect()
        # call immediately after baseline() here could never find anything
        # (same state), and cost a redundant full filesystem walk on every
        # proposal; removed in a full-file audit.
        tracked = sorted({t for s in steps for t in s.touches})
        self.conflicts.baseline(tracked, roots=spec.watch_roots or None)

        self.decisions.log(
            Stage.GATE.value, f"plan worst level {plan.worst_level.value}",
            f"{len(plan.needs_confirmation)} step(s) need confirmation; "
            f"undo coverage {plan.coverage.get('coverage_ratio', 0):.0%}; "
            f"uncovered: {plan.uncovered or 'none'}",
            {"tracked_paths": tracked})
        return plan

    #: Public: `Torra.dependency_warnings(plan, approvals)`. Delegates to
    #: torra.dependency_warnings -- see that module's docstring for why
    #: it's a separate file rather than a method body here.
    dependency_warnings = staticmethod(_dependency_warnings)

    # -- stage 6: execute -------------------------------------------------- #

    def execute(self, result: RunResult, approvals: dict[str, bool] | None = None,
                dry_run: bool = False,
                on_output: Callable[[str, str], None] | None = None) -> RunResult:
        """Run an approved plan, updating and returning the same RunResult.
        Broken into the phase helpers below, called from here in order.

        `on_output(step_id, chunk)`, when given, is called with each real
        step's live stdout/stderr as it arrives (see `shell_output_sink`).
        Never fires for a dry run -- dry-run steps never call `shell()` at
        all, they return a `[dry-run] ...` placeholder directly."""
        started = time.perf_counter() - result.seconds
        plan = result.plan
        if plan is None:
            result.outcome = Outcome.FAILED
            result.abort_reason = "nothing to execute"
            return self._finish(result, started)

        if result.executed:
            return self._refuse_replay(result)

        # A step needing confirmation and absent from `approvals` is treated
        # as refused -- defaulting an unanswered dangerous command to yes is
        # how accidents happen. The whole plan is only REJECTED when
        # *nothing* ends up approved; a mixed approval runs the approved
        # steps and skips the rest (`_run_steps`'s "not sp.approved" branch)
        # rather than blocking everything, including steps that never
        # needed a tick in the first place.
        refused = self._apply_approvals(plan, approvals or {})
        if refused and not any(sp.approved for sp in plan.steps):
            result.outcome = Outcome.REJECTED
            result.abort_reason = ("not approved: "
                                   + ", ".join(sp.step.step_id for sp in refused))
            self.decisions.log(Stage.PRESENT.value, "plan rejected",
                               result.abort_reason)
            return self._finish(result, started)
        if refused:
            self.decisions.log(
                Stage.PRESENT.value, "partial approval",
                f"{len(refused)} step(s) declined and will be skipped: "
                + ", ".join(sp.step.step_id for sp in refused))

        held = self._stage_rollback(plan)

        result.stages.append(Stage.EXECUTE.value)
        # A fresh attempt reports fresh results -- discard whatever a prior
        # dry run (or, now that the guard above permits it, a prior rejected
        # attempt -- though that one never populated results in the first
        # place) left behind, or a second dry run would append duplicate
        # stub entries alongside the first.
        result.results = []
        resume_path = self.workdir / "resume" / f"{safe_filename(plan.spec.task_id)}.json"
        resolution = resolve_resume(
            plan.spec.task_id,
            {sp.step.step_id: command_digest(sp.step.command) for sp in plan.steps},
            len(plan.steps), resume_path, dry_run)
        if resolution.note:
            self.decisions.log(
                Stage.EXECUTE.value,
                "resuming from a prior attempt" if resolution.resumed_steps
                else "resume point ignored", resolution.note)

        failed = self._run_steps(plan, result, dry_run, resolution.resume, resume_path,
                                 on_output, resolution.resumed_steps, resolution.interrupted)

        if failed:
            self._sweep_rollback(result, resume_path, held)
        else:
            self._finish_success(result, dry_run, resume_path)

        return self._finish(result, started, dry_run=dry_run)

    def _refuse_replay(self, result: RunResult) -> RunResult:
        """Refuse a second execute() call on a result that already ran for
        real. Keyed on `result.executed`, not `outcome` -- see §2.8."""
        self.decisions.log(
            Stage.EXECUTE.value, "refused: already executed",
            f"this plan already reached outcome '{result.outcome.value}'; "
            "ignoring the repeat execute() call rather than re-running "
            "its commands")
        result.decisions = self.decisions.trail()
        return result

    @staticmethod
    def _apply_approvals(plan: Plan, decisions: dict[str, bool]) -> list[StepPlan]:
        """Mark every step approved or refused; return the refused ones."""
        for sp in plan.steps:
            sp.approved = (True if not sp.needs_confirmation
                           else bool(decisions.get(sp.step.step_id, False)))
        return [sp for sp in plan.steps if sp.approved is False]

    def _stage_rollback(self, plan: Plan) -> list[UndoAction]:
        """Empty the undo stack and return the declared entries, unpushed (HANDOFF.md §2.59)."""
        self.rollback.clear()
        self._armed = False
        held = declared_undos(plan.spec, self.safety, self.decisions)
        approved = [sp for sp in plan.steps if sp.approved]
        self.decisions.log(
            Stage.PRESENT.value, "approved",
            f"{len(approved)} step(s) approved, "
            f"{sum(len(sp.undos) for sp in approved) + len(held)} undo(s) ready")
        return held

    def reject(self, result: RunResult) -> RunResult:
        """Explicitly decline a plan without running anything. Distinct
        from execute()'s implicit refusal (an all-safe plan runs, not
        refuses, under `execute(result, {})`). Refuses if `result.executed`
        is already True, so a late click can't overwrite a real outcome."""
        if result.executed:
            self.decisions.log(
                Stage.PRESENT.value, "refused: already executed",
                "a real run already began; cannot retroactively un-run it")
            result.decisions = self.decisions.trail()
            return result
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
                write_audit: bool = True, dry_run: bool = False) -> RunResult:
        """Stage report (`pipeline_report.finish`). Always runs, even after an early exit."""
        return finish(result, started, self.agent, self.decisions, self.audit_dir,
                      self.telemetry, write_audit, dry_run)

    def stats(self) -> dict[str, Any]:
        """Everything torra knows about this session."""
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
