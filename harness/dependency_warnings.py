"""Best-effort warnings about an approved step depending on a declined one.

Depends on: harness.pipeline_types (Plan). Split out of pipeline.py, which
mixes execute()'s own gate logic with this pre-execute, advisory-only check --
two different concerns that don't need to share a file, and keeping them
together was what pushed pipeline.py's functional LOC over this project's
500-line modularity threshold (the same real reason resolve_resume() moved
into core/resume.py -- see HANDOFF.md §2.33). Not under harness/gates/:
unlike the five real gates, this never blocks anything, so filing it
alongside them would misrepresent what it actually does.
"""

from __future__ import annotations

from .pipeline_types import Plan

__all__ = ["dependency_warnings"]


def dependency_warnings(plan: Plan, approvals: dict[str, bool]) -> list[str]:
    """Does an approved step's command mention a path a declined step's
    `touches` would have produced? Meant to be called by the caller
    (`cli.py`/`tui.py`) after collecting approvals and *before* calling
    `execute()`, so the operator sees this while they can still change
    their mind -- `execute()` itself only skips a declined step, it never
    looks ahead at what a later step assumes.

    A plain substring check, not a real dependency graph: the harness has
    no model of what one step's output means to another, and never will
    without becoming exactly the kind of open-ended "list of things to
    detect" HANDOFF.md §3 already warns against. This can neither catch
    every real dependency (a step that reads the file's *contents*, not
    its path, leaves no trace here) nor rule out a coincidental path
    match -- it is a hint to double-check, not a gate, and callers must
    not treat an empty result as a guarantee nothing was missed."""
    approved_ids = {
        sp.step.step_id for sp in plan.steps
        if not sp.needs_confirmation or approvals.get(sp.step.step_id, False)}
    declined = [sp for sp in plan.steps if sp.step.step_id not in approved_ids]
    declined_paths = {path: sp.step.step_id for sp in declined for path in sp.step.touches}
    if not declined_paths:
        return []
    warnings = []
    for sp in plan.steps:
        if sp.step.step_id not in approved_ids:
            continue
        for path, declined_id in declined_paths.items():
            if path in sp.step.command:
                warnings.append(
                    f"{sp.step.step_id} references {path}, which "
                    f"{declined_id} (declined) would have produced")
    return warnings
