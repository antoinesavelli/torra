"""
harness_ops.py
Phase 6. The operational surface.

Phase 5 ended with a recommendation: stop building gates, run the harness on
real work for a month, and let the telemetry say what to do next. Then it became
obvious why that had not happened. Running a single task meant writing a Python
script that imports six modules, constructs eight collaborators, and builds Step
objects with lambdas. Nobody does that fifty times a week.

So Phase 6 adds no gates. It removes the friction that would stop the evidence
from ever being collected:

  RotatingTaskLogger  size-based rotation, the one preemptive fix Phase 5 asked
                      for, because unbounded growth bites at the worst moment
  TaskSpec            a task is a YAML file, not a Python program
  SystemdUnitBuilder  generates the unit and timer for a weekly evidence report

Phases 1 through 5 are unmodified.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from harness_core import Step
from harness_telemetry import TaskLogger, TaskRecord

__all__ = [
    "RotationReport",
    "RotatingTaskLogger",
    "TaskSpecError",
    "TaskSpec",
    "SystemdUnitBuilder",
]


# --------------------------------------------------------------------------- #
# Log rotation
# --------------------------------------------------------------------------- #


@dataclass
class RotationReport:
    """What one rotation pass did."""

    rotated: bool = False
    from_path: str = ""
    to_path: str = ""
    bytes_moved: int = 0
    archives_kept: int = 0
    archives_removed: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Serializable dict form, for the audit trail."""
        return asdict(self)


