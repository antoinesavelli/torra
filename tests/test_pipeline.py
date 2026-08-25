"""
The seven stages end to end, and the TUI's contract with them.

These are the tests that would have caught "the harness has no LLM": every one
drives a real Harness with a real Agent against a scripted backend, so a model
that is never called shows up as a failure rather than as six phases of work.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import plan_reply, scripted

from harness.core import ConflictDetector, OllamaClient, Step, TaskSpec
from harness.gates import Precondition, RiskLevel, StateVerifier, UndoGenerator, Verdict
from harness.pipeline import Harness
from harness.pipeline_types import Outcome, Plan, RunResult, Stage, StepPlan

SAFE_PLAN = plan_reply(
    "write-config", "set workers to 4 in /tmp/h7/app.conf",
    [{"id": "mkdir", "run": "mkdir -p /tmp/h7", "estimated_seconds": 2},
     {"id": "write", "kind": "write",
      "run": "printf 'workers = 4\\n' > /tmp/h7/app.conf",
      "touches": ["/tmp/h7/app.conf"], "estimated_seconds": 3}])


# --------------------------------------------------------------------------- #
# The happy path
# --------------------------------------------------------------------------- #


def test_all_seven_stages_in_order(make_harness, tmp_path: Path) -> None:
    """The stage contract, which the TUI and the CLI both depend on."""
    target = tmp_path / "app.conf"
    harness = make_harness(scripted(plan_reply(
        "t", f"write the worker count to {target}",
        [{"id": "w", "kind": "write", "run": f"printf 'workers = 4\\n' > {target}",
          "touches": [str(target)]}])))

    planned = harness.plan(f"set the worker count to 4 in {target}")
    assert planned.outcome is Outcome.AWAITING_APPROVAL
    assert planned.stages == ["converse", "observe", "propose", "gate", "present"]

    done = harness.execute(planned, approvals={})

    assert done.outcome is Outcome.COMPLETED
    assert done.stages == [s.value for s in Stage]
    assert target.read_text(encoding="utf-8").strip() == "workers = 4"
    assert Path(done.audit_path).exists()


def test_the_audit_records_the_model_and_the_gates(make_harness,
                                                   tmp_path: Path) -> None:
    """An audit that omits which model proposed what is not an audit."""
    target = tmp_path / "a.conf"
    harness = make_harness(scripted(plan_reply(
        "t", f"write a marker into {target}",
        [{"id": "w", "run": f"touch {target}", "touches": [str(target)]}])))

    done = harness.execute(harness.plan(f"create {target}"), {})
    payload = json.loads(Path(done.audit_path).read_text(encoding="utf-8"))

    assert payload["outcome"] == "completed"
    assert payload["turn"]["model"]
    assert payload["turn"]["intent"] == "plan"
    assert payload["plan"]["steps"][0]["verdict"]["level"]
    assert payload["plan"]["steps"][0]["undos"]
    assert payload["plan"]["coverage"]["coverage_ratio"] == 1.0
    stages = {d["stage"] for d in payload["decisions"]}
    assert {"converse", "gate", "execute"} <= stages


def test_every_finished_turn_is_recorded_to_telemetry(make_harness,
                                                       tmp_path: Path) -> None:
    """Found in a full-file audit: `harness.telemetry.records.TaskLogger`
    had a working `record()` and no caller anywhere in pipeline.py,
    agent.py, or tui.py -- `harness label`/`harness evidence` (cli.py)
    only ever read tasks.jsonl, and nothing ever wrote to it. Every
    `_finish()` call that writes an audit record must also log one here,
    including turns that never reach execute() at all."""
    target = tmp_path / "marker"
    harness = make_harness(scripted(
        {"intent": "answer", "message": "just a question"},
        plan_reply("t", f"touch {target}",
                  [{"id": "touch", "run": f"touch {target}"}])))

    answered = harness.plan("what is this machine")
    assert answered.outcome is Outcome.ANSWERED
    planned = harness.plan(f"create {target}")
    harness.execute(planned, {})

    records = harness.telemetry.read()
    assert len(records) == 2
    assert records[0].status == "answered"
    assert records[1].status == "completed"
    assert records[1].intent == "plan"


def test_the_telemetry_record_carries_the_anchors_the_clarifier_found(
        make_harness, tmp_path: Path) -> None:
    """Found in a full-file audit: TaskRecord.clarifier_anchors was
    declared but from_result() never assigned it, so every persisted
    record carried an empty list. LoopholeAudit only worked because it
    falls back to recomputing anchors from the request text -- this
    checks the field itself is now populated, so the log is
    self-contained without needing that fallback."""
    target = tmp_path / "app.conf"
    harness = make_harness(scripted(plan_reply(
        "t", f"write to {target}",
        [{"id": "w", "run": f"touch {target}", "touches": [str(target)]}])))

    harness.execute(harness.plan(f"write to {target}"), {})

    records = harness.telemetry.read()
    assert records[-1].clarifier_anchors, (
        f"an absolute path in {target!r} should have registered as an anchor"
    )
    assert "absolute path" in records[-1].clarifier_anchors


def test_a_precondition_blocked_plan_is_not_recorded_as_approved(
        make_harness, tmp_path: Path) -> None:
    """Found by an independent audit: `_apply_plan_fields` recorded
    `approved = all(sp.approved is not False for sp in plan.steps)`. A
    plan blocked by a failed precondition never reaches execute(), so
    every step's `approved` is still None -- `is not False` treated that
    as approved, so a BLOCKED task nobody ever saw was persisted as
    `approved: true`. `_apply_approvals()` always resolves every step to
    a real True/False once execute() actually runs, so requiring `is
    True` is exactly as strict for a normal completed run."""
    marker = tmp_path / "nope"
    verifier = StateVerifier()
    verifier.add(Precondition("impossible", lambda: False, "never passes"))
    harness = make_harness(scripted(plan_reply(
        "t", f"create {marker}", [{"id": "t", "run": f"touch {marker}"}])),
        verifier=verifier)

    result = harness.plan(f"create {marker}")

    assert result.outcome is Outcome.BLOCKED
    records = harness.telemetry.read()
    assert records[-1].approved is False


def test_a_normally_completed_plan_is_still_recorded_as_approved(
        make_harness, tmp_path: Path) -> None:
    target = tmp_path / "app.conf"
    harness = make_harness(scripted(plan_reply(
        "t", f"write to {target}",
        [{"id": "w", "run": f"touch {target}", "touches": [str(target)]}])))

    harness.execute(harness.plan(f"write to {target}"), {})

    assert harness.telemetry.read()[-1].approved is True


# --------------------------------------------------------------------------- #
# Approval
# --------------------------------------------------------------------------- #


def test_a_dangerous_step_does_not_run_unapproved(make_harness,
                                                  tmp_path: Path) -> None:
    """The whole point. An unapproved dangerous command is refused, not defaulted."""
    victim = tmp_path / "victim.txt"
    victim.write_text("still here", encoding="utf-8")
    harness = make_harness(scripted(plan_reply(
        "t", f"remove {victim}",
        [{"id": "rm", "run": f"rm -f {victim}", "touches": [str(victim)]}])))

    planned = harness.plan(f"delete {victim}")
    assert planned.plan is not None
    assert planned.plan.needs_confirmation, "rm should require confirmation"

    done = harness.execute(planned, approvals={})

    assert done.outcome is Outcome.REJECTED
    assert victim.exists(), "an unapproved command ran"
    assert done.results == []


def test_an_approved_step_runs(make_harness, tmp_path: Path) -> None:
    """Approval is per step id, and it is honoured."""
    victim = tmp_path / "victim.txt"
    victim.write_text("goodbye", encoding="utf-8")
    harness = make_harness(scripted(plan_reply(
        "t", f"remove {victim}",
        [{"id": "rm", "run": f"rm -f {victim}", "touches": [str(victim)]}])))

    planned = harness.plan(f"delete {victim}")
    done = harness.execute(planned, approvals={"rm": True})

    assert done.outcome is Outcome.COMPLETED
    assert not victim.exists()


def test_a_safe_step_needs_no_tick(make_harness, tmp_path: Path) -> None:
    """Asking for approval on `df -h` trains people to tick without reading."""
    harness = make_harness(scripted(plan_reply(
        "t", "report disk usage on /",
        [{"id": "df", "run": "df -h /"}])))

    planned = harness.plan("report disk usage on /")
    assert planned.plan is not None
    assert planned.plan.needs_confirmation == []
    assert harness.execute(planned, {}).outcome is Outcome.COMPLETED


def test_reject_never_runs_an_all_safe_plan(make_harness, tmp_path: Path) -> None:
    """Found in a full-file audit: the TUI's Reject button used to bypass
    the pipeline entirely (self.current = None, no audit, no telemetry).
    Routing it through execute(result, {}) instead would have been wrong
    in a different way -- a safe step's `approved` defaults to True
    regardless of the approvals dict, so an all-safe plan would actually
    *run* on "reject" rather than being declined. Harness.reject() must
    refuse unconditionally, independent of what would have needed a tick."""
    marker = tmp_path / "marker"
    harness = make_harness(scripted(plan_reply(
        "t", f"touch {marker}", [{"id": "touch", "run": f"touch {marker}"}])))

    planned = harness.plan(f"create {marker}")
    assert planned.plan is not None and planned.plan.needs_confirmation == []

    done = harness.reject(planned)

    assert done.outcome is Outcome.REJECTED
    assert not marker.exists(), "reject() ran an all-safe plan instead of declining it"
    assert done.audit_path
    records = harness.telemetry.read()
    assert records and records[-1].status == "rejected"


# --------------------------------------------------------------------------- #
# Failure and recovery
# --------------------------------------------------------------------------- #


def test_a_failed_step_reverts_a_file_nobody_registered(make_harness,
                                                        tmp_path: Path) -> None:
    """No caller-supplied undo anywhere, and the file still comes back."""
    conf = tmp_path / "app.conf"
    conf.write_text("ORIGINAL\n", encoding="utf-8")
    harness = make_harness(scripted(plan_reply(
        "t", f"rewrite {conf} then verify the service",
        [{"id": "write", "kind": "write", "run": f"printf 'NEW\\n' > {conf}",
          "touches": [str(conf)]},
         {"id": "boom", "run": "exit 7"}])))

    done = harness.execute(harness.plan(f"rewrite {conf} and verify"), {})

    assert done.outcome is Outcome.FAILED
    assert conf.read_text(encoding="utf-8") == "ORIGINAL\n"
    assert done.rollback is not None and done.rollback.ok
    assert all(r.verified is not False for r in done.rollback.results)


def test_a_successful_run_cleans_up_its_shadow_copies(make_harness,
                                                       tmp_path: Path) -> None:
    """Found in a full-file audit: UndoGenerator.cleanup() had no caller
    anywhere, so every run that snapshotted a file left its shadow
    directory on disk forever (14 accumulated on one real machine)."""
    conf = tmp_path / "app.conf"
    conf.write_text("original\n", encoding="utf-8")
    harness = make_harness(scripted(plan_reply(
        "t", f"rewrite {conf}",
        [{"id": "write", "kind": "write", "run": f"printf 'new\\n' > {conf}",
          "touches": [str(conf)]}])))

    done = harness.execute(harness.plan(f"rewrite {conf}"), {})

    assert done.outcome is Outcome.COMPLETED
    assert not any(harness.undo.shadow_dir.iterdir()), (
        "a completed run left its shadow copies behind")


def test_a_dry_run_keeps_its_shadow_copies(make_harness, tmp_path: Path) -> None:
    """A dry run's own snapshots (taken at propose time, before dry_run is
    even known) must survive it -- clicking Run right after still needs
    them for real rollback."""
    conf = tmp_path / "app.conf"
    conf.write_text("original\n", encoding="utf-8")
    harness = make_harness(scripted(plan_reply(
        "t", f"rewrite {conf}",
        [{"id": "write", "kind": "write", "run": f"printf 'new\\n' > {conf}",
          "touches": [str(conf)]}])))

    done = harness.execute(harness.plan(f"rewrite {conf}"), {}, dry_run=True)

    assert done.outcome is Outcome.COMPLETED
    assert any(harness.undo.shadow_dir.iterdir()), (
        "a dry run deleted the snapshots the real run would need"
    )


def test_a_declared_safe_rollback_is_still_classified(make_harness,
                                                       tmp_path: Path) -> None:
    """A declared `safe: true` cannot suppress a real CRITICAL classification.

    Before this was fixed, the exact command below ran automatically during
    the rollback sweep with zero classification and zero confirmation --
    see HANDOFF.md §6.4. Now it must be auto-declined (the default
    confirm_unsafe with nobody wired in to approve it), not silently run.
    """
    target = tmp_path / "should-survive"
    target.mkdir()
    harness = make_harness(scripted(plan_reply(
        "t", "provision then fail",
        [{"id": "boom", "run": "exit 7"}],
        rollback=[{"run": f"rm -rf {target}", "description": "cleanup",
                  "safe": True}])))

    done = harness.execute(harness.plan("provision then fail"), {})

    assert done.outcome is Outcome.FAILED
    assert done.rollback is not None
    assert [r.status for r in done.rollback.results] == ["cancelled"]
    assert target.exists(), "a CRITICAL command ran despite being declared safe"
    overrides = [d for d in done.decisions if d["decision"] == "rollback safety override"]
    assert len(overrides) == 1


def test_execute_twice_on_the_same_result_does_not_replay_it(make_harness,
                                                              tmp_path: Path) -> None:
    """Found live in an audit log: stages [...,"execute","report","execute"]
    and two result entries for one step. The TUI's Run button stays
    clickable mid-run, so a double-click dispatched execute() twice on the
    same already-completed RunResult, re-running every step's command a
    second time. A read-only `du` is merely wasteful to repeat; anything
    that mutates state would not be."""
    marker = tmp_path / "marker"
    harness = make_harness(scripted(plan_reply(
        "t", f"touch {marker}",
        [{"id": "touch", "run": f"touch {marker}"}])))

    planned = harness.plan(f"create {marker}")
    once = harness.execute(planned, {})
    assert once.outcome is Outcome.COMPLETED
    assert len(once.results) == 1

    twice = harness.execute(once, {})

    assert twice.outcome is Outcome.COMPLETED
    assert len(twice.results) == 1, "the step ran a second time"
    assert twice.stages.count("execute") == 1
    refusals = [d for d in twice.decisions if d["decision"] == "refused: already executed"]
    assert len(refusals) == 1


def test_ticking_the_box_after_a_rejection_still_runs(make_harness,
                                                       tmp_path: Path) -> None:
    """Found live right after the guard above shipped: a plan clicked Run
    before its checkbox was ticked gets REJECTED (nothing ran, by design --
    see 2.3), the operator ticks the box and clicks Run again, and the
    ordinary retry has to work. Keying the replay guard on `outcome !=
    AWAITING_APPROVAL` blocked this exact, completely normal flow, because
    REJECTED is a terminal outcome too even though zero commands ran."""
    victim = tmp_path / "victim.txt"
    victim.write_text("still here", encoding="utf-8")
    harness = make_harness(scripted(plan_reply(
        "t", f"remove {victim}",
        [{"id": "rm", "run": f"rm -f {victim}", "touches": [str(victim)]}])))

    planned = harness.plan(f"delete {victim}")
    rejected = harness.execute(planned, approvals={})
    assert rejected.outcome is Outcome.REJECTED
    assert victim.exists()

    approved = harness.execute(rejected, approvals={"rm": True})

    assert approved.outcome is Outcome.COMPLETED
    assert not victim.exists()


def test_reject_refuses_to_overwrite_a_plan_that_already_ran(make_harness,
                                                              tmp_path: Path) -> None:
    """Found in a full-file audit of the TUI's own presentation layer: the
    Reject button was never disabled after a run completed (unlike
    Run/Dry-run), and Harness.reject() had no guard against being called
    on an already-executed result. A stray click on Reject after a real
    run would have silently overwritten a COMPLETED outcome with
    REJECTED and written a fresh audit record claiming "nothing ran" --
    actively misrepresenting a run that may have had real, destructive
    side effects."""
    marker = tmp_path / "marker"
    harness = make_harness(scripted(plan_reply(
        "t", f"touch {marker}", [{"id": "touch", "run": f"touch {marker}"}])))

    done = harness.execute(harness.plan(f"touch {marker}"), {})
    assert done.outcome is Outcome.COMPLETED
    assert marker.exists()

    still_done = harness.reject(done)

    assert still_done.outcome is Outcome.COMPLETED, (
        "a completed run's outcome was overwritten by a late Reject"
    )
    assert marker.exists()
    refusals = [d for d in still_done.decisions if d["decision"] == "refused: already executed"]
    assert len(refusals) == 1


def test_reject_still_works_on_a_plan_that_never_ran(make_harness,
                                                      tmp_path: Path) -> None:
    """The ordinary case must keep working: nothing executed yet, so
    Reject is free to actually reject."""
    marker = tmp_path / "marker"
    harness = make_harness(scripted(plan_reply(
        "t", f"touch {marker}", [{"id": "touch", "run": f"touch {marker}"}])))

    planned = harness.plan(f"touch {marker}")
    rejected = harness.reject(planned)

    assert rejected.outcome is Outcome.REJECTED
    assert not marker.exists()


def test_a_dry_run_does_not_block_the_real_run_that_follows(make_harness,
                                                             tmp_path: Path) -> None:
    """Previewing a plan with Dry run must not count as "already executed"
    -- that would block the real Run a dry run is meant to precede."""
    marker = tmp_path / "marker"
    harness = make_harness(scripted(plan_reply(
        "t", f"touch {marker}",
        [{"id": "touch", "run": f"touch {marker}"}])))

    planned = harness.plan(f"create {marker}")
    dry = harness.execute(planned, {}, dry_run=True)
    assert dry.outcome is Outcome.COMPLETED
    assert not marker.exists(), "a dry run touched the filesystem"

    real = harness.execute(dry, {})

    assert real.outcome is Outcome.COMPLETED
    assert marker.exists()
    assert len(real.results) == 1


def test_gate_does_not_redundantly_walk_watch_roots(make_harness,
                                                    tmp_path: Path) -> None:
    """Found in a full-file audit: _gate() called conflicts.detect() right
    after conflicts.baseline() on the same object -- detect() re-reads
    exactly the state baseline() just captured, so it always returned []],
    the same as Plan.conflicts' own default. With inotify unavailable,
    ConflictDetector._detect_tree falls straight to a full _reconcile()
    walk with no event queue to check first, so this cost a second full
    filesystem walk of watch_roots on every single plan proposal for
    nothing -- full_walks must stay 0 through baseline() alone."""
    watched = tmp_path / "watched"
    watched.mkdir()
    detector = ConflictDetector(use_inotify=False)
    harness = make_harness(scripted(plan_reply(
        "t", "touch a file", [{"id": "w", "run": "true"}],
        watch_roots=[str(watched)])), conflicts=detector)

    harness.plan("touch a file")

    assert detector.full_walks == 0, "baseline() alone must not trigger a _reconcile() walk"


def test_a_precondition_flip_stops_execution(make_harness,
                                             tmp_path: Path) -> None:
    """The probe-12 scenario: a service dies at step one, step two must not run."""
    marker = tmp_path / "should-not-exist"
    state = {"up": True}
    verifier = StateVerifier()
    verifier.add(Precondition("svc", lambda: state["up"], "must stay up",
                              ttl_seconds=0))

    harness = make_harness(scripted(plan_reply(
        "t", "restart the api and then write a marker",
        [{"id": "kill", "run": "true"},
         {"id": "second", "run": f"touch {marker}"}])), verifier=verifier)

    planned = harness.plan("restart ollama.service on popos-01 and verify")
    state["up"] = False
    done = harness.execute(planned, {})

    assert done.outcome is Outcome.BLOCKED
    assert "precondition went false mid-run" in done.abort_reason
    assert not marker.exists()


def test_a_block_before_any_step_ran_is_still_retryable(make_harness,
                                                         tmp_path: Path) -> None:
    """Found in a full-file audit, not live: `executed` used to be set
    unconditionally before the step loop, so a plan BLOCKED by a
    precondition or conflict check on step one -- before a single command
    ran -- was marked "already executed" the same as a plan that actually
    ran something, and could never be retried. The same bug §2.8 fixed for
    REJECTED, just missed for this path: zero results means zero side
    effects, and it must stay retryable once the underlying condition is
    fixed, exactly like ticking a box after a rejection."""
    marker = tmp_path / "marker"
    state = {"up": True}
    verifier = StateVerifier()
    verifier.add(Precondition("svc", lambda: state["up"], "must stay up",
                              ttl_seconds=0))
    harness = make_harness(scripted(plan_reply(
        "t", f"touch {marker}",
        [{"id": "touch", "run": f"touch {marker}"}])), verifier=verifier)

    planned = harness.plan(f"create {marker}")
    state["up"] = False
    blocked = harness.execute(planned, {})
    assert blocked.outcome is Outcome.BLOCKED
    assert blocked.results == []

    state["up"] = True
    retried = harness.execute(blocked, {})

    assert retried.outcome is Outcome.COMPLETED
    assert marker.exists()


def test_an_external_edit_stops_the_run(make_harness, tmp_path: Path) -> None:
    """Something moved under us. Continuing would clobber a human's work."""
    conf = tmp_path / "shared.conf"
    conf.write_text("original", encoding="utf-8")
    marker = tmp_path / "second-ran"

    harness = make_harness(scripted(plan_reply(
        "t", f"touch {conf} then write a marker",
        [{"id": "first", "run": "true", "touches": [str(conf)]},
         {"id": "second", "run": f"touch {marker}", "touches": [str(conf)]}])))

    planned = harness.plan(f"update {conf} in two steps")
    conf.write_text("EDITED BY A HUMAN", encoding="utf-8")
    done = harness.execute(planned, {})

    assert done.outcome is Outcome.BLOCKED
    assert "external file change" in done.abort_reason
    assert not marker.exists()


