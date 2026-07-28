"""ROLLBACK_ON_FAIL: derives undos from step state, then proves each one worked.

Depends on: stdlib only.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

__all__ = [
    "SnapshotKind", "Snapshot", "UndoAction", "UndoGenerator",
    "RollbackResult", "RollbackReport", "VerifiedRollback",
]


class SnapshotKind(str, Enum):
    """What kind of prior state a snapshot captured."""

    FILE_CONTENT = "file_content"
    FILE_ABSENT = "file_absent"
    DIR_ABSENT = "dir_absent"
    SERVICE_STATE = "service_state"
    PACKAGE_SET = "package_set"


@dataclass
class Snapshot:
    """Prior state captured before a step ran."""

    kind: SnapshotKind
    target: str
    detail: str = ""
    shadow_path: str | None = None
    digest: str | None = None
    payload: Any | None = None
    mode: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serializable dict form."""
        d = asdict(self)
        d["kind"] = self.kind.value
        if isinstance(d.get("payload"), (set, frozenset)):
            d["payload"] = sorted(d["payload"])
        return d


@dataclass
class UndoAction:
    """An undo that can prove it worked.

    `verify` returns True when the system is genuinely back to its pre-step
    state. Without it, "undone" only means the callable did not raise, which is
    a much weaker claim than it reads as.
    """

    description: str
    undo: Callable[[], Any]
    safe: bool = True
    verify: Callable[[], bool] | None = None
    generated: bool = False
    snapshot: Snapshot | None = None
    step_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serializable dict form, excluding callables."""
        return {
            "description": self.description,
            "safe": self.safe,
            "generated": self.generated,
            "step_id": self.step_id,
            "has_verifier": self.verify is not None,
            "snapshot": self.snapshot.to_dict() if self.snapshot else None,
        }


@dataclass
class RollbackResult:
    """Outcome of undoing one action."""

    description: str
    status: str  # undone | unverified | cancelled | failed
    error: str = ""
    order: int = 0
    verified: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serializable dict form."""
        return asdict(self)


@dataclass
class RollbackReport:
    """Aggregate of one rollback sweep."""

    ok: bool
    results: list[RollbackResult] = field(default_factory=list)

    @property
    def undone(self) -> list[RollbackResult]:
        """Actions whose undo ran and verified."""
        return [r for r in self.results if r.status == "undone"]

    @property
    def unverified(self) -> list[RollbackResult]:
        """Actions whose undo ran but could not be proven."""
        return [r for r in self.results if r.status == "unverified"]

    @property
    def cancelled(self) -> list[RollbackResult]:
        """Unsafe undos the operator declined."""
        return [r for r in self.results if r.status == "cancelled"]

    @property
    def failed(self) -> list[RollbackResult]:
        """Undos that raised."""
        return [r for r in self.results if r.status == "failed"]

    def to_dict(self) -> dict[str, Any]:
        """Serializable dict form."""
        return {"ok": self.ok, "results": [r.to_dict() for r in self.results]}


SERVICE_PATTERN = re.compile(
    r"\bsystemctl\s+(?:--user\s+)?(start|stop|restart|enable|disable|mask|unmask)"
    r"\s+([\w@.\-]+)", re.IGNORECASE)
PACKAGE_PATTERN = re.compile(
    r"\b(?:apt|apt-get|dnf|pacman)\s+(install|remove|purge)\s+(.+)", re.IGNORECASE)
MKDIR_PATTERN = re.compile(r"\bmkdir\b(?:\s+-\w+)*\s+([^\s;|&]+)")