class RotatingTaskLogger(TaskLogger):
    """TaskLogger that rotates itself before the file becomes a problem.

    Phase 5 measured 516 bytes per task, about 9 MB a year at fifty tasks a day,
    and called that survivable but unbounded. Unbounded is the operative word: a
    chatty automation loop changes that number by an order of magnitude without
    anyone noticing until a disk fills during something important.

    Rotation is checked on write rather than on a timer, because a timer needs a
    daemon and a daemon is a thing that can be down. Archives are numbered, not
    timestamped, so the naming cannot collide and `.1` always means most recent.

    Read still spans the archives, so rotating does not lose history for
    `drift()` or `loophole()`. That matters: a rotation scheme that quietly
    shrinks the evidence base defeats the point of collecting it.
    """

    DEFAULT_MAX_BYTES: int = 32 * 1024 * 1024
    DEFAULT_KEEP: int = 5

    def __init__(
        self,
        path: os.PathLike[str] | str,
        clock: Callable[[], float] = time.time,
        redact: bool = True,
        max_request_chars: int = 2000,
        max_bytes: int = DEFAULT_MAX_BYTES,
        keep: int = DEFAULT_KEEP,
    ) -> None:
        """
        Args:
            max_bytes: Rotate once the active log passes this size.
            keep: How many numbered archives to retain. Older ones are deleted.
            (Other arguments as TaskLogger.)
        """
        super().__init__(path, clock, redact, max_request_chars)
        self.max_bytes = max(1, int(max_bytes))
        self.keep = max(0, int(keep))
        self.rotations: int = 0

    def archive_path(self, index: int) -> Path:
        """Path of archive N, where 1 is the most recent."""
        return self.path.with_suffix(self.path.suffix + f".{index}")

    def archives(self) -> List[Path]:
        """Existing archives, most recent first.

        Indexed numerically rather than sorted by name. `sorted()` on these
        paths puts `.10` between `.1` and `.2`, which silently reorders history
        the moment `keep` goes above nine.
        """
        return [p for p in (self.archive_path(i) for i in range(1, self.keep + 2))
                if p.exists()]

    def should_rotate(self) -> bool:
        """True when the active log has passed max_bytes."""
        try:
            return self.path.exists() and self.path.stat().st_size >= self.max_bytes
        except OSError:
            return False

    def rotate(self, force: bool = False) -> RotationReport:
        """Shift the active log to `.1` and renumber the archives.

        Returns:
            A RotationReport. Never raises: a rotation failure must not take
            down the task being logged, so it is recorded and reported.
        """
        report = RotationReport()
        if not force and not self.should_rotate():
            return report
        if not self.path.exists():
            return report

        try:
            size = self.path.stat().st_size

            # Drop the oldest archive first. It is about to be pushed out of
            # the window, and shifting onto it instead would silently overwrite
            # it: the first version of this loop started the shift at keep-1,
            # which destroyed one archive per rotation without saying so.
            for index in range(self.keep, self.keep + 2):
                candidate = self.archive_path(index)
                if candidate.exists():
                    candidate.unlink()
                    report.archives_removed.append(str(candidate))

            # Then shift the rest down one slot: .4 -> .5, ..., .1 -> .2.
            for index in range(self.keep - 1, 0, -1):
                source = self.archive_path(index)
                if source.exists():
                    os.replace(source, self.archive_path(index + 1))

            if self.keep > 0:
                os.replace(self.path, self.archive_path(1))
                report.to_path = str(self.archive_path(1))
            else:
                self.path.unlink()
                report.to_path = "<discarded, keep=0>"

            report.rotated = True
            report.from_path = str(self.path)
            report.bytes_moved = size
            report.archives_kept = len(self.archives())
            self.rotations += 1
        except OSError as exc:
            note = f"rotation failed: {exc!r}"
            report.errors.append(note)
            self.errors.append(note)
        return report

    def record(self, record: TaskRecord) -> bool:
        """Append one record, rotating first when the log is oversized."""
        if self.should_rotate():
            self.rotate()
        return super().record(record)

    def read(self, limit: Optional[int] = None,
             include_archives: bool = True) -> List[TaskRecord]:
        """Read records across the active log and its archives, oldest first.

        Args:
            limit: Return at most this many, most recent.
            include_archives: Read rotated files too. On by default, because
                a rotation that silently shrinks the evidence base defeats the
                purpose of keeping one.
        """
        records: List[TaskRecord] = []
        if include_archives:
            # Oldest archive first, by index rather than by name.
            for archive in reversed(self.archives()):
                saved, self.path = self.path, archive
                try:
                    records.extend(super().read())
                finally:
                    self.path = saved
        records.extend(super().read())
        return records[-limit:] if limit else records

    def stats(self) -> Dict[str, Any]:
        """Counters plus rotation state."""
        base = super().stats()
        base.update({
            "rotations": self.rotations,
            "max_bytes": self.max_bytes,
            "keep": self.keep,
            "active_bytes": self.path.stat().st_size if self.path.exists() else 0,
            "archives": [str(p) for p in self.archives()],
        })
        return base


# --------------------------------------------------------------------------- #
# Task specs
# --------------------------------------------------------------------------- #


class TaskSpecError(ValueError):
    """Raised when a task file is malformed. Loud on purpose."""


