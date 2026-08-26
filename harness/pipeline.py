"""Harness: the seven-stage orchestrator, the only thing that decides what runs.

Depends on: harness.agent (Agent), harness.core (task/step primitives, ModelBackend,
OllamaClient, ConflictDetector, ResumePoint), harness.gates (all five guardrails),
harness.pipeline_types (Stage, Outcome, StepPlan, Plan, RunResult -- the data model
this module produces).

converse/observe/propose/gate/present/execute/report. `observe`/`propose`
live inside `Agent.converse` -- the old `clarify` stage (a wall in front of
the operator) stopped making sense once a model could ask a better question.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .agent import Agent, AgentError, Intent, Turn
from .core import (
    ConflictDetector,
    HarnessError,
    ModelBackend,
    OllamaClient,
    OllamaError,
    ResumePoint,
    StepResult,
    TaskSpec,
)
from .gates import (
    Clarifier,
    DecisionLog,
    ModelRouter,
    RiskLevel,
    SafetyClassifier,
    StateVerifier,
    UndoGenerator,
    VerifiedRollback,
)
from .pipeline_types import Outcome, Plan, RunResult, Stage, StepPlan
from .telemetry import TaskLogger

__all__ = ["Stage", "Outcome", "StepPlan", "Plan", "RunResult", "Harness"]


class Harness:
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
            shadow_dir=self.workdir / f"harness-shadow-{os.getpid()}")
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
        self.knowledge_dir = self._resolve_knowledge_dir(knowledge_dir)

        self.agent = agent or Agent(
            ollama=self.ollama, router=self.router,
            clarifier=self.clarifier, safety=self.safety, knowledge_dir=self.knowledge_dir,
            backends=backends)

    @staticmethod
    def _resolve_knowledge_dir(explicit: str | None) -> str | None:
        """Explicit value (incl. `""` to disable) wins; else `HARNESS_KNOWLEDGE_DIR`."""
        return (explicit if explicit is not None else os.environ.get("HARNESS_KNOWLEDGE_DIR")) or None

    @staticmethod
    def _safe_filename(task_id: str) -> str:
        """A model- or human-chosen `task_id` as a single, contained path component.

        Found live: the resume-file path built `task_id` straight into
        `self.workdir / "resume" / f"{task_id}.json"` with no sanitizing at
        all -- unlike the audit path below, which already did this. A
        `task_id` of `"../../../../tmp/evil"` wrote a real file outside
        `workdir/resume/` on any run where a later step failed before
        cleanup. Replacing every character outside alnum/`-`/`_`/`.` is
        what makes this safe: `/` is the only character that can turn one
        path component into several, and it can no longer survive into the
        result -- a leftover `..` with no `/` around it is just a funny
        filename, not a traversal.
        """
        return "".join(c if c.isalnum() or c in "-_." else "_" for c in task_id)

    # -- stage 1 to 3: the model ------------------------------------------- #

    def plan(self, request: str, model: str | None = None,
            on_thinking: Callable[[str], None] | None = None) -> RunResult:
        """Stages converse, observe and propose. Runs nothing."""
        # Returns a RunResult whose outcome is AWAITING_APPROVAL when a gated
        # plan is attached, or ANSWERED / NEEDS_ANSWER / REFUSED / BACKEND_DOWN.
        #
        # A fresh task starts a fresh decision trail. Before the TUI could
        # keep one Harness alive across many turns (one tab, many
        # requests), this didn't need saying: a new Harness meant a new
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

    # -- stage 6: execute -------------------------------------------------- #

    def execute(self, result: RunResult, approvals: dict[str, bool] | None = None,
                dry_run: bool = False) -> RunResult:
        """Run an approved plan, updating and returning the same RunResult.
        Broken into the phase helpers below, called from here in order."""
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
        # how accidents happen.
        refused = self._apply_approvals(plan, approvals or {})
        if refused:
            result.outcome = Outcome.REJECTED
            result.abort_reason = ("not approved: "
                                   + ", ".join(sp.step.step_id for sp in refused))
            self.decisions.log(Stage.PRESENT.value, "plan rejected",
                               result.abort_reason)
            return self._finish(result, started)

        self._stage_rollback(plan)

        result.stages.append(Stage.EXECUTE.value)
        # A fresh attempt reports fresh results -- discard whatever a prior
        # dry run (or, now that the guard above permits it, a prior rejected
        # attempt -- though that one never populated results in the first
        # place) left behind, or a second dry run would append duplicate
        # stub entries alongside the first.
        result.results = []
        resume = ResumePoint(task_id=plan.spec.task_id, total=len(plan.steps))
        resume_path = self.workdir / "resume" / f"{self._safe_filename(plan.spec.task_id)}.json"

        failed = self._run_steps(plan, result, dry_run, resume, resume_path)

        if failed:
            self._sweep_rollback(result)
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

    def _stage_rollback(self, plan: Plan) -> None:
        """Push every step's derived undos, then the plan's declared ones."""
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

    def _run_steps(self, plan: Plan, result: RunResult, dry_run: bool,
                    resume: ResumePoint, resume_path: Path) -> bool:
        """Run each step in order, True if it failed/blocked. Guards run
        inside this loop, not once before it: state can change mid-run."""
        for sp in plan.steps:
            if self.recheck_between_steps:
                veto = self.verifier.guard()
                if veto:
                    result.outcome = Outcome.BLOCKED
                    result.abort_reason = veto
                    self.decisions.log(Stage.EXECUTE.value, "stopped mid-run", veto)
                    return True

            found = self.conflicts.detect() if (
                self.conflicts.hashes or self.conflicts.roots) else []
            if found:
                result.outcome = Outcome.BLOCKED
                result.abort_reason = (
                    f"{len(found)} external file change(s) before {sp.step.step_id}: "
                    + ", ".join(f"{c.path}={c.kind.value}" for c in found[:4]))
                self.decisions.log(Stage.EXECUTE.value, "external conflict",
                                   result.abort_reason)
                return True

            sp.result = self._run_one_step(sp, result, dry_run)

            if not sp.result.ok:
                result.outcome = Outcome.FAILED
                result.abort_reason = f"{sp.step.step_id} failed: {sp.result.error}"
                self.decisions.log(Stage.EXECUTE.value, "step failed",
                                   result.abort_reason)
                return True

            resume.completed.append(sp.step.step_id)
            self.conflicts.refresh(sp.step.touches)
            try:
                resume.save(resume_path)
            except Exception:
                pass  # a resume point is a nicety, not a reason to stop
        return False

    @staticmethod
    def _run_one_step(sp: StepPlan, result: RunResult, dry_run: bool) -> StepResult:
        """Run one step, converting any raise into a failed StepResult.
        Returns rather than assigns `sp.result` (mypy can't narrow a
        mutated attribute to non-None across a call boundary)."""
        step_started = time.perf_counter()
        try:
            if dry_run:
                output: Any = f"[dry-run] {sp.step.command}"
            else:
                # Set only here, not in execute() before the loop: a plan
                # blocked by the precondition/conflict checks above --
                # before any step ever ran -- must stay retryable the same
                # way a REJECTED plan does. Setting this unconditionally
                # before the loop marked that case "already executed" too,
                # the same bug §2.8 fixed for REJECTED, just missed for a
                # BLOCKED-with-zero-results plan. Found in a full-file
                # audit, not live.
                result.executed = True
                if sp.step.run is None:
                    raise HarnessError(
                        f"step {sp.step.step_id!r} has no run action attached")
                output = sp.step.run()
            step_result = StepResult(sp.step.step_id, True, str(output or ""),
                                     seconds=time.perf_counter() - step_started)
        except Exception as exc:
            # Broad on purpose: whatever a step's `run` raises becomes a
            # failed StepResult instead of propagating out of execute(), so
            # the caller still reaches the rollback sweep. An uncaught
            # exception here would skip rollback entirely.
            step_result = StepResult(sp.step.step_id, False, "",
                                     f"{type(exc).__name__}: {exc}",
                                     time.perf_counter() - step_started)
        result.results.append(step_result)
        return step_result

    def _sweep_rollback(self, result: RunResult) -> None:
        """Undo everything pushed so far, if there's anything to undo."""
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

    def _finish_success(self, result: RunResult, dry_run: bool, resume_path: Path) -> None:
        """Mark completed, drop rollback state, clean shadows, drop the resume file."""
        result.outcome = Outcome.COMPLETED
        self.rollback.clear()
        if not dry_run:
            # Nothing failed, so nothing rolls back to these snapshots.
            # Skipped for a dry run: capture_file() already ran during
            # _gate()'s propose-time snapshot, and a dry run must not blow
            # away the shadow copies the real run still needs.
            self.undo.cleanup()
        try:
            resume_path.unlink(missing_ok=True)
        except OSError:
            pass
        self.decisions.log(Stage.EXECUTE.value, "completed",
                           f"{len(result.results)} step(s) ran")

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

    def _summarize(self, result: RunResult) -> str:
        """The model's own account of what its steps found, requested only
        when a model actually authored this plan -- same rule as telemetry:
        a failure here must never break the run it is reporting on."""
        assert result.plan is not None
        try:
            return self.agent.summarize(
                result.request, [sp.step for sp in result.plan.steps],
                result.results, result.plan.model)
        except Exception as exc:
            self.decisions.log(Stage.REPORT.value, "summary failed",
                               f"{type(exc).__name__}: {exc}")
            return ""

    def _finish(self, result: RunResult, started: float,
                write_audit: bool = True, dry_run: bool = False) -> RunResult:
        """Stage report. Always runs, even after an early exit."""
        # write_audit=False is the awaiting-approval case: the plan is
        # complete but the run is not, so the stage list is left open for
        # execute() to continue and no audit record is written yet.
        result.seconds = time.perf_counter() - started
        if not write_audit:
            result.decisions = self.decisions.trail()
            return result
        if Stage.REPORT.value not in result.stages:
            result.stages.append(Stage.REPORT.value)
        # Summarize before the trail() snapshot below, not after -- a failed
        # call logs its own decision (see _summarize()), and a snapshot
        # taken first would silently drop that entry from both `result` and
        # the audit file, leaving no record of why summary ended up empty.
        #
        # dry_run excludes it entirely, not just labels it: _run_one_step()
        # fabricates "[dry-run] <command>" as every step's "output" under a
        # dry run -- nothing was actually checked. Found live: summarizing
        # that fabricated text got a confident, plausible-sounding answer
        # ("your SSH keys are present and correctly configured") with zero
        # real evidence behind it, indistinguishable from a genuine
        # post-execution summary to the operator reading it.
        if not dry_run and result.plan and result.plan.model and result.results:
            result.summary = self._summarize(result)
        result.decisions = self.decisions.trail()

        # task_id_prefix (session-scoped, see HANDOFF.md §2.23/§2.26)
        # applies here too, not just to the plan path's model-visible
        # task_id -- without it, two tabs' first non-plan turn (answer/
        # ask/refuse) both landed on "turn-1", one silently overwriting
        # the other's audit file. _turn_counter, not len(self.agent.turns):
        # the two aren't interchangeable (Agent.repair() appends to
        # `turns` without incrementing the counter), and _turn_counter is
        # the one fast-forwarded past a reopened session's prior turns.
        task_id = (result.plan.spec.task_id if result.plan
                   else f"{self.agent.task_id_prefix}turn-{self.agent._turn_counter}")
        path = self.audit_dir / f"{self._safe_filename(task_id)}.audit.json"
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
