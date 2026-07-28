"""
test_phase6.py
Acceptance cases for the Phase 6 operational surface.

  log rotation      cases 102 to 106
  task specs        cases 107 to 113
  systemd units     cases 114 to 115
  CLI               cases 116 to 121
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import harness_cli  # noqa: E402
from harness_ops import (  # noqa: E402
    RotatingTaskLogger,
    SystemdUnitBuilder,
    TaskSpec,
    TaskSpecError,
)
from harness_telemetry import TaskRecord  # noqa: E402


def rec(request: str, **kw) -> TaskRecord:
    """Build a TaskRecord with sensible defaults."""
    base = dict(task_id="t", request=request, at=1.0)
    base.update(kw)
    return TaskRecord(**base)


def write_task(path: Path, body: str) -> Path:
    """Write a task file and return its path."""
    path.write_text(body, encoding="utf-8")
    return path


GOOD_TASK = """
task_id: t1
request: deploy commit a1b2c3d to staging
steps:
  - id: one
    kind: shell
    run: echo hello
    estimated_seconds: 5
  - id: two
    kind: write
    run: echo written
    touches: [/tmp/harness-test-nonexistent.conf]
"""


# --------------------------------------------------------------------------- #
# Log rotation, cases 102 to 106
# --------------------------------------------------------------------------- #


def test_case_102_rotates_when_oversized(tmp_path: Path) -> None:
    """Case 102: the Phase 5 gap. Unbounded growth is now bounded."""
    log = RotatingTaskLogger(tmp_path / "tasks.jsonl", max_bytes=2000, keep=3)
    for i in range(40):
        log.record(rec(f"deploy commit a1b2c3d{i} to staging"))

    assert log.rotations >= 1, log.stats()
    assert log.archive_path(1).exists()
    assert log.path.stat().st_size < 2000 + 1024, "the active log stays small"


def test_case_103_history_survives_rotation(tmp_path: Path) -> None:
    """Case 103: a rotation that shrinks the evidence base defeats the point."""
    # keep is sized to hold the whole run, so this measures whether reading
    # spans archives at all. Case 104 measures the retention window itself.
    log = RotatingTaskLogger(tmp_path / "tasks.jsonl", max_bytes=1500, keep=30)
    for i in range(60):
        log.record(rec(f"task number {i} on /srv/api"))

    assert log.rotations >= 1
    across = log.read()
    assert len(across) == 60, f"read {len(across)} of 60 across archives"

    active_only = log.read(include_archives=False)
    assert len(active_only) < 60, "the active log alone must be shorter"
    assert across[0].request == "task number 0 on /srv/api", "oldest first"
    assert across[-1].request == "task number 59 on /srv/api"


def test_case_104_keep_window_is_enforced(tmp_path: Path) -> None:
    """Case 104: archives beyond `keep` are removed, not accumulated."""
    log = RotatingTaskLogger(tmp_path / "tasks.jsonl", max_bytes=600, keep=2)
    for i in range(120):
        log.record(rec(f"task {i} on /srv/api with a reasonably long request"))

    archives = log.archives()
    assert len(archives) <= 2, [str(p) for p in archives]
    assert log.rotations >= 3, "several rotations should have happened"
    assert not log.archive_path(4).exists()


def test_case_105_rotation_failure_never_raises(tmp_path: Path) -> None:
    """Case 105: telemetry must not break a run, rotation included."""
    log = RotatingTaskLogger(tmp_path / "tasks.jsonl", max_bytes=100, keep=2)
    log.record(rec("a task with a request long enough to exceed one hundred bytes"))

    blocker = tmp_path / "blocker"
    blocker.write_text("file, not a directory", encoding="utf-8")
    log.path = blocker / "nested" / "tasks.jsonl"

    report = log.rotate(force=True)
    assert report.rotated is False
    assert log.record(rec("still should not raise")) is False
    assert log.errors


def test_case_106_keep_zero_discards(tmp_path: Path) -> None:
    """Case 106: keep=0 is a valid choice and means what it says."""
    log = RotatingTaskLogger(tmp_path / "tasks.jsonl", max_bytes=300, keep=0)
    for i in range(30):
        log.record(rec(f"task {i} with a request of some length here"))

    assert log.rotations >= 1
    assert log.archives() == []
    assert log.read() == log.read(include_archives=False)


# --------------------------------------------------------------------------- #
# Task specs, cases 107 to 113
# --------------------------------------------------------------------------- #


def test_case_107_loads_a_valid_task(tmp_path: Path) -> None:
    """Case 107: a task is a file."""
    spec = TaskSpec.load(write_task(tmp_path / "t.yaml", GOOD_TASK))

    assert spec.task_id == "t1"
    assert spec.request == "deploy commit a1b2c3d to staging"
    assert spec.commands() == ["echo hello", "echo written"]

    steps = spec.to_steps(dry_run=True)
    assert [s.step_id for s in steps] == ["one", "two"]
    assert steps[0].kind == "shell"
    assert steps[0].estimated_seconds == 5.0
    assert steps[1].touches == ["/tmp/harness-test-nonexistent.conf"]


def test_case_108_command_and_classified_text_are_identical(tmp_path: Path) -> None:
    """Case 108: the whole reason task files beat lambdas.

    A Python Step can execute something different from the `command` string next
    to it, and no gate would ever know. Here the executed command IS the
    classified command, by construction.
    """
    spec = TaskSpec.load(write_task(tmp_path / "t.yaml", """
