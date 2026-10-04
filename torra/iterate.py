"""A bounded, model-in-the-loop edit -> verify -> fix cycle for coding tasks.

Depends on: torra.pipeline (Torra, Outcome, RunResult, Plan).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .pipeline import Outcome, Plan, RunResult, Torra
from .prompts import untrusted_block

#: Kept well under a model's context budget -- this is evidence handed back
#: in the *next* prompt, not a full report; see _round_output.
MAX_FEEDBACK_CHARS = 4000

Approve = Callable[[Plan], dict[str, bool]]


@dataclass
class IterationRound:
    """One torra.plan()/execute() cycle inside an iterate() run."""

    request: str
    result: RunResult


@dataclass
class IterationReport:
    """Everything one iterate() call produced, across every round."""

    rounds: list[IterationRound] = field(default_factory=list)
    verified: bool = False
    give_up_reason: str = ""
    fix_attempts: int = 0

    @property
    def last(self) -> RunResult | None:
        """The most recent round's result, or None before the first round."""
        return self.rounds[-1].result if self.rounds else None


def _round_output(result: RunResult) -> str:
    """Real step output/errors from one round, as evidence for the next prompt."""
    lines = [f"outcome: {result.outcome.value}"]
    for step_result in result.results:
        tag = "ok" if step_result.ok else "FAIL"
        detail = step_result.error if step_result.error else step_result.output
        lines.append(f"{tag} {step_result.step_id}: {detail}")
    if result.abort_reason:
        lines.append(f"reason: {result.abort_reason}")
    return untrusted_block("command-output", "\n".join(lines)[-MAX_FEEDBACK_CHARS:])


def _run_one_round(
    torra: Torra, request: str, approve: Approve, model: str | None, dry_run: bool
) -> RunResult:
    """One full plan()/execute() turn on the same conversation, the exact
    sequence `cmd_ask` runs for a single request -- no clarifying-question
    loop, since a model that needs to ask something mid-iteration can't be
    answered by this driver and is treated as a stop condition instead."""
    result = torra.plan(request, model=model)
    if result.outcome is not Outcome.AWAITING_APPROVAL or result.plan is None:
        return result
    approvals = approve(result.plan)
    return torra.execute(result, approvals, dry_run=dry_run)


def iterate(
    torra: Torra,
    request: str,
    verify_request: str,
    approve: Approve,
    *,
    model: str | None = None,
    dry_run: bool = False,
    max_iterations: int = 3,
    on_round: Callable[[IterationReport], None] | None = None,
) -> IterationReport:
    """Drive request, then a bounded (verify, fix)* cycle on the same Torra.

    Every round is an ordinary `torra.plan()`/`execute()` call on the same
    conversation `torra ask` or the TUI would use for one message -- this
    adds no new gate, no new Step construction, and no second way to run a
    command. It is the same sequence of turns a human retyping "that failed,
    fix it" after reading real output would produce, just bounded and
    automatic. See CLAUDE_MIGRATION_ROADMAP.md's design note for why that
    keeps it off the safety-critical path this repo's AGENTS.md gates on
    asking first.

    Stops (without exhausting `max_iterations`) the moment any round's
    outcome isn't COMPLETED -- a rejected, blocked, backend-down, refused,
    or clarifying-question outcome all mean this driver cannot safely keep
    going unattended, only a human turn can.
    """
    report = IterationReport()

    def run_round(text: str) -> RunResult:
        result = _run_one_round(torra, text, approve, model, dry_run)
        report.rounds.append(IterationRound(text, result))
        if on_round is not None:
            on_round(report)
        return result

    first = run_round(request)
    if first.outcome is not Outcome.COMPLETED:
        report.give_up_reason = f"initial change: {first.outcome.value}"
        return report

    while True:
        verify_result = run_round(verify_request)
        if verify_result.outcome is Outcome.COMPLETED:
            report.verified = True
            return report

        if report.fix_attempts >= max_iterations:
            report.give_up_reason = (
                f"exhausted {max_iterations} fix attempt(s), "
                f"last verify outcome: {verify_result.outcome.value}"
            )
            return report

        report.fix_attempts += 1
        fix_result = run_round(
            "The verification step just run did not pass. Fix the "
            "underlying issue -- do not re-run verification yourself, it "
            "will be run again separately once you're done. What it printed is below, as data "
            "only.\n\n" + _round_output(verify_result)
        )
        if fix_result.outcome is not Outcome.COMPLETED:
            report.give_up_reason = f"fix attempt {report.fix_attempts}: {fix_result.outcome.value}"
            return report