@dataclass
class TaskSpec:
    """A task described as data rather than as a Python program.

    The Phase 5 recommendation was to run the harness on real work for a month.
    That was never going to happen while running one task meant importing six
    modules and building Step objects with lambdas.

    A task file looks like this:

        task_id: deploy-api-2026-07-27
        request: deploy commit a1b2c3d to staging
        watch_roots: [/srv/api]
        steps:
          - id: pull
            kind: shell
            run: git -C /srv/api pull --ff-only
            estimated_seconds: 20
            touches: [/srv/api/docker-compose.yml]
          - id: up
            kind: deploy
            run: docker compose -f /srv/api/docker-compose.yml up -d
            estimated_seconds: 240

    `run` is the shell command the step executes. It is also what the safety
    classifier sees, so what gets classified is exactly what gets run. That
    equivalence is the whole reason this is worth having: a Python lambda can
    execute something different from the `command` string next to it, and no
    gate would ever know.
    """

    task_id: str
    request: str
    steps: List[Dict[str, Any]] = field(default_factory=list)
    watch_roots: List[str] = field(default_factory=list)
    rollback: List[Dict[str, Any]] = field(default_factory=list)
    source: str = ""

    #: Fields a step may declare. Anything else is a typo, and typos in a file
    #: that drives root shell commands should be errors, not shrugs.
    STEP_FIELDS = {"id", "kind", "run", "description", "estimated_seconds",
                   "touches", "cwd", "timeout"}

    @classmethod
    def load(cls, path: os.PathLike[str] | str) -> "TaskSpec":
        """Read a task file.

        Raises:
            TaskSpecError: on a missing file, bad YAML, missing required key,
                or an unknown step field.
        """
        import yaml

        target = Path(path)
        if not target.exists():
            raise TaskSpecError(f"task file not found: {target}")
        try:
            data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            raise TaskSpecError(f"{target}: unreadable YAML: {exc}") from exc
        if not isinstance(data, dict):
            raise TaskSpecError(f"{target}: top level must be a mapping")

        for key in ("task_id", "request", "steps"):
            if key not in data:
                raise TaskSpecError(f"{target}: missing required key '{key}'")
        if not isinstance(data["steps"], list) or not data["steps"]:
            raise TaskSpecError(f"{target}: 'steps' must be a non-empty list")

        seen: set[str] = set()
        for index, raw in enumerate(data["steps"], start=1):
            if not isinstance(raw, dict):
                raise TaskSpecError(f"{target}: step {index} must be a mapping")
            unknown = set(raw) - cls.STEP_FIELDS
            if unknown:
                raise TaskSpecError(
                    f"{target}: step {index} has unknown field(s) "
                    f"{sorted(unknown)}; known fields are {sorted(cls.STEP_FIELDS)}")
            if "run" not in raw:
                raise TaskSpecError(f"{target}: step {index} has no 'run' command")
            step_id = str(raw.get("id") or f"step{index}")
            if step_id in seen:
                raise TaskSpecError(f"{target}: duplicate step id '{step_id}'")
            seen.add(step_id)

        return cls(
            task_id=str(data["task_id"]),
            request=str(data["request"]),
            steps=list(data["steps"]),
            watch_roots=[str(r) for r in (data.get("watch_roots") or [])],
            rollback=list(data.get("rollback") or []),
            source=str(target),
        )

    def commands(self) -> List[str]:
        """Every shell command this task will run, in order."""
        return [str(raw["run"]) for raw in self.steps]

    def to_steps(
        self,
        dry_run: bool = False,
        runner: Optional[Callable[[str, Optional[str], Optional[float]], str]] = None,
    ) -> List[Step]:
        """Build executable Steps.

        Args:
            dry_run: Print what would run instead of running it. The safety
                gate still classifies the real command, so a dry run exercises
                every gate without touching the system.
            runner: Override how a command is executed. Injectable for tests.

        Returns:
            Steps whose `command` is byte for byte what `run` executes.
        """
        execute = runner or self._shell

        def build(raw: Dict[str, Any], index: int) -> Step:
            """Turn one step mapping into an executable Step."""
            command = str(raw["run"])
            step_id = str(raw.get("id") or f"step{index}")
            cwd = raw.get("cwd")
            timeout = raw.get("timeout")

            def action() -> str:
                """Run this step's command, or describe it under dry run."""
                if dry_run:
                    return f"[dry-run] {command}"
                return execute(command, cwd, timeout)

            return Step(
                step_id=step_id,
                kind=str(raw.get("kind") or "shell"),
                command=command,
                description=str(raw.get("description") or command),
                estimated_seconds=(float(raw["estimated_seconds"])
                                   if raw.get("estimated_seconds") is not None else None),
                touches=[str(t) for t in (raw.get("touches") or [])],
                run=action,
            )

        return [build(raw, i) for i, raw in enumerate(self.steps, start=1)]

    @staticmethod
    def _shell(command: str, cwd: Optional[str] = None,
               timeout: Optional[float] = None) -> str:
        """Run one shell command, raising on a non-zero exit.

        Raises on failure on purpose: the executor treats a raising step as a
        failed step, which is what triggers rollback. A step that swallowed a
        non-zero exit would report success and leave the system half-changed.
        """
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            cwd=cwd, timeout=timeout or 3600,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"exit {result.returncode}: {(result.stderr or result.stdout).strip()[:400]}"
            )
        return (result.stdout or "").strip()

    def rollback_actions(
        self, runner: Optional[Callable[[str, Optional[str], Optional[float]], str]] = None
    ) -> List[Tuple[str, Callable[[], Any], bool]]:
        """Caller-supplied undo actions declared in the task file.

        Phase 3 generates undos for files, services, packages, and mkdir. This
        is for the rest: migrations, and anything else the generator honestly
        reports as uncovered.
        """
        execute = runner or self._shell
        out: List[Tuple[str, Callable[[], Any], bool]] = []
        for raw in self.rollback:
            if not isinstance(raw, dict) or "run" not in raw:
                continue
            command = str(raw["run"])
            description = str(raw.get("description") or f"undo: {command}")
            safe = bool(raw.get("safe", True))
            out.append((description, lambda c=command: execute(c, None, None), safe))
        return out


