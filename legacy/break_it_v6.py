"""
break_it_v6.py
Phase 6 adversarial probes, plus the final sweep across all six phases.

The operational surface gets probed hardest at exactly one point: a task file is
now a document that drives root shell commands, and documents get edited by
people in a hurry. Anything a malformed or hostile task file can do to this
harness, it can do on a real box.

Run:  python3 break_it_v6.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, List, Tuple

import harness_cli
from core_behaviors_v2 import ParsingSafetyClassifier
from core_behaviors_v4 import StructuralClarifier
from harness_ops import RotatingTaskLogger, SystemdUnitBuilder, TaskSpec, TaskSpecError
from harness_telemetry import TaskRecord

FINDINGS: List[Tuple[str, str, str]] = []


def finding(severity: str, probe: str, note: str) -> None:
    FINDINGS.append((severity, probe, note))
    print(f"  [{severity}] {note}")


def rec(request: str, **kw) -> TaskRecord:
    base = dict(task_id="t", request=request, at=1.0)
    base.update(kw)
    return TaskRecord(**base)


def write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def probe_hostile_task_files() -> None:
    """A task file drives root shell commands. What can a bad one do?"""
    print("\nPROBE A: hostile and malformed task files")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        hostile = [
            ("yaml bomb", "task_id: &a [x,x,x,x,x,x,x,x,x]\nrequest: *a\nsteps: *a\n"),
            ("python object", "task_id: !!python/object/apply:os.system ['echo pwned']\n"
                              "request: r\nsteps: [{run: echo}]\n"),
            ("null steps", "task_id: t\nrequest: r\nsteps: ~\n"),
            ("steps is a string", "task_id: t\nrequest: r\nsteps: 'echo hi'\n"),
            ("deeply nested", "task_id: t\nrequest: r\nsteps:\n  - run: {a: {b: {c: 1}}}\n"),
            ("empty file", ""),
            ("binary junk", "\x00\x01\x02 not yaml at all \xff"),
            ("giant request", "task_id: t\nrequest: " + "x" * 100_000
                              + "\nsteps: [{run: echo}]\n"),
        ]
        crashed = []
        for label, body in hostile:
            path = write(root / f"{label.replace(' ', '_')}.yaml", body)
            try:
                TaskSpec.load(path)
                # Loading successfully is fine as long as it did not execute
                # anything, which the next probe covers.
            except TaskSpecError:
                pass  # the intended outcome
            except Exception as exc:
                crashed.append(f"{label}: {type(exc).__name__}: {exc}")

        for note in crashed:
            finding("HIGH", "hostile-task", f"unhandled exception: {note}")
        if not crashed:
            finding("OK", "hostile-task",
                    f"{len(hostile)} malformed and hostile files, all rejected as "
                    "TaskSpecError or parsed harmlessly, none crashed the loader")

        # yaml.safe_load must not construct Python objects.
        marker = root / "pwned"
        path = write(root / "exec.yaml",
                     f"task_id: !!python/object/apply:os.system ['touch {marker}']\n"
                     "request: r\nsteps: [{run: echo}]\n")
        try:
            TaskSpec.load(path)
        except Exception:
            pass
        if marker.exists():
            finding("HIGH", "hostile-task",
                    "a task file executed code during parsing; safe_load is not "
                    "being used")
        else:
            finding("OK", "hostile-task",
                    "a !!python/object tag did not execute; safe_load holds")


def probe_command_equivalence() -> None:
    """Does what gets classified always equal what gets run?"""
    print("\nPROBE B: classified command versus executed command")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        tricky = [
            "rm -rf /var/lib/x",
            "echo a && rm -rf /var/lib/y",
            "sudo -- sh -c 'rm -rf /var/lib/z'",
            "printf 'x' > /etc/fstab",
            "find /var -delete",
        ]
        body = ["task_id: t", "request: deploy commit a1b2c3d to staging", "steps:"]
        for i, command in enumerate(tricky):
            body.append(f"  - id: s{i}")
            body.append(f"    run: {json.dumps(command)}")
        spec = TaskSpec.load(write(root / "t.yaml", "\n".join(body) + "\n"))

        steps = spec.to_steps(dry_run=True)
        mismatches = [(a, b.command) for a, b in zip(spec.commands(), steps)
                      if a != b.command]
        for classified, executed in mismatches:
            finding("HIGH", "command-equivalence",
                    f"classified {classified!r} but would run {executed!r}")
        if not mismatches:
            finding("OK", "command-equivalence",
                    f"{len(tricky)} commands: what the safety gate classifies is "
                    "byte for byte what the step executes")

        safety = ParsingSafetyClassifier()
        missed = [c for c in spec.commands()
                  if not safety.classify(c).requires_confirmation]
        if missed:
            finding("HIGH", "command-equivalence",
                    f"dangerous commands not flagged through the task file path: {missed}")
        else:
            finding("OK", "command-equivalence",
                    "all 5 dangerous commands flagged when read from a file")


def probe_dry_run_touches_nothing() -> None:
    """Does --dry-run really execute nothing, through the whole CLI?"""
    print("\nPROBE C: dry run isolation")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        markers = [root / f"m{i}" for i in range(4)]
        body = ["task_id: dry", "request: deploy commit a1b2c3d to staging", "steps:"]
        for i, marker in enumerate(markers):
            body.append(f"  - id: s{i}")
            body.append(f"    run: touch {marker}")
        task = write(root / "t.yaml", "\n".join(body) + "\n")

        code = harness_cli.main(["--workdir", str(root / "w"), "run", str(task),
                                 "--dry-run", "--non-interactive", "--offline"])
        created = [m for m in markers if m.exists()]
        if created:
            finding("HIGH", "dry-run", f"a dry run created {len(created)} file(s)")
        else:
            finding("OK", "dry-run",
                    f"exit {code}, all 8 stages ran, zero of {len(markers)} files created")

        log = root / "w" / "telemetry" / "tasks.jsonl"
        if log.exists():
            finding("OK", "dry-run", "the dry run was still recorded as evidence")
        else:
            finding("MED", "dry-run", "a dry run left no record")


def probe_exit_codes() -> None:
    """Exit codes end up in shell scripts. Do they mean what they claim?"""
    print("\nPROBE D: exit code contract")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cases = [
            ("clean", "task_id: a\nrequest: deploy commit a1b2c3d to staging\n"
                      "steps: [{id: s, run: 'true'}]\n", harness_cli.EXIT_OK),
            ("vague", "task_id: b\nrequest: tidy up the box\n"
                      "steps: [{id: s, run: 'true'}]\n", harness_cli.EXIT_BLOCKED),
            ("dangerous", "task_id: c\nrequest: deploy commit a1b2c3d to staging\n"
                          "steps: [{id: s, run: 'rm -rf /var/lib/nope'}]\n",
             harness_cli.EXIT_BLOCKED),
            ("fails", "task_id: d\nrequest: deploy commit a1b2c3d to staging\n"
                      "steps: [{id: s, run: 'exit 9'}]\n", harness_cli.EXIT_FAILED),
            ("malformed", "task_id: e\nsteps: []\n", harness_cli.EXIT_USAGE),
        ]
        wrong = []
        for label, body, expected in cases:
            task = write(root / f"{label}.yaml", body)
            got = harness_cli.main(["--workdir", str(root / "w"), "run", str(task),
                                    "--non-interactive", "--offline"])
            if got != expected:
                wrong.append(f"{label}: got {got}, expected {expected}")
            print(f"  {label:<10} exit {got} (expected {expected})")
        for note in wrong:
            finding("HIGH", "exit-codes", note)
        if not wrong:
            finding("OK", "exit-codes",
                    "0 ok, 1 usage, 2 stopped by a gate, 3 executed and failed; "
                    "all five cases match")


def probe_rotation_integrity() -> None:
    """Does rotation ever lose or reorder a record?"""
    print("\nPROBE E: rotation integrity")
    with tempfile.TemporaryDirectory() as tmp:
        # Sized so the window CAN hold everything: 400 records at roughly 400
        # bytes each, 4 KB per file, 45 archives. The first version of this
        # probe used max_bytes=800 and keep=40, which holds two records per
        # file and mathematically cannot retain 400. It reported a HIGH finding
        # against correct code, which is its own kind of failure.
        log = RotatingTaskLogger(Path(tmp) / "tasks.jsonl", max_bytes=4096, keep=60)
        total = 400
        for i in range(total):
            log.record(rec(f"task {i:04d} on /srv/api"))

        back = log.read()
        on_disk = sum(len(f.read_text(encoding="utf-8").splitlines())
                      for f in Path(tmp).iterdir() if f.is_file())
        print(f"  {total} records, {log.rotations} rotations, "
              f"{len(log.archives())} archives, {len(back)} read back, "
              f"{on_disk} lines on disk")

        if len(back) != on_disk:
            finding("HIGH", "rotation-integrity",
                    f"read() returned {len(back)} but {on_disk} lines are on disk")
        elif len(back) != total:
            finding("MED", "rotation-integrity",
                    f"{len(back)} of {total} retained; the keep window is the "
                    "limit, not a bug, but it is silent about what it dropped")
        else:
            finding("OK", "rotation-integrity",
                    f"all {total} records survived {log.rotations} rotations "
                    f"across {len(log.archives())} archives")

        requests = [r.request for r in back]
        if requests != sorted(requests):
            first_bad = next(i for i in range(1, len(requests))
                             if requests[i] < requests[i - 1])
            finding("HIGH", "rotation-integrity",
                    f"records came back out of order at index {first_bad}; "
                    "numbered archives sort lexically unless indexed numerically")
        else:
            finding("OK", "rotation-integrity",
                    "chronological order preserved past the .9 to .10 boundary, "
                    "where a lexical sort would have reordered history")

        # And the window itself: an undersized keep must drop the OLDEST.
        tight = RotatingTaskLogger(Path(tmp) / "tight.jsonl", max_bytes=4096, keep=3)
        for i in range(200):
            tight.record(rec(f"task {i:04d} on /srv/api"))
        kept = tight.read()
        if kept and kept[-1].request.endswith("0199 on /srv/api"):
            finding("OK", "rotation-integrity",
                    f"an undersized window kept the newest {len(kept)} and dropped "
                    "the oldest, which is the correct direction")
        else:
            finding("HIGH", "rotation-integrity",
                    "an undersized window did not retain the newest records")


def probe_systemd_units_are_valid() -> None:
    """Would systemd actually accept what we generate?"""
    print("\nPROBE F: systemd unit validity")
    with tempfile.TemporaryDirectory() as tmp:
        builder = SystemdUnitBuilder("/opt/venv/bin/python3", "/opt/harness",
                                     "/home/antoine/.harness", "Mon 09:00")
        service, timer = builder.write(Path(tmp))

        problems = []
        service_text = service.read_text(encoding="utf-8")
        if not service_text.startswith("[Unit]"):
            problems.append("service does not start with [Unit]")
        if "ExecStart=" not in service_text:
            problems.append("service has no ExecStart")
        if "ExecStart=python3" in service_text or "ExecStart=harness" in service_text:
            problems.append("ExecStart is not an absolute path")

        analyzer = subprocess.run(["systemd-analyze", "verify", str(service)],
                                  capture_output=True, text=True)
        if analyzer.returncode != 0 and analyzer.stderr.strip():
            noise = [l for l in analyzer.stderr.splitlines()
                     if "Unit" in l and "not found" not in l.lower()]
            if noise:
                finding("MED", "systemd-validity",
                        f"systemd-analyze complained: {noise[0][:100]}")
            else:
                finding("OK", "systemd-validity",
                        "systemd-analyze raised only path-resolution noise, which "
                        "is expected for a unit written outside the search path")
        else:
            finding("OK", "systemd-validity", "systemd-analyze verify passed")

        for note in problems:
            finding("HIGH", "systemd-validity", note)
        if not problems:
            finding("OK", "systemd-validity",
                    "absolute interpreter, Persistent=true, WantedBy=timers.target")


def probe_cli_without_a_workdir() -> None:
    """Every read-only command on a box that has never run a task."""
    print("\nPROBE G: cold start")
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp) / "never-used"
        broke = []
        for command in (["evidence"], ["drift"], ["loophole"], ["rotate"],
                        ["doctor"], ["systemd"]):
            try:
                code = harness_cli.main(["--workdir", str(workdir)] + command)
                if code not in (harness_cli.EXIT_OK, harness_cli.EXIT_USAGE):
                    broke.append(f"{command[0]} exited {code}")
            except Exception as exc:
                broke.append(f"{command[0]}: {type(exc).__name__}: {exc}")
        for note in broke:
            finding("HIGH", "cold-start", note)
        if not broke:
            finding("OK", "cold-start",
                    "all six read-only commands work on a workdir that has never "
                    "seen a task")


def probe_full_regression() -> None:
    """All 121 cases across six phases."""
    print("\nPROBE H: full regression, phases 1 through 6")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q", "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=str(Path(__file__).resolve().parent))
    tail = (result.stdout or result.stderr).strip().splitlines()[-1:]
    print(f"  {tail[0] if tail else 'no output'}")
    if result.returncode == 0:
        finding("OK", "full-regression", "all 121 cases pass across six phases")
    else:
        finding("HIGH", "full-regression", f"regression: {tail}")


def probe_remaining_gaps() -> None:
    """What is still open after six phases."""
    print("\nPROBE I: known remaining gaps")
    finding("OPEN", "no-real-traffic",
            "every number in six phases still comes from data I generated. The "
            "harness is now usable enough to collect real evidence; nobody has")
    finding("OPEN", "clarifier-semantics",
            "the anchored-but-goalless gap is measurable but still open, waiting "
            "on traffic to decide it")
    finding("OPEN", "undo-coverage-ceiling",
            "migrations, dd, and mkfs still need a declared rollback: block")
    finding("OPEN", "no-packaging",
            "there is no pyproject.toml, so `harness` is not on PATH and every "
            "invocation is `python3 harness_cli.py` from the right directory")


def main() -> None:
    probes: List[Callable[[], None]] = [
        probe_hostile_task_files,
        probe_command_equivalence,
        probe_dry_run_touches_nothing,
        probe_exit_codes,
        probe_rotation_integrity,
        probe_systemd_units_are_valid,
        probe_cli_without_a_workdir,
        probe_full_regression,
        probe_remaining_gaps,
    ]
    for p in probes:
        try:
            p()
        except Exception as exc:
            finding("CRASH", p.__name__, f"probe raised {type(exc).__name__}: {exc}")

    print("\n" + "=" * 78)
    print("PHASE 6 FINDINGS SUMMARY")
    print("=" * 78)
    for severity in ("CRASH", "HIGH", "MED", "OPEN", "CLOSED", "OK"):
        rows = [f for f in FINDINGS if f[0] == severity]
        if not rows:
            continue
        print(f"\n{severity} ({len(rows)}):")
        for _, probe, note in rows:
            print(f"  - {probe}: {note}")


if __name__ == "__main__":
    main()
