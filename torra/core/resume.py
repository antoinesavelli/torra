"""One resume point per task: a durable record of how far it got, and
whether a fresh attempt should trust it.

Depends on: torra.core.tasks (TorraError).

`Torra.execute` writes one before each step starts and after each one ends,
and deletes it on success or when a rollback ran (see `resolve_resume`'s
docstring for why). A step still marked in progress means the process died
mid-step, and the next attempt stops instead of running it again (HANDOFF.md §2.64). `resolve_resume` is what makes
`load()` more than forensic state: called once per real (non-dry-run)
`execute()`, before its step loop, to decide which of a plan's steps -- if
any -- a crash-left resume point vouches for as already done. HANDOFF.md
§6.4 has the full story of what this replaced (nothing) and what it must
not do (trust a resume point that doesn't provably describe this exact
plan).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .tasks import TorraError

__all__ = [
    "ResumePoint",
    "ResumeResolution",
    "resolve_resume",
    "command_digest",
    "begin_step",
    "end_step",
    "interrupted_reason",
]


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
    #: `command_digest` of each completed step's command, so a resume only vouches for a step
    #: whose command is unchanged. A file without it (the older format) starts fresh.
    digests: dict[str, str] = field(default_factory=dict)
    #: The step running right now, written before it starts. Left set by a crash.
    in_progress: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serializable dict form."""
        return asdict(self)

    def _well_typed(self) -> bool:
        """Whether every field has the type a resume decision relies on."""
        return (
            isinstance(self.task_id, str)
            and isinstance(self.total, int)
            and isinstance(self.in_progress, str)
            and isinstance(self.file_hashes, dict)
            and isinstance(self.digests, dict)
            and isinstance(self.completed, list)
            and all(isinstance(c, str) for c in self.completed)
        )

    @classmethod
    def load(cls, path: os.PathLike[str] | str) -> ResumePoint | None:
        """Read a resume point, or None when absent."""
        # Raises TorraError when the file exists but is corrupt: resuming
        # from garbage is worse than starting over, so this is loud.
        target = Path(path)
        try:
            if not target.exists():
                return None
        except OSError:
            return None  # a name too long for the filesystem: there is nothing to resume from
        try:
            point = cls(**json.loads(target.read_text(encoding="utf-8")))
            if not point._well_typed():
                raise TypeError("a field has the wrong type")
            return point
        except Exception as exc:
            raise TorraError(f"corrupt resume point {target}: {exc}") from exc

    def save(self, path: os.PathLike[str] | str) -> Path:
        """Write atomically: temp file then rename, so a crash cannot truncate."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.at = time.time()
        tmp = target.with_suffix(".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                fh.write(json.dumps(self.to_dict(), indent=2))
                fh.flush()
                os.fsync(fh.fileno())
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
    #: A step that was running when the earlier attempt stopped: its effect is unknown.
    interrupted: str = ""


def command_digest(command: str) -> str:
    """A short, stable fingerprint of a step's command."""
    return hashlib.sha256(command.encode("utf-8")).hexdigest()[:16]


def begin_step(resume: ResumePoint, path: os.PathLike[str] | str, step_id: str) -> None:
    """Record, durably, that a step is about to run: a crash then leaves it marked."""
    resume.in_progress = step_id
    _save_quietly(resume, path)


def end_step(
    resume: ResumePoint,
    path: os.PathLike[str] | str,
    step_id: str,
    command: str,
    ok: bool,
    dry_run: bool,
) -> None:
    """Record a step's end. Only a real, successful step counts as completed."""
    resume.in_progress = ""
    if ok and not dry_run:
        resume.completed.append(step_id)
        resume.digests[step_id] = command_digest(command)
    if not dry_run:
        _save_quietly(resume, path)


def _save_quietly(resume: ResumePoint, path: os.PathLike[str] | str) -> None:
    try:
        resume.save(path)
    except Exception:
        pass  # a resume point is a nicety, not a reason to stop


def interrupted_reason(step_id: str, path: os.PathLike[str] | str) -> str:
    """Why a run stops instead of re-running a step an earlier attempt left in flight."""
    return (
        f"step {step_id!r} was running when torra stopped, so its effect is unknown; "
        f"check what it did, then delete {path} to start the task over"
    )


def resolve_resume(
    task_id: str,
    plan_digests: Mapping[str, str],
    total: int,
    resume_path: os.PathLike[str] | str,
    dry_run: bool,
) -> ResumeResolution:
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
            fresh, frozenset(), f"on-disk resume point is corrupt, starting fresh: {exc}"
        )
    if existing is None:
        return ResumeResolution(fresh, frozenset())

    plan_step_ids = list(plan_digests)
    matches = (
        existing.task_id == task_id
        and existing.total == total
        and plan_step_ids[: len(existing.completed)] == existing.completed
        and all(existing.digests.get(s) == plan_digests[s] for s in existing.completed)
    )
    if not matches:
        return ResumeResolution(
            fresh,
            frozenset(),
            "an on-disk resume point exists for this task_id but its step "
            "sequence doesn't match the current plan -- starting fresh "
            "rather than trusting a possibly-unrelated record",
        )

    resumed = ResumePoint(
        task_id=task_id,
        completed=list(existing.completed),
        total=total,
        file_hashes=dict(existing.file_hashes),
        digests=dict(existing.digests),
    )
    interrupted = existing.in_progress if existing.in_progress in plan_digests else ""
    note = (
        f"{len(existing.completed)}/{total} step(s) already completed in an "
        "earlier attempt that didn't reach a clean finish; skipping them "
        "rather than re-running"
    )
    return ResumeResolution(resumed, frozenset(existing.completed), note, interrupted)
