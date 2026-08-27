"""Task/step primitives: what an operator writes and what the agent proposes.

Depends on: PyYAML (lazily imported in `TaskSpec.load`/`to_yaml`), stdlib `subprocess`/
`queue` (lazily imported in `shell`/`_shell_streaming`), stdlib `threading` (module-level,
for the `shell_output_sink` thread-local).
"""

from __future__ import annotations

import functools
import os
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "HarnessError", "OllamaError", "ContextBudgetError",
    "Step", "StepResult", "TaskSpec", "TaskSpecError",
    "shell", "shell_output_sink",
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


#: Set only inside `shell_output_sink`'s `with` block, read only by `shell()`
#: -- see that context manager's docstring for why this exists instead of a
#: parameter on `shell()` or `Step.run`.
_execution_context = threading.local()


@contextmanager
def shell_output_sink(on_output: Callable[[str], None] | None) -> Iterator[None]:
    """Scope `shell()`'s live-output callback to the calling thread, for
    the duration of the block.

    `shell()` has no `on_output` parameter of its own, and `Step.run`
    can't be given one either: `TaskSpec.to_steps()` builds every step's
    `run` closure once, at plan/gate time (`Harness._gate`), long before
    the plan is approved and long before any live consumer exists to hand
    it a callback -- and `Plan.steps` keeps those exact `Step` objects
    (identity matters for approval-matching and undo) all the way through
    to `execute()`, rather than rebuilding them. A thread-local sink lets
    `Harness.execute()` opt one real run into streaming without changing
    `Step.run`'s call convention (still zero-arg, unchanged for every
    existing `Step(run=...)` in tests/examples) or touching the
    plan-time-built `Step` objects at all.

    Safe across concurrent sessions: each session's real execution runs
    entirely on its own worker thread (`tui.py`'s per-session
    `run_worker(..., thread=True)`), and `threading.local()` storage is
    genuinely per-OS-thread -- setting it here is invisible to any other
    thread, including another session's concurrent run.
    """
    if on_output is None:
        yield
        return
    previous = getattr(_execution_context, "on_output", None)
    _execution_context.on_output = on_output
    try:
        yield
    finally:
        _execution_context.on_output = previous


def _pipefail_wrapped(command: str) -> str:
    """`command`, prefixed with `set -o pipefail` and suffixed with a check
    that forgives a `SIGPIPE` (exit 128+13=141) on any non-final pipeline
    stage when the final stage genuinely exited 0.

    `pipefail` alone is exactly right for its original purpose (`sudo
    something | sort | head` reporting success with empty output when
    `sudo` alone fails, undetectable without it) -- but it is *also* right
    for a case nobody wanted caught: `du -ah /home | sort -rh | head -20`,
    an entirely ordinary, benign command, fails under plain `pipefail`
    because `head` closes its input after 20 lines, `sort`/`du` upstream
    get killed by `SIGPIPE` for writing to a closed pipe, and `pipefail`
    reports that as the pipeline's own failure even though `head` already
    produced exactly the right output. Found live: a real operator hit
    this exact command shape four times in a row, including retrying with
    a different phrasing and asking the harness to diagnose its own bug,
    getting the identical failure every time.

    Distinguishing the two needs per-stage exit codes (`$PIPESTATUS`), not
    just the pipeline's own overall `$?` -- and capturing `$PIPESTATUS`
    has its own trap: bash resets it after *any* subsequent simple
    command, including a bare variable assignment, so `ec=$?` immediately
    destroys it before it can be read. The array capture below has to be
    the first statement after `command` runs, with nothing (not even
    reading `$?`) in between; `_ec` is then computed by hand from that
    array using the identical rule `pipefail` itself uses (the rightmost
    non-zero stage, or 0 if all stages succeeded), so this doesn't depend
    on bash's own already-reset `$?` at all. `_ec` is only overridden back
    to 0 when the final stage is 0 *and* every earlier non-zero stage is
    specifically 141 -- a real failure anywhere in the pipe (auth failure,
    missing command, wrong flag) is never forgiven, verified against the
    exact `sudo | sort | head` scenario `pipefail` exists for in the first
    place, not just the new benign case.
    """
    return (
        "set -o pipefail\n"
        + command + "\n"
        "_ps=(\"${PIPESTATUS[@]}\")\n"
        "_last=$((${#_ps[@]}-1))\n"
        "_ec=0\n"
        "for _i in \"${_ps[@]}\"; do\n"
        "  if [ \"$_i\" -ne 0 ]; then _ec=$_i; fi\n"
        "done\n"
        "if [ \"$_ec\" -ne 0 ] && [ \"${_ps[$_last]}\" -eq 0 ]; then\n"
        "  _ok=1\n"
        "  for _i in \"${_ps[@]:0:$_last}\"; do\n"
        "    if [ \"$_i\" -ne 0 ] && [ \"$_i\" -ne 141 ]; then _ok=0; fi\n"
        "  done\n"
        "  if [ \"$_ok\" -eq 1 ]; then _ec=0; fi\n"
        "fi\n"
        "exit \"$_ec\"\n"
    )


