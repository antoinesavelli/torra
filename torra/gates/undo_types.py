"""The value types ROLLBACK_ON_FAIL passes around: `SnapshotKind`, `Snapshot` and `UndoAction`.

Depends on: stdlib only. `torra.gates.undo` re-exports every name.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

__all__ = ["SnapshotKind", "Snapshot", "UndoAction"]


class SnapshotKind(StrEnum):
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
