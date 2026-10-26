"""Stage 7, report: the model's summary, the audit file and the telemetry record for one RunResult.

Depends on: torra.agent (Agent), torra.gates (DecisionLog), torra.pipeline_types (RunResult,
Stage), torra.telemetry (TaskLogger), torra.telemetry.scrub (redact_tree).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .agent import Agent
from .gates import DecisionLog
from .pipeline_types import RunResult, Stage
from .telemetry import TaskLogger
from .telemetry.scrub import redact_tree


def safe_filename(task_id: str) -> str:
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


def summarize(agent: Agent, decisions: DecisionLog, result: RunResult) -> str:
    """The model's own account of what its steps found, requested only
    when a model actually authored this plan -- same rule as telemetry:
    a failure here must never break the run it is reporting on."""
    assert result.plan is not None
    try:
        return agent.summarize(
            result.request, [sp.step for sp in result.plan.steps], result.results, result.plan.model
        )
    except Exception as exc:
        decisions.log(Stage.REPORT.value, "summary failed", f"{type(exc).__name__}: {exc}")
        return ""


def finish(
    result: RunResult,
    started: float,
    agent: Agent,
    decisions: DecisionLog,
    audit_dir: Path,
    telemetry: TaskLogger,
    write_audit: bool = True,
    dry_run: bool = False,
) -> RunResult:
    """Stage report. Always runs, even after an early exit."""
    # write_audit=False is the awaiting-approval case: the plan is
    # complete but the run is not, so the stage list is left open for
    # execute() to continue and no audit record is written yet.
    result.seconds = time.perf_counter() - started
    # Live wire: HANDOFF.md §5.12
    if not write_audit:
        result.decisions = decisions.trail()
        return result
    if Stage.REPORT.value not in result.stages:
        result.stages.append(Stage.REPORT.value)
    # Summarize before the trail() snapshot below, not after -- a failed
    # call logs its own decision (see summarize()), and a snapshot
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
        result.summary = summarize(agent, decisions, result)
    # Live wire: HANDOFF.md §5.23
    result.decisions = decisions.trail()

    # task_id_prefix (session-scoped, see HANDOFF.md §2.23/§2.26)
    # applies here too, not just to the plan path's model-visible
    # task_id -- without it, two tabs' first non-plan turn (answer/
    # ask/refuse) both landed on "turn-1", one silently overwriting
    # the other's audit file. _turn_counter, not len(agent.turns):
    # the two aren't interchangeable (Agent.repair() appends to
    # `turns` without incrementing the counter), and _turn_counter is
    # the one fast-forwarded past a reopened session's prior turns.
    task_id = (
        result.plan.spec.task_id
        if result.plan
        else f"{agent.task_id_prefix}turn-{agent._turn_counter}"
    )
    path = audit_dir / f"{safe_filename(task_id)}.audit.json"
    try:
        path.write_text(
            json.dumps(redact_tree(result.to_dict()), indent=2, default=str), encoding="utf-8"
        )
        result.audit_path = str(path)
    except Exception as exc:
        decisions.log("report", "audit write failed", f"{type(exc).__name__}: {exc}")

    record = telemetry.from_result(result, request=result.request)
    if record is not None:
        telemetry.record(record)
    return result