# --------------------------------------------------------------------------- #
# systemd
# --------------------------------------------------------------------------- #


class SystemdUnitBuilder:
    """Generates the user units for a scheduled evidence report.

    A user timer rather than a system one, and rather than cron, because it does
    not need root, it survives reboots, and `journalctl --user -u` gives an
    operator somewhere to look when the report stops arriving. A cron job that
    silently stops running is how a month of intended evidence collection turns
    into nothing.
    """

    def __init__(
        self,
        venv_python: os.PathLike[str] | str,
        harness_dir: os.PathLike[str] | str,
        workdir: os.PathLike[str] | str,
        on_calendar: str = "Mon 09:00",
    ) -> None:
        """
        Args:
            venv_python: Absolute path to the venv interpreter. Absolute
                because systemd has no shell and no PATH worth trusting.
            harness_dir: Directory holding the harness modules.
            workdir: Harness working directory holding telemetry.
            on_calendar: systemd OnCalendar expression.
        """
        self.venv_python = Path(venv_python)
        self.harness_dir = Path(harness_dir)
        self.workdir = Path(workdir)
        self.on_calendar = on_calendar

    def service(self) -> str:
        """The .service unit text."""
        return f"""[Unit]
Description=Terminal agent harness evidence report
Documentation=file://{self.harness_dir}/README.md

[Service]
Type=oneshot
WorkingDirectory={self.harness_dir}
ExecStart={self.venv_python} {self.harness_dir}/harness_cli.py evidence \\
    --workdir {self.workdir}
StandardOutput=journal
StandardError=journal
"""

    def timer(self) -> str:
        """The .timer unit text."""
        return f"""[Unit]
Description=Weekly harness evidence report

[Timer]
OnCalendar={self.on_calendar}
Persistent=true
RandomizedDelaySec=300

[Install]
WantedBy=timers.target
"""

    def install_hint(self, name: str = "harness-evidence") -> str:
        """The commands an operator runs to install what this generated."""
        return "\n".join([
            f"mkdir -p ~/.config/systemd/user",
            f"# write the two files, then:",
            f"systemctl --user daemon-reload",
            f"systemctl --user enable --now {name}.timer",
            f"systemctl --user list-timers {name}.timer",
            f"journalctl --user -u {name}.service -n 50",
        ])

    def write(self, target_dir: os.PathLike[str] | str,
              name: str = "harness-evidence") -> Tuple[Path, Path]:
        """Write both units to a directory. Returns (service, timer) paths."""
        directory = Path(target_dir)
        directory.mkdir(parents=True, exist_ok=True)
        service = directory / f"{name}.service"
        timer = directory / f"{name}.timer"
        service.write_text(self.service(), encoding="utf-8")
        timer.write_text(self.timer(), encoding="utf-8")
        return service, timer
