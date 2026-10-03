"""One resume point per task: a durable record of how far it got, and
whether a fresh attempt should trust it.

Depends on: torra.core.tasks (TorraError).

`Torra.execute` writes one of these after each completed step and deletes
it on any terminal outcome (success, or a failure that triggered rollback --
see `resolve_resume`'s docstring for why). `resolve_resume` is what makes
`load()` more than forensic state: called once per real (non-dry-run)
`execute()`, before its step loop, to decide which of a plan's steps -- if
any -- a crash-left resume point vouches for as already done. HANDOFF.md
§6.4 has the full story of what this replaced (nothing) and what it must
not do (trust a resume point that doesn't provably describe this exact
plan).
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .tasks import TorraError

__all__ = ["ResumePoint", "ResumeResolution", "resolve_resume"]


@dataclass
class ResumePoint:
    """Where a task got to.

    This replaces the three-tier adaptive checkpointing policy from phases 1 and
    2, along with its mid-run revision logic and JSONL delta compaction, roughly
    300 lines in total. That machinery bought automatic resumability for
    unattended multi-hour runs. An agent turn is interactive and takes seconds,
    so a full re-run costs little -- one atomically written file after each
    completed step was judged enough of a crash-forensics trail, on the
    assumption that automatic resume wasn't worth building for a task this
    short. Nothing has actually exercised that assumption: `load()` has no
    caller, so today this is a write-only record, not resumability.
    """

    task_id: str
    completed: list[str] = field(default_factory=list)
    total: int = 0
    file_hashes: dict[str, str] = field(default_factory=dict)
    at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Serializable dict form."""
        return asdict(self)

    @classmethod
    def load(cls, path: os.PathLike[str] | str) -> ResumePoint | None:
        """Read a resume point, or None when absent."""
        # Raises TorraError when the file exists but is corrupt: resuming
        # from garbage is worse than starting over, so this is loud.
        target = Path(path)
        if not target.exists():
            return None
        try:
            return cls(**json.loads(target.read_text(encoding="utf-8")))
        except Exception as exc:
            raise TorraError(f"corrupt resume point {target}: {exc}") from exc

    def save(self, path: os.PathLike[str] | str) -> Path:
        """Write atomically: temp file then rename, so a crash cannot truncate."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.at = time.time()
        tmp = target.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
            os.replace(tmp, target)
        except OSError as exc:
            raise TorraError(f"resume point write failed: {exc!r}") from exc
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
        return target


@dataclass(frozen=True)
class ResumeResolution:
    """What `execute()` should do about a possible prior attempt.

    `note`, when set, is a human-readable explanation for the caller's own
    decision log -- kept here rather than logged directly by
    `resolve_resume` so this module stays decoupled from `pipeline_types`/
    `gates`' `DecisionLog`/`Stage` (the dependency direction throughout
    this package is `pipeline.py` -> `core`, never the other way).
    """

    resume: ResumePoint
    resumed_steps: frozenset[str]
    note: str | None = None


def resolve_resume(task_id: str, plan_step_ids: Sequence[str], total: int,
                   resume_path: os.PathLike[str] | str, dry_run: bool) -> ResumeResolution:
    """Load a crash-left resume point for this exact task_id, validating it
    against the current plan before trusting a single entry in it.

    A resume point only means anything when this task_id's step sequence is
    unchanged from the attempt that wrote it -- "resuming from garbage is
    worse than starting over" (`ResumePoint.load()`'s own docstring).
    Anything else -- a differently-shaped plan that happens to reuse the
    id, a corrupt file, a stale one a crashed rollback never got the
    chance to clear -- is ignored, and the caller starts fresh, same as if
    the file didn't exist. A dry run never even looks: it never writes one
    either (`Torra._run_steps`), so treating one as meaningful here would
    let a crash *during* a preview masquerade as steps that genuinely ran.
    """
    fresh = ResumePoint(task_id=task_id, total=total)
    if dry_run:
        return ResumeResolution(fresh, frozenset())
    try:
        existing = ResumePoint.load(resume_path)
    except TorraError as exc:
        return ResumeResolution(
            fresh, frozenset(), f"on-disk resume point is corrupt, starting fresh: {exc}")
    if existing is None:
        return ResumeResolution(fresh, frozenset())

    matches = (existing.task_id == task_id
              and existing.total == total
              and list(plan_step_ids)[:len(existing.completed)] == existing.completed)
    if not matches:
        return ResumeResolution(
            fresh, frozenset(),
            "an on-disk resume point exists for this task_id but its step "
            "sequence doesn't match the current plan -- starting fresh "
            "rather than trusting a possibly-unrelated record")

    resumed = ResumePoint(task_id=task_id, completed=list(existing.completed),
                          total=total, file_hashes=dict(existing.file_hashes))
    note = (f"{len(existing.completed)}/{total} step(s) already completed in an "
           "earlier attempt that didn't reach a clean finish; skipping them "
           "rather than re-running")
    return ResumeResolution(resumed, frozenset(existing.completed), note)
