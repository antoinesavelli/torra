"""ROLLBACK_ON_FAIL, half one: derives undo actions from what a step declares
it will do. The other half, `VerifiedRollback` (proving each one worked),
lives in `rollback.py` -- split out once this file crossed the 500 functional
LOC modularity cap; see DEFENSIVE_CODING_STANDARDS.md §1.2.

Depends on: torra.gates.undo_types (the value types, re-exported here), torra.gates.undo_shadow
(the shadow directory), torra.gates.undo_remote (snapshots over the executor runner),
torra.gates.service_state.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .service_state import (
    restore_verbs,
    service_units,
    systemctl_query,
    systemctl_scope,
)
from .undo_remote import RemoteCapture
from .undo_types import Snapshot, SnapshotKind, UndoAction

__all__ = ["SnapshotKind", "Snapshot", "UndoAction", "UndoGenerator"]


#: apt/apt-get only, not dnf or pacman: `undo_packages` below hardcodes
#: `apt-get remove`, and the default `_package_probe` (`_dpkg_set`) only
#: understands dpkg. Matching a dnf/pacman install here would generate an
#: UndoAction that *looks* like a working safety net -- `safe=False`, a
#: verifier, a description -- but would fail (or on a system that happens
#: to have both package managers installed, remove the wrong packages)
#: the moment it actually ran. safety.py and clarifier.py recognize
#: dnf/pacman too, for classification -- that's a different, legitimate
#: concern from "can this generator actually invert it," which is the
#: only thing this pattern controls.
PACKAGE_PATTERN = re.compile(r"\b(?:apt|apt-get)\s+(install|remove|purge)\s+(.+)", re.IGNORECASE)
MKDIR_PATTERN = re.compile(r"\bmkdir\b(?:\s+-\w+)*\s+([^\s;|&]+)")


class UndoGenerator(RemoteCapture):
    """Derives undo actions from what a step declares it will do.

    Without this, ROLLBACK_ON_FAIL is a promise torra cannot keep for any
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
        enabled_probe: Callable[[str], str] | None = None,
        scope_probe: Callable[[str], str] | None = None,
        package_probe: Callable[[], set[str]] | None = None,
        max_shadow_bytes: int | None = None,
        prefer_reflink: bool = True,
        executor_runner: Callable[[str, str | None, float | None], str] | None = None,
    ) -> None:
        """Configure the shadow directory, injectable probes, and size/reflink policy.

        `executor_runner`, when given, is what `capture_remote_file()` uses
        to snapshot/restore a file on a second, SSH-reachable machine
        ("executor") -- the same runner `torra.core.machine.build_ssh_runner`
        already builds (§2.52). `None` (the default, and what every existing
        caller still gets) means `capture_remote_file` always returns `None`
        -- a `remote_push` step is then honestly "uncovered", never a
        fabricated undo (`HANDOFF.md` §2.53)."""
        import tempfile

        self.shadow_dir = (
            Path(shadow_dir)
            if shadow_dir
            else Path(tempfile.mkdtemp(prefix=f"torra-shadow-{os.getpid()}-"))
        )
        self.shadow_dir.mkdir(parents=True, exist_ok=True)
        self._scope_probe = scope_probe or self._systemctl_scope
        self._service_probe = service_probe or self._systemctl_state
        self._enabled_probe = enabled_probe or self._systemctl_enabled
        self._package_probe = package_probe or self._dpkg_set
        self.max_shadow_bytes = (
            self.MAX_SHADOW_BYTES if max_shadow_bytes is None else int(max_shadow_bytes)
        )
        self.prefer_reflink = bool(prefer_reflink)
        self._executor_runner = executor_runner
        self.reflink_supported: bool | None = None
        self.reflink_copies: int = 0
        self.byte_copies: int = 0
        self.snapshots: list[Snapshot] = []
        self.skipped: list[str] = []
        self._counter: int = 0
        self._claim_shadow_dir()
        self._reap_stale_shadows()

    # -- probes ------------------------------------------------------------ #

    _systemctl_scope = staticmethod(systemctl_scope)

    def _systemctl_state(self, unit: str) -> str:
        """Active state of a unit, or 'unknown' on error."""
        return systemctl_query("is-active", unit, self._scope_probe(unit))

    def _systemctl_enabled(self, unit: str) -> str:
        """Enablement of a unit (enabled, disabled, masked, static ...), or 'unknown' on error."""
        return systemctl_query("is-enabled", unit, self._scope_probe(unit))

    @staticmethod
    def _dpkg_set() -> set[str]:
        """Installed package set, or empty on error."""
        try:
            out = subprocess.run(
                ["dpkg-query", "-f", "${Package}\n", "-W"],
                capture_output=True,
                text=True,
                timeout=20,
            )
            return {line.strip() for line in out.stdout.splitlines() if line.strip()}
        except Exception:
            return set()

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
            result = subprocess.run(
                ["cp", "--reflink=always", str(probe), str(clone)],
                capture_output=True,
                text=True,
                timeout=10,
            )
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
                    ["cp", "--reflink=always", "--preserve=all", str(source), str(destination)],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if result.returncode == 0:
                    self.reflink_copies += 1
                    return "reflink"
            except Exception as exc:
                self.skipped.append(f"reflink copy of {source} failed: {exc!r}")
        shutil.copy2(source, destination)
        self.byte_copies += 1
        return "copy"

    # -- capture ----------------------------------------------------------- #

    def capture_file(self, path: os.PathLike[str] | str, step_id: str = "") -> UndoAction | None:
        """Snapshot one file and build its restore action."""
        target = Path(path)

        if not target.exists():
            snap = Snapshot(SnapshotKind.FILE_ABSENT, str(target), "did not exist before the step")
            self.snapshots.append(snap)

            def undo_absent() -> None:
                """Delete whatever the step created."""
                if target.exists():
                    target.unlink()

            return UndoAction(
                f"delete {target} (it did not exist before)",
                undo_absent,
                True,
                lambda: not target.exists(),
                True,
                snap,
                step_id,
            )

        try:
            stat = target.stat()
        except OSError as exc:
            self.skipped.append(f"{target}: stat failed {exc!r}")
            return None
        size, mode = stat.st_size, stat.st_mode
        if size > self.max_shadow_bytes:
            self.skipped.append(
                f"{target} is {size} bytes, over max_shadow_bytes="
                f"{self.max_shadow_bytes}; no undo generated, declare one manually"
            )
            return None

        self._counter += 1
        shadow = self.shadow_dir / f"{self._counter:05d}_{target.name}"
        try:
            how = self._copy(target, shadow)
        except OSError as exc:
            self.skipped.append(f"{target}: shadow copy failed {exc!r}")
            return None

        dig = self.digest(target)
        snap = Snapshot(
            SnapshotKind.FILE_CONTENT,
            str(target),
            f"{size} bytes via {how}",
            str(shadow),
            dig,
            mode=mode,
        )
        self.snapshots.append(snap)

        def undo_restore() -> None:
            """Copy the shadowed bytes back and restore the original mode."""
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(shadow, target)
            os.chmod(target, mode & 0o7777)

        return UndoAction(
            f"restore {target} from snapshot ({how})",
            undo_restore,
            True,
            lambda: self.digest(target) == dig,
            True,
            snap,
            step_id,
        )

    def capture_service(self, unit: str, step_id: str = "") -> UndoAction:
        """Snapshot a unit's state and build its restore action."""
        before = self._service_probe(unit)
        enabled_before = self._enabled_probe(unit)
        scope = self._scope_probe(unit)
        snap = Snapshot(
            SnapshotKind.SERVICE_STATE, unit, f"was {before}, {enabled_before}", payload=before
        )
        self.snapshots.append(snap)

        def undo_service() -> None:
            """Put the unit back into whichever state it was in: enablement, then running."""
            # Same scope the state was captured under, not a hardcoded
            # --user -- a system unit's undo used to silently target the
            # user systemd instance, which would run without error and
            # restore nothing.
            base = ["systemctl", *(["--user"] if scope == "user" else [])]
            for verb in restore_verbs(self._enabled_probe(unit), before, enabled_before):
                subprocess.run(
                    [*base, verb, unit], capture_output=True, text=True, timeout=30, check=False
                )

        return UndoAction(
            f"restore {unit} to {before}, {enabled_before}",
            undo_service,
            True,
            lambda: (
                "unknown" not in (before, enabled_before)  # never read: nothing to verify
                and self._service_probe(unit) == before
                and self._enabled_probe(unit) == enabled_before
            ),
            True,
            snap,
            step_id,
        )

    def capture_packages(self, requested: str, step_id: str = "") -> UndoAction:
        """Snapshot the package set. Marked unsafe: removals cascade."""
        before = self._package_probe()
        snap = Snapshot(
            SnapshotKind.PACKAGE_SET,
            requested,
            f"{len(before)} packages installed before",
            payload=sorted(before),
        )
        self.snapshots.append(snap)

        def undo_packages() -> None:
            """Remove exactly the packages that appeared since the snapshot."""
            added = sorted(self._package_probe() - before)
            if added:
                subprocess.run(
                    ["apt-get", "remove", "--yes", *added],
                    capture_output=True,
                    text=True,
                    timeout=300,
                    check=False,
                )

        return UndoAction(
            f"remove packages added by '{requested}'",
            undo_packages,
            False,
            lambda: self._package_probe() <= before,
            True,
            snap,
            step_id,
        )

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

        # Found live: the prior verify was `not target.exists() or
        # any(target.iterdir())` -- when undo_mkdir declines to rmdir a
        # non-empty directory (its own stated safety rule: never delete
        # real content), that predicate reads "still exists AND has
        # content" as *success*, so VerifiedRollback reported "undone" for
        # a directory -- with whatever got written into it -- that was
        # never actually removed. The prior state was "did not exist";
        # only "does not exist now" honestly confirms that.
        return UndoAction(
            f"remove {target} if still empty",
            undo_mkdir,
            True,
            lambda: not target.exists(),
            True,
            snap,
            step_id,
        )

    def for_step(self, step: Any) -> list[UndoAction]:
        """Every undo derivable from one step, in push order."""
        actions: list[UndoAction] = []
        step_id = getattr(step, "step_id", "")
        for path in getattr(step, "touches", []) or []:
            action = self.capture_file(path, step_id)
            if action:
                actions.append(action)

        if getattr(step, "kind", "") == "remote_push" and getattr(step, "remote_path", None):
            # Snapshotted at gate time, before the push has run for real --
            # same "capture before it happens" timing capture_file already
            # uses, just over the executor runner instead of the local
            # filesystem. None (no executor configured, unreachable, or the
            # snapshot itself failed) means genuinely no undo, not a
            # skipped entry silently treated as covered (HANDOFF.md §2.53).
            remote_action = self.capture_remote_file(step.remote_path, step_id)
            if remote_action:
                actions.append(remote_action)

        command = getattr(step, "command", "") or ""
        for unit in service_units(command):
            actions.append(self.capture_service(unit, step_id))
        for verb, packages in PACKAGE_PATTERN.findall(command):
            if verb.lower() == "install":
                actions.append(self.capture_packages(packages.strip(), step_id))
            else:
                self.skipped.append(
                    f"'{verb} {packages.strip()}' removes packages and cannot be "
                    "auto-inverted; declare a rollback entry"
                )
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
        snapped = {s.target for s in self.snapshots}
        for step in steps:
            command = getattr(step, "command", "") or ""
            touches = getattr(step, "touches", None) or []
            derivable = (
                bool(touches)
                and all(str(Path(t)) in snapped for t in touches)
                or service_units(command)
                or PACKAGE_PATTERN.search(command)
                or MKDIR_PATTERN.search(command)
            )
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

    # Live wire: HANDOFF.md §5.19
    def cleanup(self) -> None:
        """Delete shadow copies made so far, after a successful run. The directory is recreated empty
        because one generator serves every `plan()` of the session and the next snapshot needs it."""
        shutil.rmtree(self.shadow_dir, ignore_errors=True)
        self.snapshots.clear()
        self.shadow_dir.mkdir(parents=True, exist_ok=True)
        self._claim_shadow_dir()
        self._counter = 0
