"""
core_behaviors_v5.py
Phase 5 behaviors. One small, safe fix.

  StrictShadowReaper   the shadow directory prefix match now requires the pid
                       suffix the harness actually writes, instead of a plain
                       startswith that could take a directory by accident of
                       naming

Phase 4's probe C found that `harness-shadowy-business` matched
`"harness-shadow-".startswith` semantics closely enough to be uncomfortable: a
directory is spared or reaped by accident of naming rather than by design. Low
severity, trivial to fix, and exactly the kind of thing that turns into a
support ticket at 2am.

Everything else in Phase 5 is telemetry, in harness_telemetry.py. Phases 1
through 4 are unmodified.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from core_behaviors_v4 import ReapReport, ShadowReaper

__all__ = ["StrictShadowReaper"]


class StrictShadowReaper(ShadowReaper):
    """A reaper that only removes directories it can prove the harness made.

    The Phase 4 reaper matched `name.startswith("harness-shadow-")`. That is
    permissive in the wrong direction: `harness-shadow-backup-of-everything` is
    a plausible thing for a person to name a directory, and it matches.

    This requires the full shape the harness actually writes,
    `harness-shadow-<pid>`, where the suffix is digits. A directory that merely
    starts with the prefix is left alone and, more usefully, is *reported* as a
    near miss, so an operator who did name something that way finds out rather
    than wondering why their directory keeps not being cleaned up.
    """

    #: A shadow directory the harness itself created.
    NAME_PATTERN = re.compile(r"^harness-shadow-\d+$")

    def __init__(
        self,
        roots: Sequence[os.PathLike[str] | str],
        prefix: str = ShadowReaper.DEFAULT_PREFIX,
        max_age_seconds: float = ShadowReaper.DEFAULT_MAX_AGE,
        min_age_seconds: float = ShadowReaper.DEFAULT_MIN_AGE,
        clock: Callable[[], float] = time.time,
        pattern: Optional[re.Pattern[str]] = None,
    ) -> None:
        """
        Args:
            pattern: Override the strict name pattern. Must be a compiled regex
                matched against the directory name in full.
            (Other arguments as ShadowReaper.)
        """
        super().__init__(roots, prefix, max_age_seconds, min_age_seconds, clock)
        self.pattern = pattern or self.NAME_PATTERN
        self.near_misses: List[str] = []

    def _matches(self, name: str) -> bool:
        """True when a directory name is one the harness would have created.

        Records a near miss when a name starts with the prefix but does not fit
        the pattern, because silently ignoring it is how an operator ends up
        with a directory that never gets cleaned and never gets explained.
        """
        if self.pattern.match(name):
            return True
        if name.startswith(self.prefix):
            note = (f"{name} starts with '{self.prefix}' but does not match "
                    f"{self.pattern.pattern}; left alone deliberately")
            if note not in self.near_misses:
                self.near_misses.append(note)
        return False

    def candidates(self) -> List[Path]:
        """Directories eligible for removal, using the strict name test."""
        now = self._clock()
        out: List[Path] = []
        for root in self.roots:
            if not root.is_dir():
                continue
            try:
                entries = list(root.iterdir())
            except OSError:
                continue
            for entry in entries:
                if not entry.is_dir() or not self._matches(entry.name):
                    continue
                try:
                    age = now - entry.stat().st_mtime
                except OSError:
                    continue
                if self.min_age_seconds <= age and age >= self.max_age_seconds:
                    out.append(entry)
        return out

    def reap(self, dry_run: bool = False) -> "ReapReport":  # type: ignore[override]
        """Remove aged out shadow directories, matching strictly.

        Args:
            dry_run: Report what would be removed without removing it.

        Returns:
            A ReapReport. Near misses are available on `self.near_misses`.
        """
        import shutil

        report = ReapReport()
        now = self._clock()

        for root in self.roots:
            if not root.is_dir():
                continue
            try:
                entries = list(root.iterdir())
            except OSError as exc:
                report.errors.append(f"{root}: {exc!r}")
                continue

            for entry in entries:
                if not entry.is_dir() or not self._matches(entry.name):
                    continue
                report.scanned += 1
                try:
                    age = now - entry.stat().st_mtime
                except OSError as exc:
                    report.errors.append(f"{entry}: {exc!r}")
                    continue

                if age < self.min_age_seconds:
                    report.kept_live += 1
                    continue
                if age < self.max_age_seconds:
                    continue

                size = self._size(entry)
                if dry_run:
                    report.removed += 1
                    report.bytes_freed += size
                    continue
                try:
                    shutil.rmtree(entry)
                    report.removed += 1
                    report.bytes_freed += size
                except OSError as exc:
                    report.errors.append(f"{entry}: rmtree failed {exc!r}")

        for note in self.near_misses:
            report.errors.append(f"near miss: {note}")
        return report
