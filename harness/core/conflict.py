"""Two-layer conflict detection: cheap fingerprints to shortlist, SHA256 to confirm.

Depends on: `inotify_simple` (optional, lazily imported, degrades to full-walk
scanning when absent).
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any

__all__ = ["ConflictKind", "Conflict", "ConflictDetector"]


class ConflictKind(str, Enum):
    """How a tracked path diverged from its baseline."""

    MODIFIED = "modified"
    DELETED = "deleted"
    CREATED = "created"
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class Conflict:
    """One detected divergence between baseline and current filesystem."""

    path: str
    kind: ConflictKind
    baseline_hash: str | None = None
    current_hash: str | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serializable dict form, for the audit trail."""
        d = asdict(self)
        d["kind"] = self.kind.value
        return d


class ConflictDetector:
    """Two-layer detection: cheap fingerprints to shortlist, SHA256 to confirm.

    Hashing every file on every check is correct and unusably slow on a real
    tree. Fingerprints alone miss a same-size same-mtime edit. So this does
    both, and uses inotify to narrow the candidate set to what the kernel says
    actually moved, with a periodic full walk to catch inotify's three silent
    failure modes: queue overflow, directories created after watches attached,
    and moves in from outside.
    """

    DEFAULT_IGNORES: tuple[str, ...] = (
        ".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache",
        ".pytest_cache", ".tox", "target", "dist", "build", ".cache",
    )

    def __init__(
        self,
        roots: Iterable[os.PathLike[str] | str] | None = None,
        ignores: Iterable[str] | None = None,
        max_files: int = 50_000,
        reconcile_every: int = 20,
        use_inotify: bool = True,
    ) -> None:
        """Configure watched roots, ignored directory names, and the walk/event policy."""
        self.roots: list[Path] = [Path(r) for r in (roots or [])]
        self.ignores: set[str] = set(ignores if ignores is not None
                                     else self.DEFAULT_IGNORES)
        self.max_files = int(max_files)
        self.reconcile_every = max(1, int(reconcile_every))
        self.use_inotify = bool(use_inotify)

        self.hashes: dict[str, str | None] = {}
        self.fingerprints: dict[str, tuple[int, int]] = {}
        self.errors: list[str] = []
        self.fast_checks: int = 0
        self.full_walks: int = 0
        self._since_walk: int = 0
        self._inotify: Any = None
        self._watch_paths: dict[int, str] = {}
        self._known: dict[str, Conflict] = {}

    # -- hashing ----------------------------------------------------------- #

    def hash_file(self, path: os.PathLike[str] | str) -> str | None:
        """SHA256 of a file, or None when absent, not a regular file, or unreadable."""
        p = Path(path)
        try:
            is_regular = stat.S_ISREG(p.stat().st_mode)
        except OSError:
            return None
        if not is_regular:
            # A FIFO opened for reading with no writer blocks forever --
            # found live, reachable pre-approval: a model-declared
            # `touches` path gets baselined during the GATE stage, before
            # any operator ever sees the plan, so a single stray FIFO (or
            # one placed adversarially) at a touched path wedges the whole
            # harness, with no timeout anywhere in the call chain. A
            # device or socket node has the same problem. p.stat() already
            # follows symlinks to their real target, so a symlink pointing
            # at a FIFO is caught the same way a direct FIFO path is --
            # only a symlink whose final target is a genuine regular file
            # still gets hashed.
            return None
        digest = hashlib.sha256()
        try:
            with p.open("rb") as fh:
                while chunk := fh.read(1 << 20):
                    digest.update(chunk)
        except OSError as exc:
            self.errors.append(f"hash failed for {p}: {exc!r}")
            return None
        return digest.hexdigest()

    def baseline(
        self,
        paths: Iterable[os.PathLike[str] | str] = (),
        roots: Iterable[os.PathLike[str] | str] | None = None,
    ) -> None:
        """Record hashes for declared paths and fingerprints for watched trees."""
        for path in paths:
            key = str(Path(path))
            self.hashes[key] = self.hash_file(key)
        if roots is not None:
            self.roots = [Path(r) for r in roots]
        self.fingerprints = {}
        for root in self.roots:
            self.fingerprints.update(self._walk(root))
        self._since_walk = 0
        self._known.clear()
        if self.use_inotify and self.roots:
            self._start_inotify()

    def _walk(self, root: Path) -> dict[str, tuple[int, int]]:
        """Fingerprint every file under root, skipping ignored directories."""
        # An explicit stack, not os.walk or recursion: os.walk follows
        # symlinks by default (a symlink loop would hang this), and
        # entry.is_dir(follow_symlinks=False) here is what keeps a loop from
        # being possible at all, since a symlink is never pushed as a
        # directory to descend into.
        out: dict[str, tuple[int, int]] = {}
        if not root.exists():
            return out
        count = 0
        stack = [root]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name not in self.ignores:
                                stack.append(Path(entry.path))
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        count += 1
                        if count > self.max_files:
                            note = (f"{root} exceeds max_files={self.max_files}; "
                                    "coverage is incomplete")
                            self.errors.append(note)
                            return out
                        try:
                            st = entry.stat(follow_symlinks=False)
                            out[entry.path] = (st.st_size, st.st_mtime_ns)
                        except OSError:
                            pass
            except OSError as exc:
                self.errors.append(f"{current}: {exc!r}")
        return out

    # -- inotify ----------------------------------------------------------- #

    def _start_inotify(self) -> None:
        """Attach watches. Degrades to walk-only and says so on failure."""
        try:
            from inotify_simple import INotify, flags  # type: ignore
        except Exception as exc:
            self.errors.append(f"inotify unavailable, walking instead: {exc!r}")
            self.use_inotify = False
            return
        try:
            self._inotify = INotify()
            mask = (flags.MODIFY | flags.CREATE | flags.DELETE
                    | flags.MOVED_FROM | flags.MOVED_TO | flags.ATTRIB)
            for root in self.roots:
                for dirpath, dirnames, _ in os.walk(root):
                    dirnames[:] = [d for d in dirnames if d not in self.ignores]
                    self._watch_paths[self._inotify.add_watch(dirpath, mask)] = dirpath
        except Exception as exc:
            self.errors.append(f"inotify setup failed, walking instead: {exc!r}")
            self._inotify = None
            self.use_inotify = False

    def _events(self) -> set[str] | None:
        """Drain the event queue into touched paths, or None when unused."""
        if not self._inotify:
            return None
        touched: set[str] = set()
        try:
            for event in self._inotify.read(timeout=0):
                base = self._watch_paths.get(event.wd)
                if base and event.name:
                    touched.add(os.path.join(base, event.name))
        except Exception as exc:
            self.errors.append(f"inotify read failed: {exc!r}")
            return None
        return touched

    # -- detection --------------------------------------------------------- #

    def detect(self) -> list[Conflict]:
        """Every divergence from baseline, declared paths and watched trees."""
        found: list[Conflict] = list(self._detect_declared())
        if self.roots:
            seen = {(c.path, c.kind) for c in found}
            found += [c for c in self._detect_tree() if (c.path, c.kind) not in seen]
        return found

    def _detect_declared(self) -> list[Conflict]:
        """Re-hash every explicitly declared path."""
        # An exhaustive case split over (had a baseline hash?, exists now?,
        # hashed successfully now?) -- the four branches below are the four
        # ConflictKind values, and each elif relies on the ones above it
        # having been false, so reordering them changes what gets reported
        # for the same state (e.g. a file both newly-created and unhashable
        # would need its own fifth branch to disambiguate, which the current
        # order doesn't need because CREATED already implies after is not None).
        out: list[Conflict] = []
        for path, before in self.hashes.items():
            p = Path(path)
            exists = p.exists()
            after = self.hash_file(path) if exists else None
            if before is None and after is not None:
                out.append(Conflict(path, ConflictKind.CREATED, None, after,
                                    "did not exist at baseline"))
            elif before is not None and not exists:
                out.append(Conflict(path, ConflictKind.DELETED, before, None,
                                    "removed since baseline"))
            elif before is not None and after is None and exists:
                out.append(Conflict(path, ConflictKind.UNREADABLE, before, None,
                                    "exists but could not be hashed"))
            elif before != after:
                out.append(Conflict(path, ConflictKind.MODIFIED, before, after,
                                    "content hash changed"))
        return out

    def _detect_tree(self) -> list[Conflict]:
        """Event fast path, with a full walk on schedule."""
        if not self.use_inotify or not self._inotify \
                or self._since_walk >= self.reconcile_every:
            return self._reconcile()

        touched = self._events() or set()
        self.fast_checks += 1
        self._since_walk += 1
        for path in touched:
            # Found live: a path that reverts to matching its baseline again
            # (edited then edited back, an editor's atomic-save round-trip
            # landing on identical bytes) still had a real inotify event, so
            # it reaches _classify -- but _classify correctly returns None
            # for "no longer different," and a bare `if conflict:` here
            # only ever adds, never removes, from `_known`. A conflict
            # recorded on an earlier fast check stuck around across every
            # later detect() call -- feeding Harness._run_steps() a false
            # "external conflict" and blocking a run for nothing -- until
            # the next scheduled full walk happened to rebuild `_known`
            # from scratch.
            conflict = self._classify(path)
            if conflict:
                self._known[conflict.path] = conflict
            else:
                self._known.pop(path, None)
        return list(self._known.values())

    def _classify(self, path: str) -> Conflict | None:
        """Turn one event path into a Conflict, or None when it is not one."""
        target = Path(path)
        known = path in self.fingerprints
        exists = target.exists()
        if not known and exists:
            return Conflict(path, ConflictKind.CREATED, None, self.hash_file(path),
                            "inotify reported a new file")
        if known and not exists:
            return Conflict(path, ConflictKind.DELETED, self.hashes.get(path), None,
                            "inotify reported a deletion")
        if not known:
            return None
        try:
            st = target.stat()
        except OSError as exc:
            return Conflict(path, ConflictKind.UNREADABLE, None, None, repr(exc))
        if self.fingerprints[path] == (st.st_size, st.st_mtime_ns):
            return None
        before = self.hashes.get(path, "<not hashed at baseline>")
        after = self.hash_file(path)
        if before == after:
            return None
        return Conflict(path, ConflictKind.MODIFIED, before, after,
                        "event plus hash confirm the change")

    def _reconcile(self) -> list[Conflict]:
        """Full walk. The source of truth the event path is checked against."""
        self.full_walks += 1
        self._since_walk = 0
        conflicts: list[Conflict] = []
        current: dict[str, tuple[int, int]] = {}
        for root in self.roots:
            current.update(self._walk(root))

        for path in current.keys() - self.fingerprints.keys():
            conflicts.append(Conflict(path, ConflictKind.CREATED, None,
                                      self.hash_file(path), "appeared in the tree"))
        for path in self.fingerprints.keys() - current.keys():
            conflicts.append(Conflict(path, ConflictKind.DELETED,
                                      self.hashes.get(path), None,
                                      "removed from the tree"))
        for path in self.fingerprints.keys() & current.keys():
            if self.fingerprints[path] == current[path]:
                continue
            before = self.hashes.get(path, "<not hashed at baseline>")
            after = self.hash_file(path)
            if before != after:
                conflicts.append(Conflict(path, ConflictKind.MODIFIED, before, after,
                                          "size or mtime moved and the hash confirms"))
        self._known = {c.path: c for c in conflicts}
        return conflicts

    def refresh(self, paths: Iterable[os.PathLike[str] | str] = ()) -> None:
        """Re-baseline after the harness itself changed something."""
        for path in paths:
            key = str(Path(path))
            self.hashes[key] = self.hash_file(key)
        self.fingerprints = {}
        for root in self.roots:
            self.fingerprints.update(self._walk(root))
        self._known.clear()
        self._since_walk = 0

    def close(self) -> None:
        """Release inotify file descriptors."""
        if self._inotify:
            try:
                self._inotify.close()
            except Exception:
                pass
            self._inotify = None

    def stats(self) -> dict[str, Any]:
        """Event versus walk counters, for the audit trail."""
        total = self.fast_checks + self.full_walks
        return {
            "fast_checks": self.fast_checks,
            "full_walks": self.full_walks,
            "fast_path_ratio": round(self.fast_checks / total, 4) if total else 0.0,
            "tracked_paths": len(self.hashes),
            "watched_files": len(self.fingerprints),
            "inotify": self.use_inotify,
            "errors": list(self.errors),
        }
