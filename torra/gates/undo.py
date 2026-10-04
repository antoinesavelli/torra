"""ROLLBACK_ON_FAIL, half one: derives undo actions from what a step declares
it will do. The other half, `VerifiedRollback` (proving each one worked),
lives in `rollback.py` -- split out once this file crossed the 500 functional
LOC modularity cap; see DEFENSIVE_CODING_STANDARDS.md §1.2.

Depends on: stdlib only.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from .service_state import (
    restore_verbs,
    service_units,
    systemctl_query,
    systemctl_scope,
)

__all__ = ["SnapshotKind", "Snapshot", "UndoAction", "UndoGenerator"]


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
PACKAGE_PATTERN = re.compile(
    r"\b(?:apt|apt-get)\s+(install|remove|purge)\s+(.+)", re.IGNORECASE)
MKDIR_PATTERN = re.compile(r"\bmkdir\b(?:\s+-\w+)*\s+([^\s;|&]+)")


class UndoGenerator:
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
    #: A sibling torra-shadow-<pid>* directory is only reaped once it is
    #: BOTH this old AND its owning pid is dead -- an in-progress run, even
    #: a long one, must never have its snapshots pulled out from under it.
    STALE_SHADOW_SECONDS: float = 24 * 3600
    #: A directory with no owner record (from before `owner.pid` existed) waits this long.
    LEGACY_SHADOW_SECONDS: float = 7 * 24 * 3600

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

        self.shadow_dir = Path(shadow_dir) if shadow_dir else Path(
            tempfile.mkdtemp(prefix=f"torra-shadow-{os.getpid()}-"))
        self.shadow_dir.mkdir(parents=True, exist_ok=True)
        self._scope_probe = scope_probe or self._systemctl_scope
        self._service_probe = service_probe or self._systemctl_state
        self._enabled_probe = enabled_probe or self._systemctl_enabled
        self._package_probe = package_probe or self._dpkg_set
        self.max_shadow_bytes = (self.MAX_SHADOW_BYTES if max_shadow_bytes is None
                                 else int(max_shadow_bytes))
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
            return int(UndoGenerator._owner_file(entry).read_text(encoding="utf-8").strip())
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
        except ProcessLookupError:
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

    @staticmethod
    def _read_remote_bytes(runner: Callable[[str, str | None, float | None], str],
                           remote_path: str) -> bytes:
        """A remote file's exact bytes (the runner strips output, which `cat` lost newlines to)."""
        import base64
        import shlex
        return base64.b64decode(runner(f"base64 -w0 -- {shlex.quote(remote_path)}", None, 30.0))

    def capture_remote_file(self, remote_path: str, step_id: str = "") -> UndoAction | None:
        """Snapshot one file on executor (over `self._executor_runner`) and
        build its restore action -- the direct remote mirror of
        `capture_file`, same real, provable undo/verify bar.

        `None` (no `UndoAction` at all) whenever the remote original can't
        be captured for any reason -- no executor configured, unreachable,
        permission denied, anything. That makes the calling `remote_push`
        step honestly "uncovered", the same philosophy this class already
        applies to `dd`/`mkfs`/anything it cannot guarantee an inverse for:
        never a fabricated or best-effort undo (`HANDOFF.md` §2.53)."""
        if self._executor_runner is None:
            self.skipped.append(f"{remote_path} (executor): no executor configured")
            return None
        runner = self._executor_runner

        import shlex
        exists_probe = f"test -e {shlex.quote(remote_path)} && echo yes || echo no"
        try:
            exists = runner(exists_probe, None, 15.0).strip() == "yes"
        except Exception as exc:
            self.skipped.append(f"{remote_path} (executor): existence check failed {exc!r}")
            return None

        if not exists:
            def undo_absent() -> None:
                """Delete whatever the push created on executor."""
                runner(f"rm -f {shlex.quote(remote_path)}", None, 15.0)

            def verify_absent() -> bool:
                try:
                    return runner(exists_probe, None, 15.0).strip() == "no"
                except Exception:
                    return False

            snap = Snapshot(SnapshotKind.FILE_ABSENT, remote_path,
                            "did not exist on executor before the step")
            self.snapshots.append(snap)
            return UndoAction(
                f"delete {remote_path} on executor (it did not exist before)",
                undo_absent, True, verify_absent, True, snap, step_id)

        try:
            content = self._read_remote_bytes(runner, remote_path)
        except Exception as exc:
            self.skipped.append(f"{remote_path} (executor): shadow capture failed {exc!r}")
            return None

        self._counter += 1
        shadow = self.shadow_dir / f"{self._counter:05d}_executor_{Path(remote_path).name}"
        try:
            shadow.write_bytes(content)
        except OSError as exc:
            self.skipped.append(f"{remote_path} (executor): shadow write failed {exc!r}")
            return None

        dig = self.digest(shadow)
        snap = Snapshot(SnapshotKind.FILE_CONTENT, remote_path,
                        f"{len(content)} bytes via executor base64", str(shadow), dig)
        self.snapshots.append(snap)

        def undo_restore() -> None:
            """Push the shadowed bytes back to executor."""
            import base64
            encoded = base64.b64encode(shadow.read_bytes()).decode("ascii")
            runner(f"echo {shlex.quote(encoded)} | base64 -d > {shlex.quote(remote_path)}",
                  None, 30.0)

        def verify_restore() -> bool:
            try:
                current = self._read_remote_bytes(runner, remote_path)
            except Exception:
                return False
            return hashlib.sha256(current).hexdigest() == dig

        return UndoAction(f"restore {remote_path} on executor from snapshot",
                          undo_restore, True, verify_restore, True, snap, step_id)

    def capture_service(self, unit: str, step_id: str = "") -> UndoAction:
        """Snapshot a unit's state and build its restore action."""
        before = self._service_probe(unit)
        enabled_before = self._enabled_probe(unit)
        scope = self._scope_probe(unit)
        snap = Snapshot(SnapshotKind.SERVICE_STATE, unit, f"was {before}, {enabled_before}",
                        payload=before)
        self.snapshots.append(snap)

        def undo_service() -> None:
            """Put the unit back into whichever state it was in: enablement, then running."""
            # Same scope the state was captured under, not a hardcoded
            # --user -- a system unit's undo used to silently target the
            # user systemd instance, which would run without error and
            # restore nothing.
            base = ["systemctl", *(["--user"] if scope == "user" else [])]
            for verb in restore_verbs(self._enabled_probe(unit), before, enabled_before):
                subprocess.run([*base, verb, unit], capture_output=True, text=True,
                               timeout=30, check=False)

        return UndoAction(
            f"restore {unit} to {before}, {enabled_before}", undo_service, True,
            lambda: (self._service_probe(unit) == before
                     and self._enabled_probe(unit) == enabled_before),
            True, snap, step_id)

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

        # Found live: the prior verify was `not target.exists() or
        # any(target.iterdir())` -- when undo_mkdir declines to rmdir a
        # non-empty directory (its own stated safety rule: never delete
        # real content), that predicate reads "still exists AND has
        # content" as *success*, so VerifiedRollback reported "undone" for
        # a directory -- with whatever got written into it -- that was
        # never actually removed. The prior state was "did not exist";
        # only "does not exist now" honestly confirms that.
        return UndoAction(f"remove {target} if still empty", undo_mkdir, True,
                          lambda: not target.exists(),
                          True, snap, step_id)

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
        snapped = {s.target for s in self.snapshots}
        for step in steps:
            command = getattr(step, "command", "") or ""
            touches = getattr(step, "touches", None) or []
            derivable = (bool(touches) and all(str(Path(t)) in snapped for t in touches)
                         or service_units(command)
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
        """Delete shadow copies made so far. Only after a successful run.

        Recreates an empty `shadow_dir` afterward rather than leaving it
        gone: one `UndoGenerator` is built once per `Torra` and reused
        across every `plan()` call in the session (`_gate()` snapshots
        files at propose time, before execute() even knows whether the run
        will be real or dry), so the *next* plan's `capture_file` needs
        somewhere to write. Leaving the directory deleted would silently
        skip every snapshot for the rest of the session -- caught, but only
        as a `skipped` entry nobody is watching for.
        """
        shutil.rmtree(self.shadow_dir, ignore_errors=True)
        self.shadow_dir.mkdir(parents=True, exist_ok=True)
        self._claim_shadow_dir()
        self._counter = 0