def test_a_dry_run_executes_nothing_but_gates_everything(make_harness,
                                                         tmp_path: Path) -> None:
    marker = tmp_path / "nope"
    harness = make_harness(scripted(plan_reply(
        "t", f"create {marker}", [{"id": "t", "run": f"touch {marker}"}])))

    done = harness.execute(harness.plan(f"create {marker}"), {}, dry_run=True)

    assert done.outcome is Outcome.COMPLETED
    assert not marker.exists()
    assert done.results and "[dry-run]" in done.results[0].output
    assert done.plan is not None and done.plan.steps[0].verdict.level


# --------------------------------------------------------------------------- #
# Non-plan outcomes
# --------------------------------------------------------------------------- #


def test_an_audit_write_failure_is_logged_not_raised(
        make_harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Telemetry can never break a run (invariant §2.5) -- the audit
    write is the same principle applied to the file it writes, not just
    the JSONL log."""
    marker = tmp_path / "marker"
    spec = TaskSpec.validate({
        "task_id": "audit-blocked", "request": f"create {marker}",
        "steps": [{"id": "touch", "run": f"touch {marker}"}]}, source="test")
    harness = make_harness(scripted({}))
    audit_path = harness.audit_dir / "audit-blocked.audit.json"
    real_write_text = Path.write_text

    def selective_broken_write_text(self, *args, **kwargs):
        if self == audit_path:
            raise OSError("simulated disk full")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", selective_broken_write_text)
    plan = harness._gate(spec)
    result = RunResult(request=spec.request, outcome=Outcome.AWAITING_APPROVAL, plan=plan)

    done = harness.execute(result, {})

    assert done.outcome is Outcome.COMPLETED, "the run itself must still succeed"
    assert done.audit_path == ""
    # Not done.decisions: result.decisions is snapshotted from the log
    # before this failure is logged (the same "_finish() snapshots too
    # early" shape as the summary-failure case in another test above),
    # so the record only shows up on the harness's own live log.
    assert any(d.decision == "audit write failed" for d in harness.decisions.records)


def test_stats_reports_every_subsystem(make_harness) -> None:
    harness = make_harness(scripted({}))
    stats = harness.stats()
    assert set(stats) == {"agent", "conflicts", "verifier", "undo", "decisions"}


def test_close_releases_watches_without_raising(make_harness) -> None:
    harness = make_harness(scripted({}))
    harness.close()  # must not raise


def test_a_question_is_answered_not_planned(make_harness) -> None:
    harness = make_harness(scripted(
        {"intent": "answer", "message": "40 GB free on /, 60% used."}))
    result = harness.plan("how much disk is free")

    assert result.outcome is Outcome.ANSWERED
    assert result.plan is None
    assert "40 GB" in result.message


def test_run_returns_a_non_awaiting_outcome_without_calling_approve(make_harness) -> None:
    """The convenience run() wrapper's early exit -- nothing to gate means
    nothing to approve, so `approve` must never even be called."""
    harness = make_harness(scripted(
        {"intent": "answer", "message": "40 GB free on /, 60% used."}))
    calls = []

    result = harness.run("how much disk is free", approve=lambda plan: calls.append(plan))

    assert result.outcome is Outcome.ANSWERED
    assert calls == []


def test_answer_on_a_result_with_no_turn_is_a_no_op(make_harness) -> None:
    """`answer()` re-plans by reading the original turn's questions --
    a result built without ever going through the model (a hand-written
    task file's RunResult) has no turn to answer against."""
    harness = make_harness(scripted({}))
    result = RunResult(request="r", outcome=Outcome.NEEDS_ANSWER)

    same = harness.answer(result, ["some answer"])

    assert same is result


def test_a_resume_point_that_cannot_be_written_does_not_stop_the_run(
        make_harness, tmp_path: Path) -> None:
    """A resume point is a nicety, not a reason to stop -- both the
    mid-run save() failure and _finish_success's own best-effort cleanup
    of the (never successfully written) resume file must be swallowed.
    Forced by making the resume path itself a pre-existing directory, so
    both `ResumePoint.save()` (os.replace onto a directory) and the later
    `resume_path.unlink()` (can't unlink a directory) fail for real."""
    marker = tmp_path / "marker"
    spec = TaskSpec.validate({
        "task_id": "resume-blocked", "request": f"create {marker}",
        "steps": [{"id": "touch", "run": f"touch {marker}"}]}, source="test")
    harness = make_harness(scripted({}))
    resume_dir = harness.workdir / "resume" / "resume-blocked.json"
    resume_dir.mkdir(parents=True)

    plan = harness._gate(spec)
    result = RunResult(request=spec.request, outcome=Outcome.AWAITING_APPROVAL, plan=plan)
    done = harness.execute(result, {})

    assert done.outcome is Outcome.COMPLETED
    assert marker.exists(), "the real step still ran despite the resume-point failure"


def test_a_step_with_no_run_action_fails_instead_of_crashing(make_harness) -> None:
    """`TaskSpec.to_steps()` always attaches a real closure -- this guards
    a `Step` built some other way (a `Plan` assembled by hand, as this
    test does, rather than through `_gate()`)."""
    spec = TaskSpec.validate({
        "task_id": "t", "request": "r", "steps": [{"id": "s1", "run": "true"}]},
        source="test")
    step = Step(step_id="s1", command="true", run=None)
    sp = StepPlan(step=step, verdict=Verdict("true", RiskLevel.SAFE))
    plan = Plan(spec=spec, steps=[sp])
    harness = make_harness(scripted({}))
    result = RunResult(request=spec.request, outcome=Outcome.AWAITING_APPROVAL, plan=plan)

    done = harness.execute(result, {})

    assert done.outcome is Outcome.FAILED
    assert "has no run action attached" in done.results[0].error


def test_execute_on_a_result_with_no_plan_fails_rather_than_crashing(make_harness) -> None:
    """execute() is only ever called on an AWAITING_APPROVAL result with a
    plan attached in normal use -- this is the defensive guard for a
    caller that skips straight to execute() on something else, e.g. an
    ANSWERED result."""
    harness = make_harness(scripted(
        {"intent": "answer", "message": "40 GB free on /, 60% used."}))
    answered = harness.plan("how much disk is free")

    done = harness.execute(answered, {})

    assert done.outcome is Outcome.FAILED
    assert done.abort_reason == "nothing to execute"


def test_a_vague_request_becomes_a_question(make_harness) -> None:
    """The model asks, informed by what the clarifier found missing."""
    harness = make_harness(scripted(
        {"intent": "ask", "message": "Which host?",
         "questions": ["Which host do you mean?"]}))
    result = harness.plan("tidy up the box")

    assert result.outcome is Outcome.NEEDS_ANSWER
    assert result.turn is not None
    assert result.turn.questions
    assert result.turn.clarifier_gaps, "the clarifier fed the model its gaps"


def test_a_refusal_is_reported(make_harness) -> None:
    harness = make_harness(scripted(
        {"intent": "refuse", "message": "That would destroy the boot partition."}))
    assert harness.plan("wipe /boot").outcome is Outcome.REFUSED


def test_a_dead_backend_is_reported_with_recovery(make_harness) -> None:
    def dead(path, payload, timeout):
        raise ConnectionRefusedError("connection refused")

    harness = make_harness(dead)
    result = harness.plan("do anything")

    assert result.outcome is Outcome.BACKEND_DOWN
    assert "generation failed" in result.abort_reason
    assert len(harness.ollama.recovery_options()) == 3


def test_a_failed_precondition_blocks_before_anything_runs(make_harness,
                                                           tmp_path: Path) -> None:
    marker = tmp_path / "nope"
    verifier = StateVerifier()
    verifier.add(Precondition("impossible", lambda: False, "never passes"))

    harness = make_harness(scripted(plan_reply(
        "t", f"create {marker}", [{"id": "t", "run": f"touch {marker}"}])),
        verifier=verifier)
    result = harness.plan(f"create {marker}")

    assert result.outcome is Outcome.BLOCKED
    assert "preconditions failed" in result.abort_reason
    assert not marker.exists()


# --------------------------------------------------------------------------- #
# The run() convenience and hand-written files
# --------------------------------------------------------------------------- #


def test_run_approves_through_a_callback(make_harness, tmp_path: Path) -> None:
    """What the CLI and the TUI both use."""
    victim = tmp_path / "v.txt"
    victim.write_text("x", encoding="utf-8")
    harness = make_harness(scripted(plan_reply(
        "t", f"remove {victim}",
        [{"id": "rm", "run": f"rm -f {victim}", "touches": [str(victim)]}])))

    seen = {}

    def approve(plan) -> dict:
        seen["worst"] = plan.worst_level.value
        seen["needs"] = [sp.step.step_id for sp in plan.needs_confirmation]
        return {sp.step.step_id: True for sp in plan.needs_confirmation}

    done = harness.run(f"delete {victim}", approve=approve)

    assert done.outcome is Outcome.COMPLETED
    assert seen["needs"] == ["rm"]
    assert seen["worst"] in ("dangerous", "critical")
    assert not victim.exists()


def test_run_refuses_when_no_approver_is_given(make_harness,
                                               tmp_path: Path) -> None:
    """Unattended defaults to refusing, which is the safe direction."""
    victim = tmp_path / "v.txt"
    victim.write_text("x", encoding="utf-8")
    harness = make_harness(scripted(plan_reply(
        "t", f"remove {victim}",
        [{"id": "rm", "run": f"rm -f {victim}", "touches": [str(victim)]}])))

    assert harness.run(f"delete {victim}").outcome is Outcome.REJECTED
    assert victim.exists()


def test_a_hand_written_file_still_works_without_a_model(make_harness,
                                                         tmp_path: Path) -> None:
    """When the model is down or wrong, the gates must still be reachable."""
    marker = tmp_path / "made"
    spec = TaskSpec.validate({
        "task_id": "manual", "request": f"create {marker}",
        "steps": [{"id": "t", "run": f"touch {marker}"}]}, source="test")

    harness = make_harness(scripted({}))
    plan = harness._gate(spec)

    from harness.pipeline import RunResult
    result = RunResult(request=spec.request, outcome=Outcome.AWAITING_APPROVAL,
                       stages=["gate", "present"], plan=plan)
    done = harness.execute(result, {})

    assert done.outcome is Outcome.COMPLETED
    assert marker.exists()
    assert harness.ollama.calls == 0, "no model should have been consulted"


def test_a_malicious_task_id_cannot_write_the_resume_file_outside_the_workdir(
        make_harness, tmp_path: Path) -> None:
    """Found live: the resume-file path was built straight from
    `plan.spec.task_id` with no sanitizing at all -- unlike the audit path
    built in the very same function, which already sanitized. A task_id of
    "../../evil-poc" wrote a real file outside workdir/resume/ on any run
    where a later step failed before _finish_success()'s cleanup ran (which
    only fires on a fully successful run). task_id is fully model- or
    human-controlled -- TaskSpec.validate() does `task_id=str(data[...])`
    with no character restriction at all."""
    evil_id = "../../evil-poc"
    harness = make_harness(scripted(plan_reply(
        evil_id, "do something",
        [{"id": "ok", "run": "true"}, {"id": "bad", "run": "false"}])))

    planned = harness.plan("do something")
    done = harness.execute(planned, {})

    assert done.outcome is Outcome.FAILED
    escaped = tmp_path / "evil-poc.json"
    assert not escaped.exists(), "resume file escaped the harness's own workdir"
    resume_dir = tmp_path / "wd" / "resume"
    assert list(resume_dir.glob("*.json")), "the sanitized file should land here instead"


def test_knowledge_dir_defaults_from_the_environment(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """cli.py/tui.py construct Harness() with no knowledge_dir argument at
    all -- this is the only place that has to read HARNESS_KNOWLEDGE_DIR,
    so they need no changes to pick up a configured library."""
    monkeypatch.setenv("HARNESS_KNOWLEDGE_DIR", str(tmp_path))
    harness = Harness(workdir=tmp_path / "wd")
    assert harness.knowledge_dir == str(tmp_path)


def test_an_explicit_knowledge_dir_overrides_the_environment(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HARNESS_KNOWLEDGE_DIR", "/from/env")
    harness = Harness(workdir=tmp_path / "wd", knowledge_dir=str(tmp_path))
    assert harness.knowledge_dir == str(tmp_path)


def test_an_explicit_empty_knowledge_dir_disables_it_even_with_env_set(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HARNESS_KNOWLEDGE_DIR", "/from/env")
    harness = Harness(workdir=tmp_path / "wd", knowledge_dir="")
    assert harness.knowledge_dir is None


def test_knowledge_dir_is_none_by_default_with_nothing_configured(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("HARNESS_KNOWLEDGE_DIR", raising=False)
    harness = Harness(workdir=tmp_path / "wd")
    assert harness.knowledge_dir is None


# --------------------------------------------------------------------------- #
# Reporting: turning step output into an answer
# --------------------------------------------------------------------------- #


def _chat_transport(plan_content: dict, summary_text: str):
    """A scripted-like transport that answers /api/tags and /api/show the
    same way `conftest.scripted` does, but tells the plan call and the
    summarize call apart by `format` (json_mode) instead of call order --
    the summarize call always comes after an unknown number of repair
    rounds, so position alone cannot identify it."""
    def transport(path: str, payload: dict, timeout: float) -> dict:
        if path == "/api/tags":
            return {"models": [{"name": "devstral:24b"}]}
        if path == "/api/show":
            return {}
        if payload.get("format") == "json":
            return {"message": {"content": json.dumps(plan_content)}}
        return {"message": {"content": summary_text}}
    return transport


def test_a_completed_run_gets_a_synthesized_summary(make_harness, tmp_path: Path) -> None:
    """The gap this closes: `harness ask` used to print only ok/fail per
    step, never what a read step actually found."""
    target = tmp_path / "data.txt"
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")
    plan_content = plan_reply(
        "read-file", f"read {target}",
        [{"id": "read", "kind": "read", "run": f"cat {target}"}])

    harness = make_harness(_chat_transport(
        plan_content, "The file has three lines: one, two, three."))
    planned = harness.plan(f"how many lines are in {target}")
    assert planned.outcome is Outcome.AWAITING_APPROVAL

    done = harness.execute(planned, {})

    assert done.outcome is Outcome.COMPLETED
    assert done.summary == "The file has three lines: one, two, three."
    assert done.message == done.summary


def test_a_dry_run_never_gets_a_summary(make_harness, tmp_path: Path) -> None:
    """Found by an independent audit: _run_one_step() fabricates
    "[dry-run] <command>" as every step's "output" under a dry run --
    nothing was actually checked. Summarizing that fabricated text got a
    confident, plausible-sounding answer with zero real evidence behind
    it, indistinguishable from a genuine post-execution summary. A dry
    run must never reach _summarize() at all, not just label the result
    as a preview."""
    target = tmp_path / "data.txt"
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")
    plan_content = plan_reply(
        "read-file", f"read {target}",
        [{"id": "read", "kind": "read", "run": f"cat {target}"}])

    harness = make_harness(_chat_transport(
        plan_content, "The file has three lines: one, two, three."))
    planned = harness.plan(f"how many lines are in {target}")

    done = harness.execute(planned, {}, dry_run=True)

    assert done.outcome is Outcome.COMPLETED
    assert done.results[0].output == f"[dry-run] cat {target}"
    assert done.summary == ""


def test_a_hand_written_plan_gets_no_summary_and_consults_no_model(
        make_harness, tmp_path: Path) -> None:
    """The same invariant `test_a_hand_written_file_still_works_without_a_model`
    guards, checked from the summary field's own side: a plan nobody's
    model wrote must not become the trigger for the first model call."""
    marker = tmp_path / "made"
    spec = TaskSpec.validate({
        "task_id": "manual", "request": f"create {marker}",
        "steps": [{"id": "t", "run": f"touch {marker}"}]}, source="test")

    harness = make_harness(scripted({}))
    plan = harness._gate(spec)
    result = RunResult(request=spec.request, outcome=Outcome.AWAITING_APPROVAL,
                       stages=["gate", "present"], plan=plan)
    done = harness.execute(result, {})

    assert done.summary == ""
    assert harness.ollama.calls == 0


def test_a_failed_summarize_call_does_not_break_the_run(
        make_harness, tmp_path: Path) -> None:
    """Same rule as telemetry: a reporting step that can fail a real run
    would make the harness less reliable than not reporting at all."""
    target = tmp_path / "data.txt"
    target.write_text("hello\n", encoding="utf-8")
    plan_content = plan_reply(
        "read-file", f"read {target}",
        [{"id": "read", "kind": "read", "run": f"cat {target}"}])

    def transport(path: str, payload: dict, timeout: float) -> dict:
        if path == "/api/tags":
            return {"models": [{"name": "devstral:24b"}]}
        if path == "/api/show":
            return {}
        if payload.get("format") == "json":
            return {"message": {"content": json.dumps(plan_content)}}
        raise RuntimeError("daemon unreachable")

    harness = make_harness(transport)
    planned = harness.plan(f"what is in {target}")
    done = harness.execute(planned, {})

    assert done.outcome is Outcome.COMPLETED
    assert done.summary == ""
    # Found live: _finish() used to snapshot result.decisions before calling
    # _summarize(), so a failure here logged a decision that never made it
    # into `result` or the audit file -- an empty summary with no recorded
    # reason why.
    assert any(d["decision"] == "summary failed" for d in done.decisions)
    assert json.loads(Path(done.audit_path).read_text(encoding="utf-8"))["decisions"] == \
        done.decisions


# --------------------------------------------------------------------------- #
# pipeline_types convenience properties
# --------------------------------------------------------------------------- #


def test_step_plan_has_undo_reflects_whether_any_undo_was_derived(
        make_harness, tmp_path: Path) -> None:
    marker = tmp_path / "touched"
    spec = TaskSpec.validate({
        "task_id": "t", "request": f"create {marker}",
        "steps": [{"id": "touch", "run": f"touch {marker}", "touches": [str(marker)]},
                 {"id": "no-undo", "run": "true"}]}, source="test")
    harness = make_harness(scripted({}))

    plan = harness._gate(spec)

    by_id = {sp.step.step_id: sp for sp in plan.steps}
    assert by_id["touch"].has_undo is True
    assert by_id["no-undo"].has_undo is False


def test_run_result_ok_is_true_only_when_completed(make_harness, tmp_path: Path) -> None:
    marker = tmp_path / "made"
    spec = TaskSpec.validate({
        "task_id": "t", "request": f"create {marker}",
        "steps": [{"id": "touch", "run": f"touch {marker}"}]}, source="test")
    harness = make_harness(scripted({}))
    planned = RunResult(request=spec.request, outcome=Outcome.AWAITING_APPROVAL,
                        plan=harness._gate(spec))

    done = harness.execute(planned, {})

    assert done.outcome is Outcome.COMPLETED
    assert done.ok is True
    assert RunResult(request="r", outcome=Outcome.REJECTED).ok is False


def test_run_result_message_falls_back_to_the_outcome_when_nothing_else_is_set() -> None:
    """The last resort in `message`'s fallback chain -- no summary, no
    turn (or a turn with no message), no abort_reason."""
    result = RunResult(request="r", outcome=Outcome.REJECTED)
    assert result.message == "rejected"


# --------------------------------------------------------------------------- #
# Multi-session workdir sharing (the shadow-dir collision the tabs feature closes)
# --------------------------------------------------------------------------- #


def test_two_harnesses_sharing_a_workdir_do_not_collide_on_shadow_files(
        tmp_path: Path) -> None:
    """`UndoGenerator`'s default shadow_dir is keyed only by (workdir, pid)
    -- two Harness instances sharing a workdir in one process (exactly
    what tabs are) would collide on it, and one session's cleanup() could
    wipe another session's shadow copies mid-run. The fix is session-scoped:
    each tab's Harness gets an explicit undo=UndoGenerator(shadow_dir=...)
    keyed by session id instead of the shared default. This proves the
    pattern actually prevents the collision, not just that construction
    succeeds."""
    session_a_file = tmp_path / "a.conf"
    session_a_file.write_text("session a content", encoding="utf-8")
    harness_a = Harness(
        workdir=tmp_path, ollama=OllamaClient(transport=lambda p, pay, t: {}),
        undo=UndoGenerator(shadow_dir=tmp_path / "harness-shadow-session-a"))
    harness_b = Harness(
        workdir=tmp_path, ollama=OllamaClient(transport=lambda p, pay, t: {}),
        undo=UndoGenerator(shadow_dir=tmp_path / "harness-shadow-session-b"))

    assert harness_a.undo.shadow_dir != harness_b.undo.shadow_dir

    spec_a = TaskSpec.validate({
        "task_id": "t", "request": f"edit {session_a_file}",
        "steps": [{"id": "edit", "run": f"echo overwritten > {session_a_file}",
                  "touches": [str(session_a_file)]}]}, source="test")
    # _gate() snapshots at propose time -- before execute() runs, and
    # regardless of dry-run/real, matching how the real pipeline works
    # (see pipeline.py's own comment on why cleanup() only fires on a
    # successful real execute(), not here).
    harness_a._gate(spec_a)
    assert harness_a.undo.shadow_dir.exists()
    shadow_files_before = list(harness_a.undo.shadow_dir.iterdir())
    assert shadow_files_before, "session a should have a real shadow copy on disk"

    harness_b.undo.cleanup()

    assert list(harness_a.undo.shadow_dir.iterdir()) == shadow_files_before, (
        "session b's cleanup must never touch session a's shadow directory"
    )


def test_two_sessions_non_plan_turns_do_not_collide_on_the_same_audit_file(
        tmp_path: Path) -> None:
    """`_finish()`'s audit filename for a non-plan turn (answer/ask/refuse)
    used to be `turn-{len(self.agent.turns)}` -- no task_id_prefix at all.
    Two tabs' first such turn both landed on "turn-1", one silently
    overwriting the other's audit record. Found by a full-codebase audit
    right after the multi-tab feature shipped, reproduced directly against
    two live Harness instances before this test was written."""
    harness_a = Harness(
        workdir=tmp_path, ollama=OllamaClient(transport=scripted(
            {"intent": "answer", "message": "reply for a"})))
    harness_b = Harness(
        workdir=tmp_path, ollama=OllamaClient(transport=scripted(
            {"intent": "answer", "message": "reply for b"})))
    harness_a.agent.task_id_prefix = "session-a-"
    harness_b.agent.task_id_prefix = "session-b-"

    result_a = harness_a.plan("what is the disk usage")
    result_b = harness_b.plan("what is the memory usage")

    assert result_a.audit_path != result_b.audit_path
    on_disk = json.loads(Path(result_a.audit_path).read_text(encoding="utf-8"))
    assert on_disk["request"] == "what is the disk usage"


def test_a_new_plan_call_starts_a_fresh_decision_trail(tmp_path: Path) -> None:
    """A `Harness` reused across many turns (a long-lived TUI tab) used to
    accumulate every prior turn's decisions into each new turn's audit
    record and result.decisions, growing without bound for the tab's whole
    life. plan() now clears the trail as its first act; execute() of the
    *same* task must still see that task's own plan-stage decisions."""
    harness = Harness(
        workdir=tmp_path, ollama=OllamaClient(transport=scripted(
            {"intent": "answer", "message": "first"},
            {"intent": "answer", "message": "second"})))

    result1 = harness.plan("first request")
    assert len(result1.decisions) == 1

    result2 = harness.plan("second request")
    assert len(result2.decisions) == 1, \
        "turn 2's decisions must not include turn 1's"


def test_plan_then_execute_of_the_same_task_still_share_one_decision_trail(
        make_harness, tmp_path: Path) -> None:
    """plan()'s fresh-trail clear() must not also wipe out plan-stage
    decisions by the time the *same* task reaches execute() -- only a new
    plan() call is a new task boundary, not execute() continuing one."""
    marker = tmp_path / "made"
    harness = make_harness(scripted(plan_reply(
        "t", f"create {marker}", [{"id": "touch", "run": f"touch {marker}"}])))

    planned = harness.plan(f"create {marker}")
    assert len(planned.decisions) >= 1, "plan-stage decisions must be recorded"
    plan_stage_count = len(planned.decisions)

    done = harness.execute(planned, {})
    assert len(done.decisions) >= plan_stage_count, \
        "execute() must build on plan()'s trail for the same task, not replace it"
