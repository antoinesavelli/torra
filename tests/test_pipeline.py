"""
The seven stages end to end, and the TUI's contract with them.

These are the tests that would have caught "the harness has no LLM": every one
drives a real Harness with a real Agent against a scripted backend, so a model
that is never called shows up as a failure rather than as six phases of work.
"""

from __future__ import annotations

import json
from pathlib import Path

from conftest import plan_reply, scripted

from harness.core import TaskSpec
from harness.gates import Precondition, StateVerifier
from harness.pipeline import Outcome, Stage

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


def test_a_question_is_answered_not_planned(make_harness) -> None:
    harness = make_harness(scripted(
        {"intent": "answer", "message": "40 GB free on /, 60% used."}))
    result = harness.plan("how much disk is free")

    assert result.outcome is Outcome.ANSWERED
    assert result.plan is None
    assert "40 GB" in result.message


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
