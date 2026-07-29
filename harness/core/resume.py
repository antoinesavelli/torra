"""One resume point per task: a durable record of how far it got.

Depends on: harness.core.tasks (HarnessError).

`Harness.execute` writes one of these after each completed step and deletes
it on success -- but nothing in this codebase currently reads one back to
skip already-completed steps on a retry. `load()` exists and works; no
caller calls it. Today this is forensic state (inspect the file by hand
after a crash to see how far a task got), not automatic resumability. See
HANDOFF.md's "what is actually unfinished" section before assuming a
crashed multi-step task resumes on its own.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .tasks import HarnessError

__all__ = ["ResumePoint"]


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
        # Raises HarnessError when the file exists but is corrupt: resuming
        # from garbage is worse than starting over, so this is loud.
        target = Path(path)
        if not target.exists():
            return None
        try:
            return cls(**json.loads(target.read_text(encoding="utf-8")))
        except Exception as exc:
            raise HarnessError(f"corrupt resume point {target}: {exc}") from exc

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
            raise HarnessError(f"resume point write failed: {exc!r}") from exc
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
        return target
