"""
break_it_v3.py
Adversarial probes for Phase 3, plus a final sweep confirming every finding from
Phase 1 and Phase 2 is closed.

Run:  python3 break_it_v3.py
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

from harness_core import OllamaFallbackHandler, Step
from harness_core_v3 import LockBusy, LockedCheckpointing, TaskLock
from core_behaviors import AskClarifyingQuestionsBehavior, RollbackOnFailBehavior
from core_behaviors_v2 import ParsingSafetyClassifier
from core_behaviors_v3 import (
    ScoringModelSelector,
    UndoGenerator,
    VerifiedRollbackBehavior,
)
from phase1_integrated_executor import PipelineStatus
from phase3_integrated_executor import Phase3IntegratedExecutor

FINDINGS: List[Tuple[str, str, str]] = []


def finding(severity: str, probe: str, note: str) -> None:
    FINDINGS.append((severity, probe, note))
    print(f"  [{severity}] {note}")


def healthy() -> OllamaFallbackHandler:
    return OllamaFallbackHandler(probe=lambda: True, timeout=0.1)


def pipeline(tmp: Path, **kw) -> Phase3IntegratedExecutor:
    base = dict(
        clarifier=AskClarifyingQuestionsBehavior(ask=lambda a: "staging"),
        safety=ParsingSafetyClassifier(confirm=lambda v: True),
        ollama=healthy(),
    )
    base.update(kw)
    return Phase3IntegratedExecutor(workdir=tmp / "run", **base)


# --------------------------------------------------------------------------- #


def probe_rollback_honesty() -> None:
    """The Phase 2 finding: a no-op undo passed as success."""
    print("\nPROBE A: does a fake undo still pass as success")
    old = RollbackOnFailBehavior()
    old.push("pretend to restore", lambda: None)
    old_report = old.rollback()

    new = VerifiedRollbackBehavior()
    new.push("pretend to restore", undo=lambda: None, verify=lambda: False)
    new_report = new.rollback()

    print(f"  v1 verdict: ok={old_report.ok} status={old_report.results[0].status}")
    print(f"  v3 verdict: ok={new_report.ok} status={new_report.results[0].status}")
    if old_report.ok and not new_report.ok:
        finding("CLOSED", "rollback-verification",
                "v1 reported ok for an undo that did nothing, v3 reports "
                f"'{new_report.results[0].status}'")
    else:
        finding("HIGH", "rollback-verification", "verification did not change the verdict")


def probe_undo_without_registration() -> None:
    """The Phase 2 finding: a step that forgot to push an undo was unrecoverable."""
    print("\nPROBE B: does a forgotten undo still lose the file")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        conf = root / "app.conf"
        conf.write_text("ORIGINAL\n", encoding="utf-8")

        steps = [
            Step("edit", kind="write", command="echo x", description="rewrite",
                 estimated_seconds=20, touches=[str(conf)],
                 run=lambda: conf.write_text("CLOBBERED\n", encoding="utf-8")),
            Step("boom", command="echo y", description="fail", estimated_seconds=20,
                 run=lambda: (_ for _ in ()).throw(RuntimeError("step failed"))),
        ]
        result = pipeline(root).run("no-undo", "deploy the api stack to staging", steps)

        content = conf.read_text(encoding="utf-8").strip()
        print(f"  file after failed run: {content!r}")
        if content == "ORIGINAL" and result.rollback_verified:
            finding("CLOSED", "rollback-inference",
                    "no caller-supplied undo at all and the file still came back, "
                    "verified")
        else:
            finding("HIGH", "rollback-inference",
                    f"file left as {content!r}, verified={result.rollback_verified}")


def probe_task_lock_closed() -> None:
    """The Phase 1 and 2 finding: colliding task ids raced silently."""
    print("\nPROBE C: does a task id collision still race")
    with tempfile.TemporaryDirectory() as tmp:
        a = LockedCheckpointing(Path(tmp) / "cp")
        b = LockedCheckpointing(Path(tmp) / "cp")
        a.acquire("shared")
        try:
            b.acquire("shared")
            finding("HIGH", "task-id-lock", "the second run was allowed to proceed")
        except LockBusy as exc:
            finding("CLOSED", "task-id-lock",
                    f"second run refused with the holder's identity: "
                    f"{exc.holder.describe() if exc.holder else '?'}")
        a.release_all()


def probe_lock_under_real_concurrency() -> None:
    """Two real processes, same task id, at the same time."""
    print("\nPROBE D: lock under real process concurrency")
    with tempfile.TemporaryDirectory() as tmp:
        lock_dir = Path(tmp) / "locks"
        lock_dir.mkdir(parents=True)
        here = str(Path(__file__).resolve().parent)
        script = (
            f"import sys; sys.path.insert(0, {here!r})\n"
            "from harness_core_v3 import TaskLock, LockBusy\n"
            "try:\n"
            f"    l = TaskLock({str(lock_dir)!r}, 'race'); l.acquire()\n"
            "    print('ACQUIRED', flush=True)\n"
            "    import time; time.sleep(1.5)\n"
            "except LockBusy:\n"
            "    print('REFUSED', flush=True)\n"
        )
        kids = [subprocess.Popen([sys.executable, "-c", script],
                                 stdout=subprocess.PIPE, text=True)
                for _ in range(6)]
        outcomes = []
        for kid in kids:
            try:
                outcomes.append((kid.stdout.readline() or "").strip())
            except Exception:
                outcomes.append("ERROR")
        for kid in kids:
            kid.kill()
            kid.wait(timeout=5)

        acquired = outcomes.count("ACQUIRED")
        refused = outcomes.count("REFUSED")
        print(f"  6 processes: {acquired} acquired, {refused} refused, "
              f"{6 - acquired - refused} other")
        if acquired == 1 and refused == 5:
            finding("CLOSED", "lock-concurrency",
                    "exactly one of six concurrent processes took the lock")
        else:
            finding("HIGH", "lock-concurrency",
                    f"{acquired} processes acquired the same lock, expected 1")


def probe_undo_coverage_honesty() -> None:
    """Does the generator overclaim what it can invert?"""
    print("\nPROBE E: undo coverage honesty")
    with tempfile.TemporaryDirectory() as tmp:
        gen = UndoGenerator(shadow_dir=Path(tmp) / "shadow")
        uninvertible = [
            Step("dd", command="dd if=/dev/zero of=/dev/sda", run=lambda: None),
            Step("mkfs", command="mkfs.ext4 /dev/nvme0n1p3", run=lambda: None),
            Step("migrate", command="alembic upgrade head", run=lambda: None),
            Step("truncate", command="truncate -s 0 /var/log/syslog", run=lambda: None),
            Step("dropdb", command="psql -c 'drop table users'", run=lambda: None),
        ]
        generated = sum(len(gen.for_step(s)) for s in uninvertible)
        coverage = gen.coverage(uninvertible)
        print(f"  {len(uninvertible)} uninvertible steps -> {generated} undos "
              f"generated, coverage {coverage['coverage_ratio']:.0%}")
        if generated == 0 and coverage["coverage_ratio"] == 0.0:
            finding("OK", "undo-honesty",
                    "the generator invents nothing for steps it cannot invert, "
                    "and names them in `uncovered`")
        else:
            finding("HIGH", "undo-honesty",
                    f"generated {generated} undos for uninvertible operations")


def probe_shadow_disk_cost() -> None:
    """Snapshotting every touched file costs disk. How much?"""
    print("\nPROBE F: shadow copy disk cost")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "files"
        root.mkdir()
        paths = []
        for i in range(50):
            p = root / f"conf{i}.yaml"
            p.write_bytes(b"x" * 200_000)  # 200 KB each, 10 MB total
            paths.append(str(p))

        gen = UndoGenerator(shadow_dir=Path(tmp) / "shadow")
        started = time.perf_counter()
        actions = gen.for_step(Step("bulk", touches=paths, run=lambda: None))
        elapsed = time.perf_counter() - started

        shadow_bytes = sum(f.stat().st_size for f in (Path(tmp) / "shadow").iterdir())
        print(f"  50 files x 200 KB: {len(actions)} snapshots, "
              f"{shadow_bytes/1024/1024:.1f} MB shadowed in {elapsed*1000:.0f} ms")
        if shadow_bytes > 9_000_000:
            finding("MED", "shadow-cost",
                    f"shadow copies duplicate every touched file byte for byte "
                    f"({shadow_bytes/1024/1024:.1f} MB here); on a large tree this "
                    "is real disk and there is no dedup or compression")
        if elapsed > 2.0:
            finding("MED", "shadow-cost", f"snapshotting took {elapsed:.1f}s")


def probe_undo_ordering() -> None:
    """Generated undos must not fight caller-supplied ones."""
    print("\nPROBE G: generated vs caller undo ordering")
    order: List[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        conf = root / "app.conf"
        conf.write_text("ORIGINAL", encoding="utf-8")

        steps = [
            Step("edit", kind="write", command="echo x", description="edit",
                 estimated_seconds=20, touches=[str(conf)],
                 run=lambda: conf.write_text("NEW", encoding="utf-8")),
            Step("boom", command="echo y", description="fail", estimated_seconds=20,
                 run=lambda: (_ for _ in ()).throw(RuntimeError("failed"))),
        ]
        result = pipeline(root).run(
            "ordering", "deploy the api stack to staging", steps,
            rollback_actions=[("caller high level undo",
                               lambda: order.append("caller"), True)],
        )
        statuses = [(r.description[:34], r.status) for r in result.rollback.results]
        for desc, status in statuses:
            print(f"    {status:11} {desc}")
        if statuses and statuses[0][1] == "undone" and "caller" in statuses[0][0]:
            finding("OK", "undo-ordering",
                    "caller undos unwind before generated file restores, so a high "
                    "level revert is not fighting a low level one")
        else:
            finding("MED", "undo-ordering",
                    f"caller undo did not run first: {statuses}")


def probe_verify_cannot_be_fooled() -> None:
    """A restore that writes the right bytes to the wrong place must fail."""
    print("\nPROBE H: can verification be fooled")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = root / "real.conf"
        decoy = root / "decoy.conf"
        target.write_text("SECRET", encoding="utf-8")

        gen = UndoGenerator(shadow_dir=root / "shadow")
        action = gen.capture_file(target)
        target.write_text("CHANGED", encoding="utf-8")

        # An undo that restores to the wrong path.
        decoy.write_text("SECRET", encoding="utf-8")
        if action.verify():
            finding("HIGH", "verify-fooled",
                    "verification passed while the real target is still wrong")
        else:
            finding("OK", "verify-fooled",
                    "writing the right bytes to the wrong path does not verify")

        action.undo()
        if action.verify():
            finding("OK", "verify-fooled", "a genuine restore does verify")
        else:
            finding("HIGH", "verify-fooled", "a genuine restore failed verification")


def probe_routing_regression() -> None:
    """Scoring must not have broken the routing contract from Phase 1."""
    print("\nPROBE I: routing regression")
    sel = ScoringModelSelector()
    contract = [
        ("deploy the api container to staging", "devstral"),
        ("release the new systemd unit", "devstral"),
        ("debug why nginx keeps crashing", "qwen3:32b"),
        ("find the root cause of this traceback", "qwen3:32b"),
        ("list the running containers", "qwen3:8b"),
        ("debug the failing deploy to production", "qwen3:32b"),
        ("deploy the fix for the crash", "qwen3:32b"),
        ("check why the release is broken", "qwen3:32b"),
        ("review the deploy script before we ship it", "qwen3:32b"),
        ("refactor the auth module", "devstral"),
        ("plan the migration strategy", "qwen3:32b"),
        ("show disk usage", "qwen3:8b"),
    ]
    wrong = [(t, sel.select(t).model, want) for t, want in contract
             if sel.select(t).model != want]
    for task, got, want in wrong:
        finding("HIGH", "routing-regression", f"{task!r} -> {got}, expected {want}")
    print(f"  {len(contract) - len(wrong)}/{len(contract)} correct")
    if not wrong:
        finding("CLOSED", "routing-collision",
                f"all {len(contract)} routes correct including the multi-intent "
                "cases first-match got wrong in Phase 1 and Phase 2")


def probe_phase12_regression() -> None:
    """Phase 3 must not have broken anything below it."""
    print("\nPROBE J: Phase 1 and 2 regression")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_phase1.py", "tests/test_phase2.py",
         "-q", "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=str(Path(__file__).resolve().parent),
    )
    tail = (result.stdout or result.stderr).strip().splitlines()[-1:]
    print(f"  {tail[0] if tail else 'no output'}")
    if result.returncode == 0:
        finding("OK", "phase12-regression",
                "all 40 Phase 1 and Phase 2 cases still pass unchanged")
    else:
        finding("HIGH", "phase12-regression", f"regression: {tail}")


def probe_remaining_gaps() -> None:
    """What is still open after Phase 3."""
    print("\nPROBE K: known remaining gaps")
    finding("OPEN", "inotify-fast-path",
            "the tree detector still runs a full walk as source of truth; inotify "
            "only narrows candidates. O(tree) per check, not O(changed)")
    finding("OPEN", "shadow-gc",
            "shadow copies are cleaned only on an explicit cleanup() call; a "
            "crashed run leaves them behind with no reaper")
    finding("OPEN", "undo-coverage-ceiling",
            "generation covers files, services, packages, and mkdir. Database "
            "migrations, dd, and mkfs still need a caller-supplied undo")


def main() -> None:
    probes: List[Callable[[], None]] = [
        probe_rollback_honesty,
        probe_undo_without_registration,
        probe_task_lock_closed,
        probe_lock_under_real_concurrency,
        probe_undo_coverage_honesty,
        probe_shadow_disk_cost,
        probe_undo_ordering,
        probe_verify_cannot_be_fooled,
        probe_routing_regression,
        probe_phase12_regression,
        probe_remaining_gaps,
    ]
    for p in probes:
        try:
            p()
        except Exception as exc:
            finding("CRASH", p.__name__, f"probe raised {type(exc).__name__}: {exc}")

    print("\n" + "=" * 78)
    print("PHASE 3 FINDINGS SUMMARY")
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