class UndoGenerator:
    """Derives undo actions from what a step declares it will do.

    Without this, ROLLBACK_ON_FAIL is a promise the harness cannot keep for any
    step whose author forgot to register an inverse. With an agent writing the
    steps, "whose author forgot" becomes the default case.

    Covers: files in `touches`, `systemctl` state changes, `apt install`, and
    `mkdir`. Deliberately generates nothing for `dd`, `mkfs`, or database
    migrations, and names those steps as uncovered rather than inventing an
    inverse it cannot guarantee.
    """

    MAX_SHADOW_BYTES: int = 64 * 1024 * 1024

    def __init__(
        self,
        shadow_dir: os.PathLike[str] | str | None = None,
        service_probe: Callable[[str], str] | None = None,
        package_probe: Callable[[], set[str]] | None = None,
        max_shadow_bytes: int | None = None,
        prefer_reflink: bool = True,
    ) -> None:
        """Configure the shadow directory, injectable probes, and size/reflink policy."""
        import tempfile

        self.shadow_dir = Path(shadow_dir) if shadow_dir else Path(
            tempfile.mkdtemp(prefix=f"harness-shadow-{os.getpid()}-"))
        self.shadow_dir.mkdir(parents=True, exist_ok=True)
        self._service_probe = service_probe or self._systemctl_state
        self._package_probe = package_probe or self._dpkg_set
        self.max_shadow_bytes = (self.MAX_SHADOW_BYTES if max_shadow_bytes is None
                                 else int(max_shadow_bytes))
        self.prefer_reflink = bool(prefer_reflink)
        self.reflink_supported: bool | None = None
        self.reflink_copies: int = 0
        self.byte_copies: int = 0
        self.snapshots: list[Snapshot] = []
        self.skipped: list[str] = []
        self._counter: int = 0

    # -- probes ------------------------------------------------------------ #

    @staticmethod
    def _systemctl_state(unit: str) -> str:
        """Active state of a unit, or 'unknown' on error."""
        try:
            out = subprocess.run(["systemctl", "--user", "is-active", unit],
                                 capture_output=True, text=True, timeout=5)
            return out.stdout.strip() or "unknown"
        except Exception:
            return "unknown"

    @staticmethod
    def _dpkg_set() -> set[str]:
        """Installed package set, or empty on error."""
        try:
            out = subprocess.run(["dpkg-query", "-f", "${Package}\n", "-W"],
                                 capture_output=True, text=True, timeout=20)
            return {line.strip() for line in out.stdout.splitlines() if line.strip()}
        except Exception:
            return set()

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

    # -- copying ----------------------------------------------------------- #

    def _probe_reflink(self) -> bool:
        """Test once whether this filesystem clones instead of copying."""
        if self.reflink_supported is not None:
            return self.reflink_supported
        if not self.prefer_reflink or not shutil.which("cp"):
            self.reflink_supported = False
            return False
        probe, clone = self.shadow_dir / ".rl", self.shadow_dir / ".rl-clone"
        try:
            probe.write_bytes(b"probe")
            result = subprocess.run(["cp", "--reflink=always", str(probe), str(clone)],
                                    capture_output=True, text=True, timeout=10)
            self.reflink_supported = result.returncode == 0
        except Exception:
            self.reflink_supported = False
        finally:
            for p in (probe, clone):
                try:
                    p.unlink()
                except OSError:
                    pass
        return bool(self.reflink_supported)

    def _copy(self, source: Path, destination: Path) -> str:
        """Clone or copy one file. Returns the mode actually used."""
        if self._probe_reflink():
            try:
                result = subprocess.run(
                    ["cp", "--reflink=always", "--preserve=all",
                     str(source), str(destination)],
                    capture_output=True, text=True, timeout=60)
                if result.returncode == 0:
                    self.reflink_copies += 1
                    return "reflink"
            except Exception as exc:
                self.skipped.append(f"reflink copy of {source} failed: {exc!r}")
        shutil.copy2(source, destination)
        self.byte_copies += 1
        return "copy"

    # -- capture ----------------------------------------------------------- #

    def capture_file(self, path: os.PathLike[str] | str,
                     step_id: str = "") -> UndoAction | None:
        """Snapshot one file and build its restore action."""
        target = Path(path)

        if not target.exists():
            snap = Snapshot(SnapshotKind.FILE_ABSENT, str(target),
                            "did not exist before the step")
            self.snapshots.append(snap)

            def undo_absent() -> None:
                """Delete whatever the step created."""
                if target.exists():
                    target.unlink()

            return UndoAction(f"delete {target} (it did not exist before)",
                              undo_absent, True, lambda: not target.exists(),
                              True, snap, step_id)

        try:
            stat = target.stat()
        except OSError as exc:
            self.skipped.append(f"{target}: stat failed {exc!r}")
            return None
        size, mode = stat.st_size, stat.st_mode
        if size > self.max_shadow_bytes:
            self.skipped.append(
                f"{target} is {size} bytes, over max_shadow_bytes="
                f"{self.max_shadow_bytes}; no undo generated, declare one manually")
            return None

        self._counter += 1
        shadow = self.shadow_dir / f"{self._counter:05d}_{target.name}"
        try:
            how = self._copy(target, shadow)
        except OSError as exc:
            self.skipped.append(f"{target}: shadow copy failed {exc!r}")
            return None

        dig = self.digest(target)
        snap = Snapshot(SnapshotKind.FILE_CONTENT, str(target),
                        f"{size} bytes via {how}", str(shadow), dig, mode=mode)
        self.snapshots.append(snap)

        def undo_restore() -> None:
            """Copy the shadowed bytes back and restore the original mode."""
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(shadow, target)
            os.chmod(target, mode & 0o7777)

        return UndoAction(f"restore {target} from snapshot ({how})", undo_restore,
                          True, lambda: self.digest(target) == dig, True, snap, step_id)

    def capture_service(self, unit: str, step_id: str = "") -> UndoAction:
        """Snapshot a unit's state and build its restore action."""
        before = self._service_probe(unit)
        snap = Snapshot(SnapshotKind.SERVICE_STATE, unit, f"was {before}", payload=before)
        self.snapshots.append(snap)

        def undo_service() -> None:
            """Put the unit back into whichever state it was in."""
            subprocess.run(
                ["systemctl", "--user", "start" if before == "active" else "stop", unit],
                capture_output=True, text=True, timeout=30, check=False)

        return UndoAction(f"restore {unit} to {before}", undo_service, True,
                          lambda: self._service_probe(unit) == before, True, snap, step_id)

    def capture_packages(self, requested: str, step_id: str = "") -> UndoAction:
        """Snapshot the package set. Marked unsafe: removals cascade."""
        before = self._package_probe()
        snap = Snapshot(SnapshotKind.PACKAGE_SET, requested,
                        f"{len(before)} packages installed before", payload=sorted(before))
        self.snapshots.append(snap)

        def undo_packages() -> None:
            """Remove exactly the packages that appeared since the snapshot."""
            added = sorted(self._package_probe() - before)
            if added:
                subprocess.run(["apt-get", "remove", "--yes", *added],
                               capture_output=True, text=True, timeout=300, check=False)

        return UndoAction(f"remove packages added by '{requested}'", undo_packages,
                          False, lambda: self._package_probe() <= before,
                          True, snap, step_id)

    def capture_mkdir(self, path: str, step_id: str = "") -> UndoAction | None:
        """Build an undo that removes a directory the step will create."""
        target = Path(path)
        if target.exists():
            return None
        snap = Snapshot(SnapshotKind.DIR_ABSENT, str(target), "did not exist before")
        self.snapshots.append(snap)

        def undo_mkdir() -> None:
            """Remove the directory, but only while it is still empty."""
            if target.is_dir() and not any(target.iterdir()):
                target.rmdir()

        return UndoAction(f"remove {target} if still empty", undo_mkdir, True,
                          lambda: not target.exists() or any(target.iterdir()),
                          True, snap, step_id)

    def for_step(self, step: Any) -> list[UndoAction]:
        """Every undo derivable from one step, in push order."""
        actions: list[UndoAction] = []
        step_id = getattr(step, "step_id", "")
        for path in getattr(step, "touches", []) or []:
            action = self.capture_file(path, step_id)
            if action:
                actions.append(action)

        command = getattr(step, "command", "") or ""
        for _verb, unit in SERVICE_PATTERN.findall(command):
            actions.append(self.capture_service(unit, step_id))
        for verb, packages in PACKAGE_PATTERN.findall(command):
            if verb.lower() == "install":
                actions.append(self.capture_packages(packages.strip(), step_id))
            else:
                self.skipped.append(
                    f"'{verb} {packages.strip()}' removes packages and cannot be "
                    "auto-inverted; declare a rollback entry")
        for path in MKDIR_PATTERN.findall(command):
            action = self.capture_mkdir(path, step_id)
            if action:
                actions.append(action)
        return actions

    def coverage(self, steps: Sequence[Any]) -> dict[str, Any]:
        """Which steps got a derived undo and which did not."""
        # Not an error when a step has none -- the operator should see it
        # before the run rather than after the failure.
        covered: list[str] = []
        uncovered: list[str] = []
        for step in steps:
            command = getattr(step, "command", "") or ""
            derivable = (bool(getattr(step, "touches", None))
                         or SERVICE_PATTERN.search(command)
                         or PACKAGE_PATTERN.search(command)
                         or MKDIR_PATTERN.search(command))
            (covered if derivable else uncovered).append(getattr(step, "step_id", "?"))
        return {
            "covered": covered,
            "uncovered": uncovered,
            "coverage_ratio": round(len(covered) / len(steps), 4) if steps else 0.0,
            "skipped": list(self.skipped),
        }

    def stats(self) -> dict[str, Any]:
        """Copy mode counters, for the audit trail."""
        return {
            "reflink_supported": self.reflink_supported,
            "reflink_copies": self.reflink_copies,
            "byte_copies": self.byte_copies,
            "snapshots": len(self.snapshots),
            "shadow_dir": str(self.shadow_dir),
            "skipped": list(self.skipped),
        }

    def cleanup(self) -> None:
        """Delete the shadow directory. Only after a successful run."""
        shutil.rmtree(self.shadow_dir, ignore_errors=True)