def shell(command: str, cwd: str | None = None,
          timeout: float | None = None) -> str:
    """Run one shell command, raising on a non-zero exit.

    Raising is deliberate. The executor treats a raising step as failed, which
    is what arms rollback. A step that swallowed a non-zero exit would report
    success and leave the system half changed.
    """
    on_output = getattr(_execution_context, "on_output", None)
    if on_output is not None:
        return _shell_streaming(command, cwd, timeout, on_output)

    import subprocess

    # Explicit bash with pipefail, not shell=True's default /bin/sh (dash on
    # this and most Debian-derived systems): dash's pipeline exit status is
    # only the *last* command's, so `sudo something | sort | head` reports
    # success with empty output when sudo alone fails (e.g. no password and
    # no TTY) -- exactly the failure mode that let a `du` step "complete"
    # having actually run nothing. Found live, not in a test. See
    # _pipefail_wrapped's own docstring for the SIGPIPE-forgiveness this
    # wrapping also does, and why plain `pipefail` alone isn't enough.
    result = subprocess.run(["/bin/bash", "-c", _pipefail_wrapped(command)],
                            capture_output=True, text=True,
                            cwd=cwd, timeout=timeout or 3600)
    if result.returncode != 0:
        raise RuntimeError(
            f"exit {result.returncode}: "
            f"{(result.stderr or result.stdout).strip()[:400]}")
    return (result.stdout or "").strip()


def _shell_streaming(command: str, cwd: str | None, timeout: float | None,
                     on_output: Callable[[str], None]) -> str:
    """`shell()`'s live-output path: same pipefail/timeout/exit-code contract
    as the default path above, calling `on_output` with each line of output
    as it arrives instead of only returning the whole thing at the end.

    stdout and stderr are captured into two separate pipes, exactly like
    the default path's `capture_output=True` -- not merged into one stream
    -- so the return value on success and the exception message on failure
    mean the same thing regardless of whether a caller was watching live.
    Reading two live pipes without deadlocking (a full stderr pipe while
    blocked reading stdout, or vice versa) needs one reader thread per
    pipe; both feed one queue so `on_output` still sees lines in something
    close to real arrival order rather than "all of stdout, then all of
    stderr". The timeout is enforced by the queue-read loop itself
    (`subprocess.run`'s own `timeout=` has no equivalent when reading
    incrementally), not by `Popen.wait`, so a process that never closes its
    output pipes still gets killed on schedule.
    """
    import queue
    import subprocess
    import time as _time

    proc = subprocess.Popen(
        ["/bin/bash", "-c", _pipefail_wrapped(command)],
        cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert proc.stdout is not None and proc.stderr is not None

    lines: queue.Queue[tuple[str, str] | None] = queue.Queue()

    def pump(stream: Any, tag: str) -> None:
        for line in iter(stream.readline, ""):
            lines.put((tag, line))
        stream.close()
        lines.put(None)

    readers = [
        threading.Thread(target=pump, args=(proc.stdout, "out"), daemon=True),
        threading.Thread(target=pump, args=(proc.stderr, "err"), daemon=True),
    ]
    for reader in readers:
        reader.start()

    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    deadline = _time.monotonic() + (timeout or 3600)
    open_streams = len(readers)
    while open_streams > 0:
        remaining = deadline - _time.monotonic()
        if remaining <= 0:
            proc.kill()
            for reader in readers:
                reader.join(timeout=1)
            raise subprocess.TimeoutExpired(command, timeout or 3600)
        try:
            item = lines.get(timeout=min(remaining, 0.5))
        except queue.Empty:
            continue
        if item is None:
            open_streams -= 1
            continue
        tag, line = item
        (stdout_parts if tag == "out" else stderr_parts).append(line)
        on_output(line)

    for reader in readers:
        reader.join()
    returncode = proc.wait(timeout=5)  # both pipes closed -- already exited, just reaping

    stdout = "".join(stdout_parts)
    stderr = "".join(stderr_parts)
    if returncode != 0:
        raise RuntimeError(f"exit {returncode}: {(stderr or stdout).strip()[:400]}")
    return stdout.strip()