task_id: t
request: deploy commit a1b2c3d to staging
steps:
  - id: dangerous
    run: rm -rf /var/lib/postgresql
"""))
    steps = spec.to_steps(dry_run=True)

    assert steps[0].command == "rm -rf /var/lib/postgresql"
    assert spec.commands() == [steps[0].command]
    assert steps[0].run() == "[dry-run] rm -rf /var/lib/postgresql"


def test_case_109_rejects_malformed_files_loudly(tmp_path: Path) -> None:
    """Case 109: typos in a file that drives root shell commands are errors."""
    cases = [
        ("missing key", "task_id: t\nsteps: [{run: echo hi}]\n", "request"),
        ("no steps", "task_id: t\nrequest: deploy to staging\nsteps: []\n", "steps"),
        ("step has no run", "task_id: t\nrequest: r\nsteps:\n  - id: a\n", "'run'"),
        ("unknown field", "task_id: t\nrequest: r\nsteps:\n  - run: echo\n"
                          "    touchs: [/etc]\n", "unknown field"),
        ("duplicate id", "task_id: t\nrequest: r\nsteps:\n  - id: a\n    run: echo\n"
                         "  - id: a\n    run: echo\n", "duplicate"),
        ("not a mapping", "- just\n- a\n- list\n", "mapping"),
    ]
    for label, body, fragment in cases:
        path = write_task(tmp_path / f"{label.replace(' ', '_')}.yaml", body)
        with pytest.raises(TaskSpecError) as caught:
            TaskSpec.load(path)
        assert fragment in str(caught.value), f"{label}: {caught.value}"

    with pytest.raises(TaskSpecError):
        TaskSpec.load(tmp_path / "does-not-exist.yaml")


def test_case_110_dry_run_executes_nothing(tmp_path: Path) -> None:
    """Case 110: every gate runs, the system is untouched."""
    marker = tmp_path / "should-not-exist"
    spec = TaskSpec.load(write_task(tmp_path / "t.yaml", f"""
task_id: t
request: deploy commit a1b2c3d to staging
steps:
  - id: touch
    run: touch {marker}
"""))
    for step in spec.to_steps(dry_run=True):
        step.run()
    assert not marker.exists()

    for step in spec.to_steps(dry_run=False):
        step.run()
    assert marker.exists(), "a real run must actually execute"


def test_case_111_failed_command_raises_so_rollback_fires(tmp_path: Path) -> None:
    """Case 111: a step that swallowed a non-zero exit would report success."""
    spec = TaskSpec.load(write_task(tmp_path / "t.yaml", """
