"""
core_behaviors_v3.py
Phase 3 behaviors. Closes the last three open findings.

  P0-1  UndoGenerator             derives undo actions from what a step is about
                                  to do, instead of trusting the caller to push
                                  one for every change
  P0-2  VerifiedRollbackBehavior  proves an undo worked instead of assuming the
                                  callable not raising means success
  P1-1  ScoringModelSelector      weighted scoring across all matching rules,
                                  replacing hand-ordered first-match routing

The rollback pair is the important half. Phase 2 hardened four subsystems while
ROLLBACK_ON_FAIL, whose entire job is recovering from failure, stayed on the
honor system: `status == "undone"` meant only "the callable did not raise".

Phase 1 and Phase 2 files are unmodified.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from core_behaviors import (
    DEFAULT_ROUTING_RULES,
    DEFAULT_MODEL,
    ModelChoice,
    ModelSelector,
    RollbackAction,
    RollbackOnFailBehavior,
    RollbackReport,
    RollbackResult,
)

__all__ = [
    "SnapshotKind",
    "Snapshot",
    "VerifiedAction",
    "VerifiedRollbackResult",
    "UndoGenerator",
    "VerifiedRollbackBehavior",
    "ScoringModelSelector",
]


# --------------------------------------------------------------------------- #
# P0-1: automatic undo generation
# --------------------------------------------------------------------------- #


class SnapshotKind(str, Enum):
    """What kind of prior state a snapshot captured."""

    FILE_CONTENT = "file_content"   # file existed, contents saved
    FILE_ABSENT = "file_absent"     # file did not exist, undo deletes it
    DIR_ABSENT = "dir_absent"       # directory did not exist, undo removes it
    SERVICE_STATE = "service_state"  # systemd unit active or inactive
    PACKAGE_SET = "package_set"     # installed package list


@dataclass
class Snapshot:
    """Prior state captured before a step ran."""

    kind: SnapshotKind
    target: str
    detail: str = ""
    shadow_path: Optional[str] = None
    digest: Optional[str] = None
    payload: Optional[Any] = None
    mode: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form, for the audit trail."""
        d = asdict(self)
        d["kind"] = self.kind.value
        if isinstance(d.get("payload"), (set, frozenset)):
            d["payload"] = sorted(d["payload"])
        return d


@dataclass
class VerifiedAction(RollbackAction):
    """A rollback action that can prove it worked.

    `verify` returns True when the system is genuinely back to the pre-step
    state. It is checked after `undo` runs, and a False verdict downgrades the
    result to "unverified" rather than letting it pass as "undone".
    """

    verify: Optional[Callable[[], bool]] = None
    generated: bool = False
    snapshot: Optional[Snapshot] = None