class VerifiedRollback:
    """A LIFO undo stack where every undo has to prove it worked.

    Three terminal states, not two. `undone` means the undo ran and
    verification confirmed the prior state. `unverified` means it ran without
    raising but verification says no. `failed` means it raised. An action with
    no verifier reports `verified=None`, which is honest about the fact that
    nothing was checked.
    """

    def __init__(
        self,
        confirm_unsafe: Callable[[UndoAction], bool] | None = None,
        stop_on_cancel: bool = True,
        stop_on_unverified: bool = True,
    ) -> None:
        """Configure the unsafe-undo confirmation callback and halt policies."""
        self._stack: list[UndoAction] = []
        self._confirm = confirm_unsafe or (lambda action: False)
        self.stop_on_cancel = bool(stop_on_cancel)
        self.stop_on_unverified = bool(stop_on_unverified)
        self.generated_count: int = 0

    @property
    def depth(self) -> int:
        """Number of pending undo actions."""
        return len(self._stack)

    def peek(self) -> UndoAction | None:
        """Top of the stack without popping."""
        return self._stack[-1] if self._stack else None

    def actions(self) -> list[UndoAction]:
        """Everything currently on the stack, bottom first."""
        return list(self._stack)

    def push(self, action: UndoAction) -> UndoAction:
        """Push a pre-built action."""
        self._stack.append(action)
        if action.generated:
            self.generated_count += 1
        return action

    def push_many(self, actions: Sequence[UndoAction]) -> int:
        """Push several actions, returning how many were added."""
        for action in actions:
            self.push(action)
        return len(actions)

    def add(self, description: str, undo: Callable[[], Any], safe: bool = True,
            verify: Callable[[], bool] | None = None,
            step_id: str = "") -> UndoAction:
        """Push a caller-supplied undo, optionally with a verifier."""
        return self.push(UndoAction(description, undo, safe, verify,
                                    False, None, step_id))

    def clear(self) -> None:
        """Drop the stack. Call on success, once changes are committed."""
        self._stack.clear()

    def rollback(self) -> RollbackReport:
        """Unwind the stack newest first, verifying each undo."""
        results: list[RollbackResult] = []
        order = 0

        while self._stack:
            action = self._stack.pop()
            order += 1

            if not action.safe:
                try:
                    approved = bool(self._confirm(action))
                except Exception as exc:
                    approved = False
                    results.append(RollbackResult(
                        action.description, "cancelled",
                        f"confirm raised {type(exc).__name__}: {exc}", order))
                    if self.stop_on_cancel:
                        self._stack.append(action)
                        break
                    continue
                if not approved:
                    results.append(RollbackResult(
                        action.description, "cancelled",
                        "operator declined an unsafe undo", order))
                    if self.stop_on_cancel:
                        self._stack.append(action)
                        break
                    continue

            try:
                action.undo()
            except Exception as exc:
                results.append(RollbackResult(action.description, "failed",
                                              f"{type(exc).__name__}: {exc}", order))
                continue

            if action.verify is None:
                results.append(RollbackResult(
                    action.description, "undone", "no verifier supplied, "
                    "nothing was checked", order, None))
                continue

            try:
                ok, error = bool(action.verify()), ""
            except Exception as exc:
                ok, error = False, f"verifier raised {type(exc).__name__}: {exc}"

            if ok:
                results.append(RollbackResult(action.description, "undone", "",
                                              order, True))
                continue

            results.append(RollbackResult(
                action.description, "unverified",
                error or "undo ran but the system is not back to its prior state",
                order, False))
            if self.stop_on_unverified:
                break

        return RollbackReport(
            ok=all(r.status == "undone" and r.verified is not False for r in results),
            results=results)