task_id: t
request: deploy commit a1b2c3d to staging
steps:
  - id: fails
    run: exit 7
"""))
    step = spec.to_steps()[0]
    with pytest.raises(RuntimeError) as caught:
        step.run()
    assert "exit 7" in str(caught.value)


def test_case_112_rollback_actions_are_declarable(tmp_path: Path) -> None:
    """Case 112: for what the undo generator honestly cannot invert."""
    spec = TaskSpec.load(write_task(tmp_path / "t.yaml", """
task_id: t
request: deploy commit a1b2c3d to staging
steps:
  - id: migrate
    run: echo alembic upgrade head
rollback:
  - run: echo alembic downgrade -1
    description: downgrade the migration
    safe: false
"""))
    ran: list[str] = []
    actions = spec.rollback_actions(runner=lambda c, _cwd, _t: ran.append(c) or "")

    assert len(actions) == 1
    description, undo, safe = actions[0]
    assert description == "downgrade the migration"
    assert safe is False, "a declared-unsafe undo must stay unsafe"
    undo()
    assert ran == ["echo alembic downgrade -1"]


def test_case_113_cwd_and_timeout_are_honoured(tmp_path: Path) -> None:
    """Case 113: a step can name where it runs."""
    subdir = tmp_path / "work"
    subdir.mkdir()
    (subdir / "marker.txt").write_text("here", encoding="utf-8")

    spec = TaskSpec.load(write_task(tmp_path / "t.yaml", f"""
task_id: t
request: deploy commit a1b2c3d to staging
steps:
  - id: look
    run: cat marker.txt
    cwd: {subdir}
    timeout: 10
"""))
    assert spec.to_steps()[0].run() == "here"


# --------------------------------------------------------------------------- #
# systemd, cases 114 to 115
# --------------------------------------------------------------------------- #


def test_case_114_generates_valid_looking_units(tmp_path: Path) -> None:
    """Case 114: a weekly report needs a timer, not a hope."""
    builder = SystemdUnitBuilder(
        venv_python="/home/antoine/harness/.venv/bin/python3",
        harness_dir="/home/antoine/harness",
        workdir="/home/antoine/.harness",
        on_calendar="Mon 09:00")

    service, timer = builder.service(), builder.timer()

    assert "[Unit]" in service and "[Service]" in service
    assert "Type=oneshot" in service
    assert "/home/antoine/harness/.venv/bin/python3" in service, (
        "systemd has no PATH worth trusting, the interpreter must be absolute"
    )
    assert "[Timer]" in timer
    assert "OnCalendar=Mon 09:00" in timer
    assert "Persistent=true" in timer, "a missed run must catch up after a reboot"
    assert "WantedBy=timers.target" in timer

    written_service, written_timer = builder.write(tmp_path / "units")
    assert written_service.exists() and written_timer.exists()
    assert written_service.name == "harness-evidence.service"


def test_case_115_install_hint_is_complete() -> None:
    """Case 115: the hint must be runnable, not aspirational."""
    hint = SystemdUnitBuilder("/v/bin/python3", "/h", "/w").install_hint()
    for command in ("daemon-reload", "enable --now", "list-timers", "journalctl"):
        assert command in hint, f"missing {command}"
    assert "--user" in hint, "a user timer does not need root"


# --------------------------------------------------------------------------- #
# CLI, cases 116 to 121
# --------------------------------------------------------------------------- #


def test_case_116_check_exits_zero_on_a_clean_task(tmp_path: Path, capsys) -> None:
    """Case 116: `check` classifies without running or prompting."""
    task = write_task(tmp_path / "t.yaml", GOOD_TASK)
    code = harness_cli.main(["--workdir", str(tmp_path / "w"), "check", str(task)])
    out = capsys.readouterr().out

    assert code == harness_cli.EXIT_OK
    assert "executable" in out
    assert "echo hello" in out
    assert "0 question(s)" in out


def test_case_117_check_flags_a_vague_task(tmp_path: Path, capsys) -> None:
    """Case 117: a gate stopping a task has its own exit code."""
    task = write_task(tmp_path / "t.yaml", """