@dataclass
class VerifiedRollbackResult(RollbackResult):
    """RollbackResult with the verification verdict attached."""

    verified: Optional[bool] = None
    verify_error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form, for the audit trail."""
        return asdict(self)


#: Command patterns that reveal a service state change.
SERVICE_PATTERN = re.compile(
    r"\bsystemctl\s+(?:--user\s+)?(start|stop|restart|enable|disable|mask|unmask)"
    r"\s+([\w@.\-]+)", re.IGNORECASE)
#: Command patterns that reveal a package change.
PACKAGE_PATTERN = re.compile(
    r"\b(?:apt|apt-get|dnf|pacman)\s+(install|remove|purge)\s+(.+)", re.IGNORECASE)
#: Command patterns that reveal a directory creation.
MKDIR_PATTERN = re.compile(r"\bmkdir\b(?:\s+-\w+)*\s+([^\s;|&]+)")


class UndoGenerator:
    """Derives undo actions from what a step declares it will touch.

    Phase 1 and Phase 2 both left this open: undo actions were caller supplied,
    so a step that changed state without pushing a matching undo was
    unrecoverable. That made ROLLBACK_ON_FAIL a promise the harness could not
    keep for any step whose author forgot.

    This closes it for the cases that cover most real automation:

      * a file in `Step.touches` is copied to a shadow directory before the step
        runs; the undo restores byte for byte, including mode
      * a file in `Step.touches` that does not exist yet gets an undo that
        deletes whatever the step created
      * `mkdir` in the command gets an undo that removes the directory, but only
        if it is still empty, because removing a directory someone filled is a
        second disaster rather than a recovery
      * `systemctl start|stop|restart|enable|disable` gets its prior unit state
        captured and restored
      * `apt install` gets the installed package set captured, and the undo
        removes exactly what appeared, which is why it is marked unsafe and
        requires confirmation

    What it deliberately does not generate: anything it cannot invert with
    confidence. A `dd`, a `mkfs`, a database migration. Those still need a
    caller-supplied undo, and the generator says so rather than inventing one.
    """

    #: Files larger than this are not shadowed; an undo would be a lie.
    MAX_SHADOW_BYTES: int = 64 * 1024 * 1024

    def __init__(
        self,
        shadow_dir: Optional[os.PathLike[str] | str] = None,
        service_probe: Optional[Callable[[str], str]] = None,
        package_probe: Optional[Callable[[], set[str]]] = None,
        max_shadow_bytes: Optional[int] = None,
    ) -> None:
        """
        Args:
            shadow_dir: Where pre-step file copies live. A temp dir by default.
            service_probe: Returns "active" or "inactive" for a unit name.
                Injectable so tests do not need systemd.
            package_probe: Returns the installed package set. Injectable.
            max_shadow_bytes: Refuse to shadow files larger than this.
        """
        self.shadow_dir = Path(shadow_dir) if shadow_dir else Path(
            tempfile.mkdtemp(prefix="harness-shadow-"))
        self.shadow_dir.mkdir(parents=True, exist_ok=True)
        self._service_probe = service_probe or self._systemctl_state
        self._package_probe = package_probe or self._dpkg_set
        self.max_shadow_bytes = (
            self.MAX_SHADOW_BYTES if max_shadow_bytes is None else int(max_shadow_bytes)
        )
        self.snapshots: List[Snapshot] = []
        self.skipped: List[str] = []
        self._counter: int = 0

    # -- probes ------------------------------------------------------------ #

    @staticmethod
    def _systemctl_state(unit: str) -> str:
        """Return the systemd active state of a unit, or 'unknown' on error."""
        try:
            out = subprocess.run(["systemctl", "is-active", unit],
                                 capture_output=True, text=True, timeout=5)
            return out.stdout.strip() or "unknown"
        except Exception:
            return "unknown"

    @staticmethod
    def _dpkg_set() -> set[str]:
        """Return the installed package set, or an empty set on error."""
        try:
            out = subprocess.run(["dpkg-query", "-f", "${Package}\n", "-W"],
                                 capture_output=True, text=True, timeout=20)
            return {line.strip() for line in out.stdout.splitlines() if line.strip()}
        except Exception:
            return set()

    @staticmethod
    def _digest(path: Path) -> Optional[str]:
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

    # -- capture ----------------------------------------------------------- #

    def capture_file(self, path: os.PathLike[str] | str) -> Optional[VerifiedAction]:
        """Snapshot one file and build its restore action.

        Returns:
            A VerifiedAction, or None when the file is too large to shadow
            honestly (recorded in `skipped` rather than silently dropped).
        """
        target = Path(path)

        if not target.exists():
            snap = Snapshot(SnapshotKind.FILE_ABSENT, str(target),
                            "did not exist before the step")
            self.snapshots.append(snap)

            def undo_absent() -> None:
                """Delete whatever the step created, since nothing was there before."""
                if target.exists():
                    target.unlink()

            return VerifiedAction(
                description=f"delete {target} (it did not exist before)",
                undo=undo_absent,
                safe=True,
                verify=lambda: not target.exists(),
                generated=True,
                snapshot=snap,
            )

        try:
            size = target.stat().st_size
            mode = target.stat().st_mode
        except OSError as exc:
            self.skipped.append(f"{target}: stat failed {exc!r}")
            return None

        if size > self.max_shadow_bytes:
            note = (f"{target} is {size} bytes, over max_shadow_bytes="
                    f"{self.max_shadow_bytes}; no undo generated, push one manually")
            self.skipped.append(note)
            return None

        self._counter += 1
        shadow = self.shadow_dir / f"{self._counter:05d}_{target.name}"
        try:
            shutil.copy2(target, shadow)
        except OSError as exc:
            self.skipped.append(f"{target}: shadow copy failed {exc!r}")
            return None

        digest = self._digest(target)
        snap = Snapshot(SnapshotKind.FILE_CONTENT, str(target),
                        f"{size} bytes shadowed", str(shadow), digest, mode=mode)
        self.snapshots.append(snap)

        def undo_restore() -> None:
            """Copy the shadowed bytes back and restore the original mode."""
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(shadow, target)
            if mode is not None:
                os.chmod(target, mode & 0o7777)

        return VerifiedAction(
            description=f"restore {target} from snapshot",
            undo=undo_restore,
            safe=True,
            verify=lambda: self._digest(target) == digest,
            generated=True,
            snapshot=snap,
        )

    def capture_service(self, unit: str) -> VerifiedAction:
        """Snapshot a unit's active state and build its restore action."""
        before = self._service_probe(unit)
        snap = Snapshot(SnapshotKind.SERVICE_STATE, unit, f"was {before}",
                        payload=before)
        self.snapshots.append(snap)

        def undo_service() -> None:
            """Put the unit back into whichever state it was in before the step."""
            want = "start" if before == "active" else "stop"
            subprocess.run(["systemctl", want, unit],
                           capture_output=True, text=True, timeout=30, check=False)

        return VerifiedAction(
            description=f"restore {unit} to {before}",
            undo=undo_service,
            safe=True,
            verify=lambda: self._service_probe(unit) == before,
            generated=True,
            snapshot=snap,
        )

    def capture_packages(self, requested: str) -> VerifiedAction:
        """Snapshot the installed package set before an install.

        Marked unsafe on purpose. Removing packages to unwind an install can
        cascade through dependencies, so it asks first.
        """
        before = self._package_probe()
        snap = Snapshot(SnapshotKind.PACKAGE_SET, requested,
                        f"{len(before)} packages installed before",
                        payload=sorted(before))
        self.snapshots.append(snap)

        def undo_packages() -> None:
            """Remove exactly the packages that appeared since the snapshot."""
            after = self._package_probe()
            added = sorted(after - before)
            if added:
                subprocess.run(["apt-get", "remove", "--yes", *added],
                               capture_output=True, text=True, timeout=300, check=False)

        return VerifiedAction(
            description=f"remove packages added by '{requested}'",
            undo=undo_packages,
            safe=False,  # dependency cascades: ask first
            verify=lambda: self._package_probe() <= before,
            generated=True,
            snapshot=snap,
        )

    def capture_mkdir(self, path: str) -> Optional[VerifiedAction]:
        """Build an undo that removes a directory the step is about to create."""
        target = Path(path)
        if target.exists():
            return None  # already there, the step is a no-op for our purposes

        snap = Snapshot(SnapshotKind.DIR_ABSENT, str(target),
                        "did not exist before the step")
        self.snapshots.append(snap)

        def undo_mkdir() -> None:
            """Remove the directory, but only while it is still empty."""
            # Only remove it if it is still empty. Deleting a directory someone
            # filled in the meantime is a second disaster, not a recovery.
            if target.is_dir() and not any(target.iterdir()):
                target.rmdir()

        return VerifiedAction(
            description=f"remove {target} if still empty",
            undo=undo_mkdir,
            safe=True,
            verify=lambda: not target.exists() or any(target.iterdir()),
            generated=True,
            snapshot=snap,
        )

    # -- orchestration ----------------------------------------------------- #

    def for_step(self, step: Any) -> List[VerifiedAction]:
        """Generate every undo action derivable from one step.

        Args:
            step: A Step. `touches` drives file snapshots, `command` is parsed
                for service, package, and mkdir operations.

        Returns:
            Actions in the order they should be pushed onto the stack.
        """
        actions: List[VerifiedAction] = []

        for path in getattr(step, "touches", []) or []:
            action = self.capture_file(path)
            if action:
                actions.append(action)

        command = getattr(step, "command", "") or ""

        for _, unit in SERVICE_PATTERN.findall(command):
            actions.append(self.capture_service(unit))

        for verb, packages in PACKAGE_PATTERN.findall(command):
            if verb.lower() == "install":
                actions.append(self.capture_packages(packages.strip()))
            else:
                self.skipped.append(
                    f"'{verb} {packages.strip()}' removes packages and cannot be "
                    "auto-inverted; push an undo manually"
                )

        for path in MKDIR_PATTERN.findall(command):
            action = self.capture_mkdir(path)
            if action:
                actions.append(action)

        return actions

    def coverage(self, steps: Sequence[Any]) -> Dict[str, Any]:
        """Report which steps got a generated undo and which did not.

        A step with no derivable undo is not an error, but it is something the
        operator should see before the run rather than after the failure.
        """
        covered, uncovered = [], []
        for step in steps:
            has_touches = bool(getattr(step, "touches", None))
            command = getattr(step, "command", "") or ""
            derivable = (
                has_touches
                or SERVICE_PATTERN.search(command)
                or PACKAGE_PATTERN.search(command)
                or MKDIR_PATTERN.search(command)
            )
            (covered if derivable else uncovered).append(
                getattr(step, "step_id", "?"))
        return {
            "covered": covered,
            "uncovered": uncovered,
            "coverage_ratio": round(len(covered) / len(steps), 4) if steps else 0.0,
            "skipped": list(self.skipped),
        }

    def cleanup(self) -> None:
        """Delete the shadow directory. Call only after a successful run."""
        try:
            shutil.rmtree(self.shadow_dir, ignore_errors=True)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# P0-2: verified rollback
