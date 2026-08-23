"""Task/step primitives: what an operator writes and what the agent proposes.

Depends on: PyYAML (lazily imported in `TaskSpec.load`/`to_yaml`), stdlib `subprocess`
(lazily imported in `shell`).
"""

from __future__ import annotations

import functools
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "HarnessError", "OllamaError", "ContextBudgetError",
    "Step", "StepResult", "TaskSpec", "TaskSpecError",
]


class HarnessError(RuntimeError):
    """Base class for every non-recoverable harness fault."""


class OllamaError(HarnessError):
    """Raised when the model backend cannot be reached or returns nonsense."""


class ContextBudgetError(HarnessError):
    """Raised when a single entry alone exceeds the whole token budget."""


class TaskSpecError(ValueError):
    """Raised when a proposed task is malformed. Loud on purpose."""


@dataclass
class Step:
    """One unit of work.

    `command` is both what executes and what the safety gate classifies. Keeping
    those identical is the single most important invariant in the harness: a
    step whose executable payload differs from its classified text would slip
    past every gate without any of them being wrong.
    """

    step_id: str
    command: str = ""
    kind: str = "shell"
    description: str = ""
    estimated_seconds: float | None = None
    touches: list[str] = field(default_factory=list)
    cwd: str | None = None
    timeout: float | None = None
    run: Callable[[], Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serializable dict form, excluding the callable."""
        return {k: v for k, v in asdict(self).items() if k != "run"}


@dataclass
class StepResult:
    """Outcome of a single step."""

    step_id: str
    ok: bool
    output: str = ""
    error: str = ""
    seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Serializable dict form, for the audit trail."""
        return asdict(self)


#: Fields a step may declare. Anything else is a typo, and typos in a document
#: that drives root shell commands are errors rather than shrugs.
STEP_FIELDS: set[str] = {
    "id", "kind", "run", "description", "estimated_seconds", "touches",
    "cwd", "timeout",
}

#: Fields a rollback entry may declare. Same discipline as STEP_FIELDS --
#: found live: `rollback_actions()` silently `continue`d past any entry
#: missing `run` (e.g. a `cmd` typo), so `validate()` accepted the task and
#: the operator believed they had a declared rollback that had actually
#: been dropped without a word, at the exact moment (a failed run) they'd
#: go looking for it.
ROLLBACK_FIELDS: set[str] = {"run", "description", "cwd", "timeout", "safe"}


@dataclass
class TaskSpec:
    """A task as data: what an operator writes, and what the agent proposes.

    The agent producing the same structure a human writes by hand is not a
    convenience. It means the model's output travels the exact code path the
    existing test suite already covers, instead of a parallel one nobody
    validated.
    """

    task_id: str
    request: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    watch_roots: list[str] = field(default_factory=list)
    rollback: list[dict[str, Any]] = field(default_factory=list)
    source: str = ""
    notes: str = ""

    # -- construction ------------------------------------------------------ #

    @staticmethod
    def _validate_path_list(value: Any, field_name: str, where: str) -> None:
        """A path-list field must actually be a list, not a bare string.

        Found live: `touches: /etc/hosts` (missing brackets, an easy slip
        for a model or a hand-written file -- the schema example in
        agent.py shows a single-element list) used to pass straight
        through `[str(t) for t in value]` -- Python iterates a string
        character by character, so it silently became `['/', 'e', 't',
        'c', ...]` instead of raising. `to_steps()` and `validate()` both
        returned successfully; ConflictDetector then baselined that
        garbage instead of the real path, so the exact step that declared
        it needed watching was invisible to the conflict-detection safety
        net, and `Agent._prescreen()`'s "DANGEROUS step with no touches"
        repair check never fired either, since the (garbage) list was
        non-empty.
        """
        if value is not None and not isinstance(value, list):
            raise TaskSpecError(
                f"{where}: '{field_name}' must be a list of paths, not "
                f"{type(value).__name__}")

    @staticmethod
    def _validate_estimated_seconds(value: Any, where: str) -> None:
        """Must be numeric-coercible. `to_steps()` does `float(value)`
        unguarded -- an invalid value used to raise there instead of here,
        well past `Harness.plan()`'s narrow `except OllamaError | AgentError`
        clause, skipping every decision-log/audit/telemetry call that
        clause's catch normally reaches on the way to a proper `Outcome`.
        """
        if value is None:
            return
        try:
            float(value)
        except (TypeError, ValueError) as exc:
            raise TaskSpecError(f"{where}: 'estimated_seconds' must be a number") from exc

    @classmethod
    def _validate_step(cls, raw: Any, index: int, seen: set[str], source: str) -> None:
        """Validate one step mapping, raising on any defect. Adds its id to `seen`."""
        if not isinstance(raw, dict):
            raise TaskSpecError(f"{source or 'task'}: step {index} must be a mapping")
        unknown = set(raw) - STEP_FIELDS
        if unknown:
            raise TaskSpecError(
                f"{source or 'task'}: step {index} has unknown field(s) "
                f"{sorted(unknown)}; known fields are {sorted(STEP_FIELDS)}")
        if not raw.get("run"):
            raise TaskSpecError(f"{source or 'task'}: step {index} has no 'run' command")
        step_id = str(raw.get("id") or f"step{index}")
        if step_id in seen:
            raise TaskSpecError(f"{source or 'task'}: duplicate step id '{step_id}'")
        seen.add(step_id)
        where = f"{source or 'task'}: step {index}"
        cls._validate_path_list(raw.get("touches"), "touches", where)
        cls._validate_estimated_seconds(raw.get("estimated_seconds"), where)

    @staticmethod
    def _validate_rollback_entry(raw: Any, index: int, source: str) -> None:
        """Validate one rollback mapping, raising on any defect."""
        if not isinstance(raw, dict):
            raise TaskSpecError(
                f"{source or 'task'}: rollback entry {index} must be a mapping")
        unknown = set(raw) - ROLLBACK_FIELDS
        if unknown:
            raise TaskSpecError(
                f"{source or 'task'}: rollback entry {index} has unknown field(s) "
                f"{sorted(unknown)}; known fields are {sorted(ROLLBACK_FIELDS)}")
        if not raw.get("run"):
            raise TaskSpecError(
                f"{source or 'task'}: rollback entry {index} has no 'run' command")
        safe = raw.get("safe", True)
        if not isinstance(safe, bool):
            # bool("false") is True in Python -- a quoted "false" (an easy
            # JSON-formatting slip for a model) would otherwise silently
            # mean the opposite of what was declared, undoing the whole
            # point of the safe: false authors write to say "do not trust
            # the classifier alone here."
            raise TaskSpecError(
                f"{source or 'task'}: rollback entry {index}'s 'safe' must be "
                f"true or false, not {safe!r}")

    @classmethod
    def _validate_rollback(cls, data: dict[str, Any], source: str) -> None:
        """Validate every declared rollback entry, if any."""
        for index, raw in enumerate(data.get("rollback") or [], start=1):
            cls._validate_rollback_entry(raw, index, source)

    @classmethod
    def validate(cls, data: dict[str, Any], source: str = "") -> TaskSpec:
        """Build a TaskSpec from a mapping, rejecting anything malformed.

        A guard-clause cascade, each check raising immediately: this is the
        one gate between an arbitrary mapping (hand-written YAML or a
        model's JSON) and a TaskSpec that drives root shell commands, so a
        malformed document is a hard error here rather than a warning
        somewhere downstream where the context to explain it is gone.
        """
        label = source or "task"
        if not isinstance(data, dict):
            raise TaskSpecError(f"{label}: top level must be a mapping")
        for key in ("task_id", "request", "steps"):
            if key not in data:
                raise TaskSpecError(f"{label}: missing required key '{key}'")
        if not isinstance(data["steps"], list) or not data["steps"]:
            raise TaskSpecError(f"{label}: 'steps' must be a non-empty list")
        cls._validate_path_list(data.get("watch_roots"), "watch_roots", label)

        seen: set[str] = set()
        for index, raw in enumerate(data["steps"], start=1):
            cls._validate_step(raw, index, seen, source)

        cls._validate_rollback(data, source)

        return cls(
            task_id=str(data["task_id"]),
            request=str(data["request"]),
            steps=list(data["steps"]),
            watch_roots=[str(r) for r in (data.get("watch_roots") or [])],
            rollback=list(data.get("rollback") or []),
            source=source,
            notes=str(data.get("notes") or ""),
        )

    @classmethod
    def load(cls, path: os.PathLike[str] | str) -> TaskSpec:
        """Read a task file from disk."""
        import yaml

        target = Path(path)
        if not target.exists():
            raise TaskSpecError(f"task file not found: {target}")
        try:
            data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            raise TaskSpecError(f"{target}: unreadable YAML: {exc}") from exc
        return cls.validate(data, source=str(target))

    # -- use --------------------------------------------------------------- #

    def commands(self) -> list[str]:
        """Every shell command this task will run, in order."""
        return [str(raw["run"]) for raw in self.steps]

    def to_dict(self) -> dict[str, Any]:
        """Serializable dict form."""
        return asdict(self)

    def to_yaml(self) -> str:
        """Render as a task file an operator could save and re-run."""
        import yaml

        payload: dict[str, Any] = {"task_id": self.task_id, "request": self.request}
        if self.watch_roots:
            payload["watch_roots"] = self.watch_roots
        payload["steps"] = self.steps
        if self.rollback:
            payload["rollback"] = self.rollback
        return yaml.safe_dump(payload, sort_keys=False, default_flow_style=False)

    def to_steps(
        self,
        dry_run: bool = False,
        runner: Callable[[str, str | None, float | None], str] | None = None,
    ) -> list[Step]:
        """Build executable Steps whose command is byte for byte the `run` text."""
        execute = runner or shell

        def build(raw: dict[str, Any], index: int) -> Step:
            """Turn one step mapping into an executable Step."""
            command = str(raw["run"])
            cwd = raw.get("cwd")
            timeout = raw.get("timeout")

            def action() -> str:
                """Run this step's command, or describe it under dry run."""
                if dry_run:
                    return f"[dry-run] {command}"
                return execute(command, cwd, timeout)

            return Step(
                step_id=str(raw.get("id") or f"step{index}"),
                command=command,
                kind=str(raw.get("kind") or "shell"),
                description=str(raw.get("description") or command),
                estimated_seconds=(float(raw["estimated_seconds"])
                                   if raw.get("estimated_seconds") is not None else None),
                touches=[str(t) for t in (raw.get("touches") or [])],
                cwd=cwd,
                timeout=timeout,
                run=action,
            )

        return [build(raw, i) for i, raw in enumerate(self.steps, start=1)]

    def rollback_actions(
        self, runner: Callable[[str, str | None, float | None], str] | None = None
    ) -> list[tuple[str, str, Callable[[], Any], bool]]:
        """Undo actions declared in the task, for what cannot be derived."""
        # `command` is returned alongside the callable (not just baked into
        # it) so a caller can classify it before deciding whether to trust
        # the declared `safe` flag -- see Harness.execute, which does exactly
        # that rather than taking a plan's own safety self-assessment on
        # faith. Whoever declared this entry (a human or a model) only gets
        # to make it *more* cautious than the classifier would, never less.
        # cwd/timeout are read the same way to_steps' build() reads them for
        # a regular step -- found in a full-file audit: this used to always
        # pass (None, None), silently dropping a declared cwd or timeout on
        # a rollback entry with no error or warning. Most auto-generated
        # undos use absolute paths and never notice, but a hand-written or
        # model-proposed rollback command that relies on a relative path
        # (e.g. "cd into the deploy dir and git reset") would silently run
        # from the wrong directory.
        execute = runner or shell
        out: list[tuple[str, str, Callable[[], Any], bool]] = []
        for raw in self.rollback:
            if not isinstance(raw, dict) or not raw.get("run"):
                continue
            command = str(raw["run"])
            out.append((
                str(raw.get("description") or f"undo: {command}"),
                command,
                functools.partial(execute, command, raw.get("cwd"), raw.get("timeout")),
                bool(raw.get("safe", True)),
            ))
        return out


def shell(command: str, cwd: str | None = None,
          timeout: float | None = None) -> str:
    """Run one shell command, raising on a non-zero exit.

    Raising is deliberate. The executor treats a raising step as failed, which
    is what arms rollback. A step that swallowed a non-zero exit would report
    success and leave the system half changed.
    """
    import subprocess

    # Explicit bash with pipefail, not shell=True's default /bin/sh (dash on
    # this and most Debian-derived systems): dash's pipeline exit status is
    # only the *last* command's, so `sudo something | sort | head` reports
    # success with empty output when sudo alone fails (e.g. no password and
    # no TTY) -- exactly the failure mode that let a `du` step "complete"
    # having actually run nothing. Found live, not in a test.
    result = subprocess.run(["/bin/bash", "-c", f"set -o pipefail; {command}"],
                            capture_output=True, text=True,
                            cwd=cwd, timeout=timeout or 3600)
    if result.returncode != 0:
        raise RuntimeError(
            f"exit {result.returncode}: "
            f"{(result.stderr or result.stdout).strip()[:400]}")
    return (result.stdout or "").strip()