task_id: vague
request: tidy up the box
steps:
  - id: go
    run: echo hi
""")
    code = harness_cli.main(["--workdir", str(tmp_path / "w"), "check", str(task)])
    out = capsys.readouterr().out

    assert code == harness_cli.EXIT_BLOCKED
    assert "BLOCKED" in out
    assert "no_target" in out


def test_case_118_check_flags_a_dangerous_command(tmp_path: Path, capsys) -> None:
    """Case 118: `check` is the pre-flight for a task nobody has run yet."""
    task = write_task(tmp_path / "t.yaml", """
task_id: risky
request: deploy commit a1b2c3d to staging
steps:
  - id: wipe
    run: rm -rf /var/lib/postgresql
""")
    code = harness_cli.main(["--workdir", str(tmp_path / "w"), "check",
                             str(task), "-v"])
    out = capsys.readouterr().out

    assert code == harness_cli.EXIT_BLOCKED
    assert "BLOCK" in out
    assert "critical" in out
    assert "1 command(s) need confirmation" in out


def test_case_119_run_end_to_end_and_records_it(tmp_path: Path, capsys) -> None:
    """Case 119: the whole point of Phase 6, one command, real execution."""
    marker = tmp_path / "made-by-the-task"
    task = write_task(tmp_path / "t.yaml", f"""
task_id: cli-run
request: deploy commit a1b2c3d to staging
steps:
  - id: create
    run: touch {marker}
    estimated_seconds: 2
""")
    workdir = tmp_path / "w"
    code = harness_cli.main(["--workdir", str(workdir), "run", str(task),
                             "--non-interactive", "--offline"])
    out = capsys.readouterr().out

    assert code == harness_cli.EXIT_OK, out
    assert marker.exists(), "the task must actually have run"
    assert "status : completed" in out

    log = workdir / "telemetry" / "tasks.jsonl"
    assert log.exists(), "the run must have been recorded"
    entry = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert entry["request"] == "deploy commit a1b2c3d to staging"
    assert entry["status"] == "completed"


def test_case_120_non_interactive_refuses_rather_than_guesses(tmp_path: Path,
                                                              capsys) -> None:
    """Case 120: unattended runs default to refusing, which is the safe way."""
    marker = tmp_path / "should-not-exist"
    task = write_task(tmp_path / "t.yaml", f"""
task_id: cli-dangerous
request: deploy commit a1b2c3d to staging
steps:
  - id: wipe
    run: rm -rf {marker}
""")
    code = harness_cli.main(["--workdir", str(tmp_path / "w"), "run", str(task),
                             "--non-interactive", "--offline"])
    out = capsys.readouterr().out

    assert code == harness_cli.EXIT_BLOCKED, out
    assert "safety_cancelled" in out
    assert "refused (non-interactive)" in out


def test_case_121_doctor_and_bad_task_exit_codes(tmp_path: Path, capsys) -> None:
    """Case 121: exit codes are meaningful because this ends up in shell scripts."""
    assert harness_cli.main(["--workdir", str(tmp_path / "w"), "doctor"]) \
        == harness_cli.EXIT_OK
    assert "checks passed" in capsys.readouterr().out

    bad = write_task(tmp_path / "bad.yaml", "task_id: t\nsteps: []\n")
    assert harness_cli.main(["--workdir", str(tmp_path / "w"), "run", str(bad)]) \
        == harness_cli.EXIT_USAGE

    assert harness_cli.main(["--workdir", str(tmp_path / "w"), "evidence"]) \
        == harness_cli.EXIT_OK
    assert "HARNESS EVIDENCE REPORT" in capsys.readouterr().out