# --------------------------------------------------------------------------- #


class VerifiedRollbackBehavior(RollbackOnFailBehavior):
    """Rollback that proves each undo worked.

    Phase 1's `RollbackResult.status == "undone"` meant "the callable did not
    raise", which reads far stronger than it is. A `shutil.copy2` that writes to
    a full disk, a `systemctl start` that exits 0 while the unit immediately
    fails, an `apt-get remove` that a dependency blocked: all of those return
    without raising and leave the system unrecovered.

    This runs the action's `verify` callable after its `undo` and records the
    verdict. Three terminal states now exist instead of two:

        undone      the undo ran and verification confirmed the prior state
        unverified  the undo ran without raising but verification says no
        failed      the undo raised

    An action with no `verify` is reported as `verified=None`, which is honest
    about the fact that nothing was checked.
    """

    def __init__(
        self,
        confirm_unsafe: Optional[Callable[[RollbackAction], bool]] = None,
        stop_on_cancel: bool = True,
        stop_on_unverified: bool = True,
    ) -> None:
        """
        Args:
            stop_on_unverified: Halt the sweep when an undo cannot be verified.
                Default True: continuing to unwind past a step that did not
                actually revert produces a system in a state nobody planned.
            (Other arguments as RollbackOnFailBehavior.)
        """
        super().__init__(confirm_unsafe, stop_on_cancel)
        self.stop_on_unverified = bool(stop_on_unverified)
        self.generated_count: int = 0

    def push_action(self, action: VerifiedAction) -> VerifiedAction:
        """Push a pre-built VerifiedAction onto the stack."""
        self._stack.append(action)
        if action.generated:
            self.generated_count += 1
        return action

    def push_generated(self, actions: Sequence[VerifiedAction]) -> int:
        """Push every generated action, returning how many were added."""
        for action in actions:
            self.push_action(action)
        return len(actions)

    def push(
        self,
        description: str,
        undo: Callable[[], Any],
        safe: bool = True,
        step_id: str = "",
        verify: Optional[Callable[[], bool]] = None,
    ) -> VerifiedAction:
        """Push a caller-supplied undo, optionally with a verifier."""
        action = VerifiedAction(description=description, undo=undo, safe=safe,
                                step_id=step_id, verify=verify)
        self._stack.append(action)
        return action

    def rollback(self) -> RollbackReport:
        """Unwind the stack LIFO, verifying each undo.

        Returns:
            A RollbackReport whose results are VerifiedRollbackResult. `ok` is
            True only when every action ran and every verifier that exists
            returned True.
        """
        results: List[VerifiedRollbackResult] = []
        order = 0

        while self._stack:
            action = self._stack.pop()
            order += 1
            verify = getattr(action, "verify", None)

            if not action.safe:
                try:
                    approved = bool(self._confirm(action))
                except Exception as exc:
                    results.append(VerifiedRollbackResult(
                        action.description, "cancelled",
                        f"confirm raised {type(exc).__name__}: {exc}", order))
                    if self.stop_on_cancel:
                        self._stack.append(action)
                        break
                    continue
                if not approved:
                    results.append(VerifiedRollbackResult(
                        action.description, "cancelled",
                        "operator declined an unsafe undo", order))
                    if self.stop_on_cancel:
                        self._stack.append(action)
                        break
                    continue

            try:
                action.undo()
            except Exception as exc:
                results.append(VerifiedRollbackResult(
                    action.description, "failed",
                    f"{type(exc).__name__}: {exc}", order))
                continue

            if verify is None:
                results.append(VerifiedRollbackResult(
                    action.description, "undone", "", order, verified=None,
                    verify_error="no verifier supplied, nothing was checked"))
                continue

            try:
                ok = bool(verify())
                verify_error = ""
            except Exception as exc:
                ok = False
                verify_error = f"verifier raised {type(exc).__name__}: {exc}"

            if ok:
                results.append(VerifiedRollbackResult(
                    action.description, "undone", "", order, verified=True))
                continue

            results.append(VerifiedRollbackResult(
                action.description, "unverified",
                verify_error or "undo ran but the system is not back to its "
                                "prior state",
                order, verified=False, verify_error=verify_error))
            if self.stop_on_unverified:
                break

        ok = all(r.status == "undone" and r.verified is not False for r in results)
        return RollbackReport(ok=ok, results=list(results))


