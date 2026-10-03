"""ROLLBACK_ON_FAIL, half two: a LIFO undo stack where every undo has to
prove it worked. The other half, `UndoGenerator` (deriving undos from step
state), lives in `undo.py` -- split out once this file crossed the 500
functional LOC modularity cap; see DEFENSIVE_CODING_STANDARDS.md §1.2.

Depends on: torra.gates.undo (UndoAction).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from .undo import UndoAction

__all__ = ["RollbackResult", "RollbackReport", "VerifiedRollback"]


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

    def _decline_unsafe(self, action: UndoAction, order: int) -> RollbackResult | None:
        """Ask before an unsafe undo runs. None means it was approved."""
        try:
            approved = bool(self._confirm(action))
        except Exception as exc:
            # A crashing confirm callback is treated the same as a decline,
            # not as the undo having failed -- the undo itself never ran,
            # so "cancelled" is the honest state.
            return RollbackResult(action.description, "cancelled",
                                  f"confirm raised {type(exc).__name__}: {exc}", order)
        if not approved:
            return RollbackResult(action.description, "cancelled",
                                  "operator declined an unsafe undo", order)
        return None

    @staticmethod
    def _run_and_verify(action: UndoAction, order: int) -> RollbackResult:
        """Run one undo and verify it, assuming approval is already settled."""
        try:
            action.undo()
        except Exception as exc:
            return RollbackResult(action.description, "failed",
                                  f"{type(exc).__name__}: {exc}", order)
        if action.verify is None:
            return RollbackResult(action.description, "undone",
                                  "no verifier supplied, nothing was checked", order, None)
        try:
            ok, error = bool(action.verify()), ""
        except Exception as exc:
            ok, error = False, f"verifier raised {type(exc).__name__}: {exc}"
        if ok:
            return RollbackResult(action.description, "undone", "", order, True)
        return RollbackResult(
            action.description, "unverified",
            error or "undo ran but the system is not back to its prior state", order, False)

    def rollback(self) -> RollbackReport:
        """Unwind the stack newest first, verifying each undo."""
        # LIFO: the last change made is the first one undone, the same order
        # a human unwinding a mistake by hand would use, and the only order
        # guaranteed not to undo a later step's undo out from under it. When
        # an unsafe action is declined (or its confirm callback raises), the
        # action is pushed back before breaking (rather than dropped) so
        # `depth`/`peek` still reflect it -- a caller inspecting the stack
        # after a halted sweep sees what is still pending, not a gap.
        results: list[RollbackResult] = []
        order = 0

        while self._stack:
            action = self._stack.pop()
            order += 1

            if not action.safe:
                decline = self._decline_unsafe(action, order)
                if decline is not None:
                    results.append(decline)
                    if self.stop_on_cancel:
                        self._stack.append(action)
                        break
                    continue

            result = self._run_and_verify(action, order)
            results.append(result)
            if result.status == "unverified" and self.stop_on_unverified:
                break

        return RollbackReport(
            ok=all(r.status == "undone" and r.verified is not False for r in results),
            results=results)
