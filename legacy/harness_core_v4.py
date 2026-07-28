"""
harness_core_v4.py
Phase 4 core.

  P1-3  EventDrivenConflictDetector   trusts inotify between periodic full
                                      reconciles, turning O(tree) per check
                                      into O(changed)

Phase 2 attached inotify watches but still ran a full directory walk as the
source of truth on every check, so the measured cost was 5.7 ms on 1,200 files
and would be roughly 240 ms on 50,000. That was the last scaling wall.

Phases 1 through 3 are unmodified.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from harness_core import Conflict, ConflictKind
from harness_core_v2 import FilesystemConflictDetector

__all__ = ["ReconcileStats", "EventDrivenConflictDetector"]


@dataclass
class ReconcileStats:
    """How often the detector trusted events versus walking the tree."""

    fast_checks: int = 0
    full_reconciles: int = 0
    events_seen: int = 0
    reconcile_disagreements: int = 0
    fallbacks: List[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        """Default the fallback list without sharing it between instances."""
        if self.fallbacks is None:
            self.fallbacks = []

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form, for the audit trail."""
        return asdict(self)


class EventDrivenConflictDetector(FilesystemConflictDetector):
    """Conflict detection that reads events instead of walking the tree.

    The Phase 2 detector used inotify only to narrow a candidate set, then
    walked everything anyway. That made the watch cost O(tree) per check no
    matter how little changed, which is backwards: the whole reason to attach
    inotify is that the kernel already knows what moved.

    This trusts the event stream between full reconciles. A check drains the
    inotify queue, hashes only the paths the kernel named, and reports. Every
    `reconcile_every` checks, or whenever the queue overflows, it falls back to
    a full walk and compares the two answers.

    That reconcile is not ceremony. inotify has three real failure modes and all
    of them are silent:

      * the queue overflows under heavy churn and events are dropped
      * a directory created after the watches were attached is unwatched, so
        changes inside it are invisible
      * a move into the tree from outside can arrive without a matching event

    So the design is: fast path by default, full walk on a schedule, and any
    disagreement between them is counted and reported rather than smoothed over.
    A detector that quietly drifts from the filesystem is worse than a slow one.
    """

    DEFAULT_RECONCILE_EVERY: int = 20

    def __init__(
        self,
        roots: Optional[Iterable[os.PathLike[str] | str]] = None,
        ignores: Optional[Iterable[str]] = None,
        max_files: int = 50_000,
        clock: Callable[[], float] = time.time,
        reconcile_every: int = DEFAULT_RECONCILE_EVERY,
        reconcile_seconds: float = 60.0,
    ) -> None:
        """
        Args:
            reconcile_every: Full walk after this many fast checks.
            reconcile_seconds: Full walk after this many seconds, whichever
                comes first.
            (Other arguments as FilesystemConflictDetector.)
        """
        super().__init__(roots, ignores, max_files, clock, use_inotify=True)
        self.reconcile_every = max(1, int(reconcile_every))
        self.reconcile_seconds = float(reconcile_seconds)
        self.stats = ReconcileStats()
        self._checks_since_reconcile: int = 0
        self._last_reconcile: float = 0.0
        self._known_conflicts: Dict[str, Conflict] = {}

    # -- setup -------------------------------------------------------------- #

    def baseline_tree(
        self, roots: Optional[Iterable[os.PathLike[str] | str]] = None
    ) -> Dict[str, Any]:
        """Snapshot the tree and start the event clock."""
        snapshots = super().baseline_tree(roots)
        self._checks_since_reconcile = 0
        self._last_reconcile = self._clock()
        self._known_conflicts.clear()
        if not self.use_inotify:
            self.stats.fallbacks.append(
                "inotify unavailable at baseline, every check is a full walk")
        return snapshots

    def _reconcile_due(self) -> bool:
        """True when a full walk is owed."""
        if not self.use_inotify or not self._inotify:
            return True
        if self._checks_since_reconcile >= self.reconcile_every:
            return True
        return (self._clock() - self._last_reconcile) >= self.reconcile_seconds

    # -- fast path ---------------------------------------------------------- #

    def _classify_event_path(self, path: str) -> Optional[Conflict]:
        """Turn one event path into a Conflict, or None when it is not one."""
        target = Path(path)
        snapshot = next(
            (s for s in self.snapshots.values() if path.startswith(s.root)), None)
        if snapshot is None:
            return None

        known = path in snapshot.entries
        exists = target.exists()

        if not known and exists:
            return Conflict(path, ConflictKind.CREATED, None, self.hash_file(path),
                            "inotify reported a new file in the watched tree")
        if known and not exists:
            return Conflict(path, ConflictKind.DELETED, self.baseline.get(path),
                            None, "inotify reported a deletion")
        if not known and not exists:
            return None

        try:
            st = target.stat()
        except OSError as exc:
            return Conflict(path, ConflictKind.UNREADABLE, self.baseline.get(path),
                            None, f"stat failed after an event: {exc!r}")

        if snapshot.entries[path] == (st.st_size, st.st_mtime_ns):
            return None  # event fired but nothing actually moved

        before = self.baseline.get(path, "<not hashed at baseline>")
        after = self.hash_file(path)
        if before == after:
            return None
        return Conflict(path, ConflictKind.MODIFIED, before, after,
                        "inotify reported a write and the content hash confirms it")

    def detect_tree(self) -> List[Conflict]:
        """Check the watched tree, using events when it can and walking when it must.

        Returns:
            Conflicts found. A full reconcile also records any disagreement with
            what the fast path had been reporting.
        """
        started = time.perf_counter()

        if self._reconcile_due():
            walked = super().detect_tree()
            self.stats.full_reconciles += 1
            self._checks_since_reconcile = 0
            self._last_reconcile = self._clock()

            walked_keys = {(c.path, c.kind) for c in walked}
            fast_keys = {(c.path, c.kind) for c in self._known_conflicts.values()}
            missed = fast_keys ^ walked_keys
            if missed and self.stats.fast_checks:
                self.stats.reconcile_disagreements += len(missed)
                self.stats.fallbacks.append(
                    f"reconcile found {len(missed)} discrepancy(ies) against the "
                    "event stream; events were dropped or a path was unwatched"
                )
            self._known_conflicts = {c.path: c for c in walked}
            self.last_check_seconds = time.perf_counter() - started
            return walked

        touched = self._inotify_candidates() or set()
        self.stats.events_seen += len(touched)
        self.stats.fast_checks += 1
        self._checks_since_reconcile += 1

        for path in touched:
            conflict = self._classify_event_path(path)
            if conflict:
                self._known_conflicts[conflict.path] = conflict

        self.last_check_seconds = time.perf_counter() - started
        return list(self._known_conflicts.values())

    def refresh_tree(self) -> None:
        """Re-baseline and clear the event-derived conflict set."""
        super().refresh_tree()
        self._known_conflicts.clear()
        self._checks_since_reconcile = 0
        self._last_reconcile = self._clock()

    def force_reconcile(self) -> List[Conflict]:
        """Walk the tree now, regardless of the schedule."""
        self._checks_since_reconcile = self.reconcile_every
        return self.detect_tree()

    def stats_dict(self) -> Dict[str, Any]:
        """Event versus walk counters, for the audit trail."""
        out = self.stats.to_dict()
        total = self.stats.fast_checks + self.stats.full_reconciles
        out["fast_path_ratio"] = (
            round(self.stats.fast_checks / total, 4) if total else 0.0)
        out["last_check_ms"] = round(self.last_check_seconds * 1000, 4)
        return out
