"""Feeds a completed read-only step's real output into the model's own
context, tagged by the file path it came from, and lets the model later
say a path's content no longer helps.

Depends on: harness.core (ContextCompressor, ContextStep), harness.pipeline_types
(RunResult -- type-checking only, see the TYPE_CHECKING guard below for why a
real import would cycle back through harness.agent). Split out of
agent.py/pipeline.py, which mix conversation/execution *logic* with this one
self-contained mechanism -- both files crossed this project's 500-line
modularity threshold once it landed inline (a real, measured reason, the same
one that moved resolve_resume() into core/resume.py and dependency_warnings
into its own module -- see HANDOFF.md §2.33/§2.42).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from .core import ContextCompressor, ContextStep

if TYPE_CHECKING:
    # Not a real runtime import: harness.pipeline_types imports Turn from
    # harness.agent, and harness.agent imports ReadMemory from this module
    # -- an unconditional `from .pipeline_types import RunResult` here
    # would complete the cycle (agent -> read_memory -> pipeline_types ->
    # agent) and fail at interpreter startup. `from __future__ import
    # annotations` above defers every *annotation* in this file to a
    # string, but does nothing about this being a real top-level import
    # statement -- the TYPE_CHECKING guard is what actually breaks the
    # cycle; mypy still resolves the real type since it evaluates this
    # branch, Python itself never does.
    from .pipeline_types import RunResult

__all__ = ["ReadMemory", "remember_step_reads"]


class ReadMemory:
    """Tracks path -> context-entry-id for a read-only step's real output,
    so a later `Turn.forget` can find and remove exactly that entry.

    A thin wrapper around one `Agent`'s own `ContextCompressor`, not a
    second store -- `remember`/`forget` mutate `context.entries` directly
    through `ContextCompressor.add`/`.remove`, this class only remembers
    which entry id belongs to which path.
    """

    #: Read content is capped for the same reason the TUI's live step-output
    #: view caps its own display -- a single huge file would either blow
    #: ContextCompressor's own per-entry budget (`ContextBudgetError`) or
    #: crowd out everything else once compression kicks in. Truncated with
    #: a note, not silently.
    CAP: int = 8000

    def __init__(self, context: ContextCompressor,
                on_change: Callable[[ContextStep], None] | None = None) -> None:
        self.context = context
        self._on_change = on_change
        self.paths: dict[str, str] = {}

    def remember(self, path: str, content: str) -> None:
        """Add a read-only step's real output to context, tagged by the
        path it came from. Re-reading the same path replaces its own
        prior entry rather than accumulating a second one, so re-running
        the same grep/cat step across turns doesn't silently double what's
        remembered about it."""
        try:
            existing = self.paths.get(path)
            if existing:
                self.context.remove(existing)
            shown = content if len(content) <= self.CAP else (
                content[:self.CAP] + f"\n[truncated, {len(content) - self.CAP} more bytes]")
            entry_id = f"read:{path}"
            step = self.context.add(entry_id, "system", f"[read {path}]\n{shown}")
            if self._on_change:
                self._on_change(step)
            self.paths[path] = entry_id
            self.context.maybe_compress()
        except Exception:
            pass  # context bookkeeping must never break a turn

    def forget(self, paths: Sequence[str]) -> list[str]:
        """Drop the context entries `Turn.forget` named -- returns the
        paths actually found and removed, for the decision/audit trail
        (a path already gone, e.g. compressed away since it was read, is
        not an error, just nothing to do)."""
        forgotten = []
        for path in paths:
            entry_id = self.paths.pop(path, None)
            if entry_id and self.context.remove(entry_id):
                forgotten.append(path)
        return forgotten

    def reset(self) -> None:
        """Forget every path -- called by `Agent.reset()` alongside
        clearing the context entries themselves."""
        self.paths.clear()


def remember_step_reads(memory: ReadMemory, result: RunResult) -> None:
    """Feed every completed step's real output into `memory`, for each
    path it declared in `reads`. Called by `Harness._finish_success()`,
    never for a dry run (its output is a `[dry-run] ...` placeholder, not
    real content) and never for a skipped-as-declined or failed step
    (`sp.result.ok` is False for both, and `sp.result` is None for a step
    the plan never reached). A step declaring more than one `reads` path
    gets the same output associated with each -- an accepted imprecision,
    the same shape `dependency_warnings`'s own docstring already accepts
    for a different reason: attributing a step's real output to exactly
    the right one of several declared paths isn't knowable from the
    harness's side without executing each read separately, which `reads`
    was never meant to require."""
    if result.plan is None:
        return
    for sp in result.plan.steps:
        if not sp.step.reads or sp.result is None or not sp.result.ok:
            continue
        for path in sp.step.reads:
            memory.remember(path, sp.result.output)
