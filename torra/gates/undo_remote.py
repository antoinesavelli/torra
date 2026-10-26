"""ROLLBACK_ON_FAIL for a file on the executor: snapshot and restore over the SSH runner.

Depends on: torra.gates.undo_shadow (ShadowStore), torra.gates.undo_types (Snapshot, SnapshotKind,
UndoAction). `torra.gates.undo.UndoGenerator` inherits `RemoteCapture`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

from .undo_shadow import ShadowStore
from .undo_types import Snapshot, SnapshotKind, UndoAction

__all__ = ["RemoteCapture"]


class RemoteCapture(ShadowStore):
    """`capture_remote_file`: the remote mirror of `UndoGenerator.capture_file`."""

    @staticmethod
    def _read_remote_bytes(runner: Callable[[str, str | None, float | None], str],
                           remote_path: str) -> bytes:
        """A remote file's exact bytes (the runner strips output, which `cat` lost newlines to)."""
        import base64
        import shlex
        return base64.b64decode(runner(f"base64 -w0 -- {shlex.quote(remote_path)}", None, 30.0),
                                validate=True)

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
                runner(f"rm -f -- {shlex.quote(remote_path)}", None, 15.0)

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

