"""ROLLBACK_ON_FAIL, the shadow directory: claiming it, reaping dead runs' leftovers, hashing a file.

Depends on: torra.gates.undo_types (Snapshot). `torra.gates.undo.UndoGenerator` inherits `ShadowStore`.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Callable
from pathlib import Path

from .undo_types import Snapshot

__all__ = ["ShadowStore"]


class ShadowStore:
    """The shadow directory a generator snapshots into, and the state its captures share."""

    #: A sibling torra-shadow-<pid>* directory is only reaped once it is
    #: BOTH this old AND its owning pid is dead -- an in-progress run, even
    #: a long one, must never have its snapshots pulled out from under it.
    STALE_SHADOW_SECONDS: float = 24 * 3600
    #: A directory with no owner record (from before `owner.pid` existed) waits this long.
    LEGACY_SHADOW_SECONDS: float = 7 * 24 * 3600

    shadow_dir: Path
    snapshots: list[Snapshot]
    skipped: list[str]
    _counter: int
    _executor_runner: Callable[[str, str | None, float | None], str] | None

    def _claim_shadow_dir(self) -> None:
        """Record the owning pid beside the directory (`<name>.owner`; cleanup() leaves it empty)."""
        try:
            self._owner_file(self.shadow_dir).write_text(str(os.getpid()), encoding="utf-8")
        except OSError:
            pass

    @staticmethod
    def _owner_file(entry: Path) -> Path:
        return entry.with_name(entry.name + ".owner")

    @staticmethod
    def _shadow_owner(entry: Path) -> int | None:
        """The pid that owns a shadow directory: its owner file, else a pid in its name."""
        import re
        try:
            return int(ShadowStore._owner_file(entry).read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pass
        match = re.match(r"torra-shadow-(\d+)(?:-|$)", entry.name)
        return int(match.group(1)) if match else None

    # -- reaping ------------------------------------------------------------ #

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        """True when a process with this pid is still running."""
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, OverflowError, ValueError):
            return False
        except PermissionError:
            return True  # exists, just not ours to signal
        except OSError:
            return False
        return True

    def _reap_stale_shadows(self) -> None:
        """Remove sibling shadow directories left by dead, old runs.

        Nothing in this codebase called `cleanup()` after a completed run --
        `Torra.close()`'s own docstring says shadow copies are "kept for
        post-mortem", but that only makes sense for a *failed* run whose
        rollback someone might want to inspect. `Torra.execute()` now
        calls `cleanup()` directly on success, so this reaper only ever
        has failed-run leftovers to catch, plus anything from before this
        fix existed (14 such directories were already sitting on one real
        machine when this was found in a full-file audit). A directory is
        reaped only once it is both stale by age AND its owning pid is
        dead, so a run genuinely still in progress is never touched.
        """
        import time

        parent = self.shadow_dir.parent
        try:
            entries = list(parent.iterdir()) if parent.is_dir() else []
        except OSError:
            return
        now = time.time()
        for entry in entries:
            if entry == self.shadow_dir or not entry.is_dir():
                continue
            if not entry.name.startswith("torra-shadow-"):
                continue
            owner = self._shadow_owner(entry)
            try:
                age = now - entry.stat().st_mtime
            except OSError:
                continue
            if owner is None:
                stale = age >= self.LEGACY_SHADOW_SECONDS
            else:
                stale = age >= self.STALE_SHADOW_SECONDS and not self._pid_alive(owner)
            if stale:
                shutil.rmtree(entry, ignore_errors=True)
                self._owner_file(entry).unlink(missing_ok=True)

    @staticmethod
    def digest(path: Path) -> str | None:
        """SHA256 of a file, or None when absent or unreadable."""
        if not path.exists():
            return None
        h = hashlib.sha256()
        try:
            with path.open("rb") as fh:
                while chunk := fh.read(1 << 20):
                    h.update(chunk)
        except OSError:
            return None
        return h.hexdigest()

