"""
test_phase3.py
Acceptance cases for the three Phase 3 subsystems.

  P0-1  automatic undo generation   cases 41 to 46
  P0-2  verified rollback           cases 47 to 51
  P0-3  task locking                cases 52 to 55
  P1-1  scoring model routing       cases 56 to 58
  integration                       cases 59 to 61
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness_core import OllamaFallbackHandler, Step  # noqa: E402
from harness_core_v3 import LockBusy, LockedCheckpointing, TaskLock  # noqa: E402
from core_behaviors import AskClarifyingQuestionsBehavior  # noqa: E402
from core_behaviors_v2 import ParsingSafetyClassifier  # noqa: E402
from core_behaviors_v3 import (  # noqa: E402
    ScoringModelSelector,
    SnapshotKind,
    UndoGenerator,
    VerifiedRollbackBehavior,
)
from phase1_integrated_executor import PipelineStatus, Stage  # noqa: E402
from phase3_integrated_executor import Phase3IntegratedExecutor  # noqa: E402


def healthy() -> OllamaFallbackHandler:
    return OllamaFallbackHandler(probe=lambda: True, timeout=0.1)


# --------------------------------------------------------------------------- #
# P0-1: automatic undo generation, cases 41 to 46
# --------------------------------------------------------------------------- #


def test_case_41_file_edit_is_restored_byte_for_byte(tmp_path: Path) -> None:
    """Case 41: a file the step touches is restored exactly, mode included."""
    target = tmp_path / "nginx.conf"
    target.write_text("original contents\n", encoding="utf-8")
    os.chmod(target, 0o640)
    before_mode = target.stat().st_mode & 0o7777

    gen = UndoGenerator(shadow_dir=tmp_path / "shadow")
    step = Step("edit", kind="write", touches=[str(target)], run=lambda: None)
    actions = gen.for_step(step)

    assert len(actions) == 1
    assert actions[0].generated is True
    assert actions[0].snapshot.kind is SnapshotKind.FILE_CONTENT

    target.write_text("CLOBBERED BY THE STEP\n", encoding="utf-8")
    os.chmod(target, 0o777)

    actions[0].undo()

    assert target.read_text(encoding="utf-8") == "original contents\n"
    assert target.stat().st_mode & 0o7777 == before_mode
    assert actions[0].verify() is True


def test_case_42_created_file_is_deleted_on_undo(tmp_path: Path) -> None:
    """Case 42: a file that did not exist before is removed, not restored."""
    target = tmp_path / "brand_new.conf"
    assert not target.exists()

    gen = UndoGenerator(shadow_dir=tmp_path / "shadow")
    actions = gen.for_step(Step("create", touches=[str(target)], run=lambda: None))

    assert actions[0].snapshot.kind is SnapshotKind.FILE_ABSENT
    target.write_text("the step made this", encoding="utf-8")

    actions[0].undo()
    assert not target.exists()
    assert actions[0].verify() is True


def test_case_43_service_state_is_captured_and_restored() -> None:
    """Case 43: a systemctl command gets its prior unit state snapshotted."""
    state = {"api.service": "active", "worker.service": "inactive"}
    calls: list[list[str]] = []

    gen = UndoGenerator(service_probe=lambda unit: state.get(unit, "unknown"))
    step = Step("restart", command="sudo systemctl restart api.service && "
                                  "systemctl start worker.service",
                run=lambda: None)
    actions = gen.for_step(step)

    assert len(actions) == 2
    assert {a.snapshot.target for a in actions} == {"api.service", "worker.service"}
    assert actions[0].snapshot.payload == "active"
    assert actions[1].snapshot.payload == "inactive"
    assert "restore api.service to active" in actions[0].description
    assert "restore worker.service to inactive" in actions[1].description

    # Verification reads current state, so a drifted unit fails verification.
    assert actions[0].verify() is True
    state["api.service"] = "failed"
    assert actions[0].verify() is False


def test_case_44_package_install_undo_is_marked_unsafe() -> None:
    """Case 44: removing packages can cascade, so it asks before undoing."""
    installed = {"curl", "git"}
    gen = UndoGenerator(package_probe=lambda: set(installed))

    actions = gen.for_step(Step("install", command="sudo apt install -y nginx redis",
                                run=lambda: None))
    assert len(actions) == 1
    assert actions[0].safe is False, "package removal must require confirmation"
    assert actions[0].snapshot.kind is SnapshotKind.PACKAGE_SET

    installed.add("nginx")
    assert actions[0].verify() is False, "extra packages mean not yet reverted"

    # A removal command cannot be auto-inverted, and says so.
    gen2 = UndoGenerator(package_probe=lambda: set(installed))
    assert gen2.for_step(Step("purge", command="apt purge nginx", run=lambda: None)) == []
    assert any("cannot be auto-inverted" in s for s in gen2.skipped)


def test_case_45_mkdir_undo_refuses_to_delete_a_filled_directory(tmp_path: Path) -> None:
    """Case 45: removing a directory someone filled is a second disaster."""
    target = tmp_path / "newdir"
    gen = UndoGenerator(shadow_dir=tmp_path / "shadow")
    actions = gen.for_step(Step("make", command=f"mkdir -p {target}", run=lambda: None))

    assert len(actions) == 1
    assert actions[0].snapshot.kind is SnapshotKind.DIR_ABSENT

    target.mkdir()
    (target / "someone_elses_file.txt").write_text("important", encoding="utf-8")

    actions[0].undo()
    assert target.exists(), "must not delete a directory that has contents"
    assert (target / "someone_elses_file.txt").exists()
    assert actions[0].verify() is True

    (target / "someone_elses_file.txt").unlink()
    actions[0].undo()
    assert not target.exists(), "an empty directory is safe to remove"


def test_case_46_coverage_reports_what_it_could_not_invert(tmp_path: Path) -> None:
    """Case 46: steps with no derivable undo are named before the run, not after."""
    gen = UndoGenerator(shadow_dir=tmp_path / "shadow")
    conf = tmp_path / "a.conf"
    conf.write_text("x", encoding="utf-8")

    steps = [
        Step("edit", touches=[str(conf)], run=lambda: None),
        Step("svc", command="systemctl restart api", run=lambda: None),
        Step("migrate", command="alembic upgrade head", run=lambda: None),
        Step("burn", command="dd if=/dev/zero of=/dev/sda", run=lambda: None),
    ]
    for step in steps:
        gen.for_step(step)

    coverage = gen.coverage(steps)
    assert coverage["covered"] == ["edit", "svc"]
    assert coverage["uncovered"] == ["migrate", "burn"]
    assert coverage["coverage_ratio"] == 0.5

    # A file too large to shadow honestly is skipped and reported.
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 5000)
    small_gen = UndoGenerator(shadow_dir=tmp_path / "shadow2", max_shadow_bytes=100)
    assert small_gen.for_step(Step("big", touches=[str(big)], run=lambda: None)) == []
    assert any("over max_shadow_bytes" in s for s in small_gen.skipped)


# --------------------------------------------------------------------------- #
# P0-2: verified rollback, cases 47 to 51
# --------------------------------------------------------------------------- #


def test_case_47_undo_that_did_not_work_is_not_reported_as_undone() -> None:
    """Case 47: the finding. A no-op undo used to pass as success."""
    rb = VerifiedRollbackBehavior()
    reverted = {"done": False}

    rb.push("pretend to restore the config",
            undo=lambda: None,                    # does nothing
            verify=lambda: reverted["done"])      # and the verifier knows

    report = rb.rollback()

    assert report.ok is False, "Phase 1 and 2 both reported ok here"
    assert report.results[0].status == "unverified"
    assert report.results[0].verified is False
    assert "not back to its prior state" in report.results[0].error


def test_case_48_verified_undo_is_reported_as_undone(tmp_path: Path) -> None:
    """Case 48: a real restore verifies and passes."""
    target = tmp_path / "conf"
    target.write_text("good", encoding="utf-8")
    gen = UndoGenerator(shadow_dir=tmp_path / "shadow")
    action = gen.capture_file(target)
    target.write_text("bad", encoding="utf-8")

    rb = VerifiedRollbackBehavior()
    rb.push_action(action)
    report = rb.rollback()

    assert report.ok is True
    assert report.results[0].status == "undone"
    assert report.results[0].verified is True
    assert target.read_text(encoding="utf-8") == "good"


def test_case_49_unverified_undo_halts_the_sweep() -> None:
    """Case 49: unwinding past a step that did not revert is not recovery."""
    order: list[str] = []
    rb = VerifiedRollbackBehavior(stop_on_unverified=True)

    rb.push("bottom", undo=lambda: order.append("bottom"), verify=lambda: True)
    rb.push("middle (broken)", undo=lambda: order.append("middle"), verify=lambda: False)
    rb.push("top", undo=lambda: order.append("top"), verify=lambda: True)

    report = rb.rollback()

    assert order == ["top", "middle"], "bottom must not be touched"
    assert report.ok is False
    assert [r.status for r in report.results] == ["undone", "unverified"]
    assert rb.depth == 1, "the un-unwound remainder stays for the operator"

    # Opting out lets the sweep continue, which is sometimes what you want.
    order.clear()
    loose = VerifiedRollbackBehavior(stop_on_unverified=False)
    loose.push("bottom", undo=lambda: order.append("bottom"), verify=lambda: True)
    loose.push("middle", undo=lambda: order.append("middle"), verify=lambda: False)
    loose.rollback()
    assert order == ["middle", "bottom"]


def test_case_50_missing_verifier_is_honest_about_it() -> None:
    """Case 50: no verifier means verified=None, not verified=True."""
    rb = VerifiedRollbackBehavior()
    rb.push("legacy undo with no verifier", undo=lambda: None)

    report = rb.rollback()

    assert report.results[0].status == "undone"
    assert report.results[0].verified is None
    assert "nothing was checked" in report.results[0].verify_error
    assert report.ok is True, "an unverified-by-omission undo still counts as ok"


def test_case_51_a_raising_verifier_counts_as_unverified() -> None:
    """Case 51: a verifier that blows up must not be read as success."""
    rb = VerifiedRollbackBehavior(stop_on_unverified=False)
    rb.push("restore", undo=lambda: None,
            verify=lambda: (_ for _ in ()).throw(OSError("cannot stat")))

    report = rb.rollback()

    assert report.ok is False
    assert report.results[0].status == "unverified"
    assert "OSError" in report.results[0].verify_error

    # LIFO ordering from Phase 1 is preserved.
    order: list[str] = []
    rb2 = VerifiedRollbackBehavior()
    for name in ("first", "second", "third"):
        rb2.push(name, undo=lambda n=name: order.append(n), verify=lambda: True)
    rb2.rollback()
    assert order == ["third", "second", "first"]


# --------------------------------------------------------------------------- #
# P0-3: task locking, cases 52 to 55
# --------------------------------------------------------------------------- #


def test_case_52_second_holder_is_refused_with_an_identity(tmp_path: Path) -> None:
    """Case 52: the finding. A colliding task id fails fast and names the holder."""
    first = TaskLock(tmp_path / "locks", "deploy-1")
    first.acquire()
    assert first.held

    second = TaskLock(tmp_path / "locks", "deploy-1")
    with pytest.raises(LockBusy) as caught:
        second.acquire()

    assert caught.value.holder is not None
    assert caught.value.holder.pid == os.getpid()
    assert "already running" in str(caught.value)
    assert "pid" in str(caught.value)

    first.release()
    second.acquire()  # now free
    assert second.held
    second.release()


def test_case_53_lock_releases_when_the_holder_dies(tmp_path: Path) -> None:
    """Case 53: a crashed run must not wedge a task id forever."""
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir(parents=True)
    script = (
        "import fcntl,sys,time,os\n"
        f"fh=open({str(lock_dir / 'ghost.lock')!r},'a+')\n"
        "fcntl.flock(fh.fileno(), fcntl.LOCK_EX)\n"
        "print('locked', flush=True)\n"
        "time.sleep(30)\n"
    )
    child = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE,
                             text=True)
    assert child.stdout.readline().strip() == "locked"

    blocked = TaskLock(lock_dir, "ghost")
    assert blocked.is_locked() is True

    child.kill()
    child.wait(timeout=5)
    time.sleep(0.1)

    assert blocked.is_locked() is False, "the kernel must have released it"
    blocked.acquire()
    assert blocked.held
    blocked.release()


def test_case_54_blocking_mode_waits_then_times_out(tmp_path: Path) -> None:
    """Case 54: block=True waits, and gives up cleanly at the timeout."""
    holder = TaskLock(tmp_path / "locks", "busy")
    holder.acquire()

    waiter = TaskLock(tmp_path / "locks", "busy", block=True, timeout=0.2,
                      poll_interval=0.02)
    started = time.perf_counter()
    with pytest.raises(LockBusy):
        waiter.acquire()
    elapsed = time.perf_counter() - started

    assert 0.15 < elapsed < 1.0, f"waited {elapsed:.3f}s, expected about 0.2s"
    holder.release()


def test_case_55_locked_checkpointing_refuses_a_concurrent_run(tmp_path: Path) -> None:
    """Case 55: two checkpointers on one task id, second is refused not raced."""
    a = LockedCheckpointing(tmp_path / "cp")
    b = LockedCheckpointing(tmp_path / "cp")

    a.acquire("shared-id")
    with pytest.raises(LockBusy):
        b.acquire("shared-id")

    assert b.contentions, "contention must be recorded"
    assert b.errors, "and surfaced in errors, not swallowed"
    assert a.stats()["locks_held"] == ["shared-id"]

    a.release_all()
    b.acquire("shared-id")
    assert b.stats()["locks_held"] == ["shared-id"]
    b.release_all()


# --------------------------------------------------------------------------- #
# P1-1: scoring model routing, cases 56 to 58
# --------------------------------------------------------------------------- #


def test_case_56_scoring_resolves_the_collisions_first_match_could_not() -> None:
    """Case 56: the finding. Multi-intent tasks route on evidence, not order."""
    sel = ScoringModelSelector()

    assert sel.select("debug the failing deploy to production").model == "qwen3:32b"
    assert sel.select("deploy the fix for the crash").model == "qwen3:32b"
    assert sel.select("check why the release is broken").model == "qwen3:32b"
    assert sel.select("review the deploy script before we ship it").model == "qwen3:32b", (
        "this is the one first-match got wrong in Phase 1 and Phase 2"
    )


def test_case_57_phase1_routing_still_holds() -> None:
    """Case 57: scoring must not break the original routing contract."""
    sel = ScoringModelSelector()

    assert sel.select("deploy the api container to staging").model == "devstral"
    assert sel.select("release the new systemd unit").model == "devstral"
    assert sel.select("debug why nginx keeps crashing").model == "qwen3:32b"
    assert sel.select("find the root cause of this traceback").model == "qwen3:32b"
    assert sel.select("list the running containers").model == "qwen3:8b"

    fallback = sel.select("xyzzy plugh")
    assert fallback.matched_rule == "default"
    assert fallback.confidence < 0.5

    limited = ScoringModelSelector(available=["qwen3:32b", "qwen3:8b"])
    assert limited.select("deploy the api to staging").model != "devstral"


def test_case_58_confidence_reflects_the_winning_margin() -> None:
    """Case 58: a contested route reports lower confidence than a clear one."""
    sel = ScoringModelSelector()

    clear = sel.select("deploy the api container to staging")
    contested = sel.select("debug the failing deploy to production")

    assert clear.confidence > contested.confidence, (
        f"clear={clear.confidence} contested={contested.confidence}"
    )
    assert len(sel.score("debug the failing deploy to production")) >= 2
    assert "over" in contested.reason, "the reason must name what it beat"

    trace = sel.explain("debug the failing deploy to production")
    assert "debug" in trace and "deploy" in trace


# --------------------------------------------------------------------------- #
# Integration, cases 59 to 61
# --------------------------------------------------------------------------- #


def build_pipeline(tmp_path: Path, **kwargs) -> Phase3IntegratedExecutor:
    """Phase 3 pipeline with external dependencies stubbed to a known state."""
    defaults = dict(
        clarifier=AskClarifyingQuestionsBehavior(ask=lambda a: "staging"),
        safety=ParsingSafetyClassifier(confirm=lambda v: True),
        ollama=healthy(),
    )
    defaults.update(kwargs)
    return Phase3IntegratedExecutor(workdir=tmp_path / "run", **defaults)


def test_case_59_failed_run_auto_reverts_a_file_nobody_registered(tmp_path: Path) -> None:
    """Case 59: end to end. No caller-supplied undo, and the file comes back."""
    conf = tmp_path / "nginx.conf"
    conf.write_text("original\n", encoding="utf-8")

    steps = [
        Step("edit", kind="write", command="tee /srv/api/nginx.conf",
             description="rewrite the config", estimated_seconds=20,
             touches=[str(conf)],
             run=lambda: conf.write_text("MODIFIED BY THE STEP\n", encoding="utf-8")),
        Step("restart", command="systemctl restart api", description="restart",
             estimated_seconds=20,
             run=lambda: (_ for _ in ()).throw(RuntimeError("unit failed to start"))),
    ]

    pipe = build_pipeline(tmp_path)
    result = pipe.run("p3-revert", "deploy the api stack to staging", steps)
    # Note: no rollback_actions argument at all.

    assert result.status is PipelineStatus.EXECUTION_FAILED
    assert conf.read_text(encoding="utf-8") == "original\n", (
        "Phase 1 and 2 would have left this modified"
    )
    assert result.rollback_verified is True
    assert result.generated_undos, "undos must have been derived, not supplied"
    assert all(r.status == "undone" for r in result.rollback.results)


def test_case_60_undo_coverage_and_verdicts_reach_the_audit(tmp_path: Path) -> None:
    """Case 60: the operator can see what was covered and what was not."""
    conf = tmp_path / "app.conf"
    conf.write_text("v1", encoding="utf-8")

    steps = [
        Step("edit", kind="write", command="echo x", description="edit",
             estimated_seconds=20, touches=[str(conf)], run=lambda: "ok"),
        Step("migrate", command="alembic upgrade head", description="migrate",
             estimated_seconds=40, run=lambda: "ok"),
    ]

    pipe = build_pipeline(tmp_path)
    result = pipe.run("p3-audit", "deploy the api stack to staging", steps)

    assert result.status is PipelineStatus.COMPLETED
    payload = json.loads(Path(result.audit_path).read_text(encoding="utf-8"))

    assert payload["phase"] == 3
    assert payload["undo_coverage"]["covered"] == ["edit"]
    assert payload["undo_coverage"]["uncovered"] == ["migrate"], (
        "a step with no derivable undo must be named in the audit"
    )
    assert payload["generated_undos"]
    assert payload["routing_scores"], "the full score table must be persisted"
    stages = {d["stage"] for d in payload["decisions"]}
    assert "undo_generation" in stages
    assert payload["stages_completed"] == [s.value for s in Stage]


def test_case_61_pipeline_refuses_a_locked_task_id(tmp_path: Path) -> None:
    """Case 61: a second pipeline on a live task id refuses to start."""
    steps = [Step("s1", command="echo a", description="a", estimated_seconds=20,
                  run=lambda: "ok")]

    first = build_pipeline(tmp_path)
    first.checkpointer.acquire("contended")

    second = build_pipeline(tmp_path, checkpointer=LockedCheckpointing(
        tmp_path / "run" / "checkpoints"))
    result = second.run("contended", "deploy the api stack to staging", steps)

    assert result.status is PipelineStatus.PRECONDITION_FAILED
    assert result.lock_holder is not None
    assert result.lock_holder["pid"] == os.getpid()
    assert result.execution is None, "nothing may run while another holds the lock"

    payload = json.loads(Path(result.audit_path).read_text(encoding="utf-8"))
    assert any(d["stage"] == "task_lock" for d in payload["decisions"])

    first.checkpointer.release_all()
    ok = second.run("contended", "deploy the api stack to staging", steps)
    assert ok.status is PipelineStatus.COMPLETED