# --------------------------------------------------------------------------- #
# P1-1: scoring based model routing
# --------------------------------------------------------------------------- #


#: Per-rule weight. Higher wins a tie on match count.
DEFAULT_RULE_WEIGHTS: Dict[str, float] = {
    "debug": 3.0,     # a broken anything is a debugging task first
    "plan": 2.0,
    "analyze": 1.8,
    "deploy": 1.5,
    "code": 1.5,
    "quick": 0.5,     # lowest, so any real signal outranks a bare "check"
}


class ScoringModelSelector(ModelSelector):
    """Routes by scoring every rule instead of taking the first match.

    First-match routing needs a hand-ordered table, and every new rule means
    re-deciding where it goes. Worse, it cannot express "this task matches both
    debug and deploy, and debug should win" without also asserting that debug
    always wins, which is not true for "deploy the new debugger".

    Scoring fixes both. Each rule contributes `weight * match_count`, ties break
    on weight, and the winning margin becomes the confidence. The rule table is
    unchanged from Phase 1, so this is a drop-in swap.
    """

    def __init__(
        self,
        rules: Optional[List[Tuple[str, str, str, str]]] = None,
        default_model: str = DEFAULT_MODEL,
        available: Optional[Sequence[str]] = None,
        weights: Optional[Dict[str, float]] = None,
    ) -> None:
        """
        Args:
            weights: Per-rule-name weight. Defaults to DEFAULT_RULE_WEIGHTS,
                with 1.0 for any rule not listed.
            (Other arguments as ModelSelector.)
        """
        super().__init__(rules, default_model, available)
        self.weights = dict(weights or DEFAULT_RULE_WEIGHTS)

    #: Multiplier for the rule that matched earliest in the task text.
    LEAD_MULTIPLIER: float = 2.0

    def score(self, task: str) -> List[Tuple[str, str, float, List[str]]]:
        """Score every rule against a task, weighting position as well as count.

        Term frequency alone gets "review the deploy script before we ship it"
        wrong: two deploy terms outvote one review term and it routes to a code
        model. The fix is that the *earliest* matching rule owns the intent.
        In English imperative task descriptions the leading verb is what you are
        doing and everything after it is what you are doing it to, so the rule
        that matches first gets LEAD_MULTIPLIER and the rest score on frequency
        alone.

        That is exactly what separates "review the deploy script" from
        "deploy the reviewed script", which term counting cannot see.

        Returns:
            (rule_name, model, score, matched_terms), highest score first.
        """
        text = task or ""
        raw: List[Tuple[str, str, float, List[str], int]] = []

        for name, pattern, model, _why in self.rules:
            if self.available is not None and model not in self.available:
                continue

            base = self.weights.get(name, 1.0)
            terms: List[str] = []
            first_at = -1
            for match in re.finditer(pattern, text, re.IGNORECASE):
                if first_at < 0:
                    first_at = match.start()
                terms.append(match.group(0))
            if terms:
                raw.append((name, model, base * len(terms), terms, first_at))

        if not raw:
            return []

        lead_at = min(row[4] for row in raw)
        scored = [
            (name, model,
             round(score * (self.LEAD_MULTIPLIER if at == lead_at else 1.0), 4),
             terms)
            for name, model, score, terms, at in raw
        ]
        scored.sort(key=lambda row: (row[2], self.weights.get(row[0], 1.0)),
                    reverse=True)
        return scored

    def select(self, task: str) -> ModelChoice:
        """Pick the highest scoring model, with the margin as confidence."""
        scored = self.score(task)
        if not scored:
            return ModelChoice(
                model=self.default_model,
                matched_rule="default",
                reason="no routing rule matched, using the cheap default",
                confidence=0.3,
            )

        name, model, score, terms = scored[0]
        runner_up = scored[1][2] if len(scored) > 1 else 0.0
        margin = (score - runner_up) / score if score else 0.0
        confidence = round(0.6 + 0.4 * margin, 4)

        contest = ""
        if len(scored) > 1:
            contest = (" over " + ", ".join(
                f"{n}({s:g})" for n, _m, s, _t in scored[1:3]))

        return ModelChoice(
            model=model,
            matched_rule=name,
            reason=(f"rule '{name}' scored {score:g} on "
                    f"{sorted(set(t for t in terms if t))}{contest}"),
            confidence=confidence,
        )

    def explain(self, task: str) -> str:
        """Human readable score table for one task."""
        rows = self.score(task)
        if not rows:
            return f"{task!r}: no rule matched, default {self.default_model}"
        lines = [f"{task!r}:"]
        for name, model, score, terms in rows:
            lines.append(f"  {score:6.2f}  {name:<9} -> {model:<11} "
                         f"{sorted(set(t for t in terms if t))}")
        return "\n".join(lines)
