"""Torra's step loop: arming, guarding, running and sweeping the steps of an approved plan.

Depends on: torra.core (ConflictDetector, ResumePoint, StepResult, TorraError, Conflict),
torra.core.resume (begin_step, end_step, interrupted_reason), torra.core.tasks (shell_output_sink),
torra.gates (the collaborators it declares), torra.pipeline_types (Outcome, Plan, RunResult, Stage,
StepPlan), torra.pipeline_rollback (drop_edited_undos, sweep), torra.read_memory
(remember_step_reads), torra.agent (Agent). `torra.pipeline.Torra` inherits `StepRunner`; its
`execute()` calls these in order.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from collections.abc import Set as AbstractSet
from pathlib import Path
from typing import Any

from .agent import Agent
from .core import Conflict, ConflictDetector, ResumePoint, StepResult, TorraError
from .core.resume import begin_step, end_step, interrupted_reason
from .core.tasks import shell_output_sink
from .gates import DecisionLog, StateVerifier, UndoAction, UndoGenerator, VerifiedRollback
from .pipeline_rollback import drop_edited_undos, sweep
from .pipeline_types import Outcome, Plan, RunResult, Stage, StepPlan
from .read_memory import remember_step_reads

__all__ = ["StepRunner"]


class StepRunner:
    """The step loop `Torra.execute()` drives once a plan is approved and its rollback staged."""

    _armed: bool = False  # a real step was armed in this attempt (see `_arm_step`)

    agent: Agent
    conflicts: ConflictDetector
    decisions: DecisionLog
    recheck_between_steps: bool
    rollback: VerifiedRollback
    undo: UndoGenerator
    verifier: StateVerifier

    def _arm_step(
        self, sp: StepPlan, dry_run: bool, resume: ResumePoint, resume_path: Path
    ) -> None:
        """Right before a step runs: its derived undos go on the stack and the resume file
        records it as in flight."""
        if not dry_run:
            self._armed = True
            self.agent.state = None  # the step may change the machine: observe it again next turn
            self.rollback.push_many(sp.undos)
            begin_step(resume, resume_path, sp.step.step_id)

    def _block_on_conflict(
        self, found: Sequence[Conflict], sp: StepPlan, result: RunResult
    ) -> None:
        """Mark the run BLOCKED and keep the sweep off the files that changed."""
        result.outcome = Outcome.BLOCKED
        result.abort_reason = (
            f"{len(found)} external file change(s) before {sp.step.step_id}: "
            + ", ".join(f"{c.path}={c.kind.value}" for c in found[:4])
        )
        self.decisions.log(Stage.EXECUTE.value, "external conflict", result.abort_reason)
        drop_edited_undos(self.rollback, self.decisions, found)

    def _run_steps(
        self,
        plan: Plan,
        result: RunResult,
        dry_run: bool,
        resume: ResumePoint,
        resume_path: Path,
        on_output: Callable[[str, str], None] | None = None,
        resumed_steps: AbstractSet[str] = frozenset(),
        interrupted: str = "",
    ) -> bool:
        """Run each step in order, True if it failed/blocked. Guards run
        inside this loop, not once before it: state can change mid-run."""
        for sp in plan.steps:
            if not dry_run and sp.step.step_id in resumed_steps:
                # Nothing to guard or run: this step's effects already
                # happened, for real, in an earlier attempt this exact
                # resume point vouches for (`_resolve_resume`) -- the
                # recheck/conflict guards below exist to catch state
                # changing since a step was *about to* run, which doesn't
                # apply to a step that isn't going to run at all this time.
                sp.result = StepResult(
                    sp.step.step_id, True, f"[resumed] {sp.step.command}", seconds=0.0
                )
                result.results.append(sp.result)
                result.executed = True
                continue

            if not sp.approved:
                # A declined step in an otherwise-mixed approval: skipped,
                # not run -- never reaches `_run_one_step`, so it can never
                # set `result.executed`. Not `result.executed = True` here
                # deliberately: this step genuinely never ran, unlike the
                # resumed case above (which vouches for a real prior run).
                sp.result = StepResult(
                    sp.step.step_id, True, f"[skipped] not approved: {sp.step.command}", seconds=0.0
                )
                result.results.append(sp.result)
                continue

            if sp.step.step_id == interrupted:
                result.outcome = Outcome.BLOCKED
                result.abort_reason = interrupted_reason(interrupted, resume_path)
                return True

            if self.recheck_between_steps:
                veto = self.verifier.guard()
                if veto:
                    result.outcome = Outcome.BLOCKED
                    result.abort_reason = veto
                    self.decisions.log(Stage.EXECUTE.value, "stopped mid-run", veto)
                    return True

            found = (
                self.conflicts.detect() if (self.conflicts.hashes or self.conflicts.roots) else []
            )
            if found:
                self._block_on_conflict(found, sp, result)
                return True

            self._arm_step(sp, dry_run, resume, resume_path)
            sp.result = self._run_one_step(sp, result, dry_run, on_output)
            end_step(resume, resume_path, sp.step.step_id, sp.step.command, sp.result.ok, dry_run)
            self.conflicts.refresh(sp.step.touches)  # its own writes, even a failed step's

            if not sp.result.ok:
                result.outcome = Outcome.FAILED
                result.abort_reason = f"{sp.step.step_id} failed: {sp.result.error}"
                self.decisions.log(Stage.EXECUTE.value, "step failed", result.abort_reason)
                return True
        return False

    @staticmethod
    def _run_one_step(
        sp: StepPlan,
        result: RunResult,
        dry_run: bool,
        on_output: Callable[[str, str], None] | None = None,
    ) -> StepResult:
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
                    raise TorraError(f"step {sp.step.step_id!r} has no run action attached")
                step_id = sp.step.step_id
                sink = (lambda chunk: on_output(step_id, chunk)) if on_output is not None else None
                with shell_output_sink(sink):
                    output = sp.step.run()
            step_result = StepResult(
                sp.step.step_id, True, str(output or ""), seconds=time.perf_counter() - step_started
            )
        except (Exception, KeyboardInterrupt) as exc:
            # Broad on purpose: whatever a step's `run` raises, Ctrl-C included,
            # becomes a failed StepResult instead of propagating out of
            # execute(), so the caller still reaches the rollback sweep. An
            # uncaught exception here would skip rollback entirely.
            step_result = StepResult(
                sp.step.step_id,
                False,
                "",
                f"{type(exc).__name__}: {exc}",
                time.perf_counter() - step_started,
            )
        result.results.append(step_result)
        return step_result

    def _sweep_rollback(
        self, result: RunResult, resume_path: Path, held: Sequence[UndoAction]
    ) -> None:
        """Undo everything pushed so far, if there's anything to undo.

        Drops the resume file only when a rollback was attempted (complete or not): that
        reversed, or tried to reverse, steps the file would vouch for as done, and a retry
        must not skip steps it can no longer trust are still applied (HANDOFF.md §6.4). A
        block or failure that rolled nothing back keeps it, with its record of what finished."""
        sweep(self.rollback, self.decisions, result, held, self._armed, self.conflicts)
        if result.rollback is not None:
            try:
                resume_path.unlink(missing_ok=True)
            except OSError:
                pass

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
            remember_step_reads(self.agent.read_memory, result)
            try:
                resume_path.unlink(missing_ok=True)
            except OSError:
                pass
        self.decisions.log(Stage.EXECUTE.value, "completed", f"{len(result.results)} step(s) ran")
