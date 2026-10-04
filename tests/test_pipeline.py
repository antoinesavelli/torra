"""
The seven stages end to end, and the TUI's contract with them.

These are the tests that would have caught "torra has no LLM": every one
drives a real Torra with a real Agent against a scripted backend, so a model
that is never called shows up as a failure rather than as six phases of work.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import plan_reply, scripted

from torra.core import ConflictDetector, OllamaClient, OllamaError, RecoveryOption, Step, TaskSpec
from torra.core.resume import ResumePoint, command_digest
from torra.gates import (
    DecisionLog,
    Precondition,
    RiskLevel,
    SafetyClassifier,
    StateVerifier,
    UndoAction,
    UndoGenerator,
    Verdict,
    VerifiedRollback,
)
from torra.pipeline import Torra
from torra.pipeline_rollback import declared_undos, sweep
from torra.pipeline_types import Outcome, Plan, RunResult, Stage, StepPlan

SAFE_PLAN = plan_reply(
    "write-config", "set workers to 4 in /tmp/h7/app.conf",
    [{"id": "mkdir", "run": "mkdir -p /tmp/h7", "estimated_seconds": 2},
     {"id": "write", "kind": "write",
      "run": "printf 'workers = 4\\n' > /tmp/h7/app.conf",
      "touches": ["/tmp/h7/app.conf"], "estimated_seconds": 3}])


# --------------------------------------------------------------------------- #
# The happy path
# --------------------------------------------------------------------------- #


def test_all_seven_stages_in_order(make_torra, tmp_path: Path) -> None:
    """The stage contract, which the TUI and the CLI both depend on."""
    target = tmp_path / "app.conf"
    torra = make_torra(scripted(plan_reply(
        "t", f"write the worker count to {target}",
        [{"id": "w", "kind": "write", "run": f"printf 'workers = 4\\n' > {target}",
          "touches": [str(target)]}])))

    planned = torra.plan(f"set the worker count to 4 in {target}")
    assert planned.outcome is Outcome.AWAITING_APPROVAL
    assert planned.stages == ["converse", "observe", "propose", "gate", "present"]

    done = torra.execute(planned, approvals={})

    assert done.outcome is Outcome.COMPLETED
    assert done.stages == [s.value for s in Stage]
    assert target.read_text(encoding="utf-8").strip() == "workers = 4"
    assert Path(done.audit_path).exists()


def test_the_audit_records_the_model_and_the_gates(make_torra,
                                                   tmp_path: Path) -> None:
    """An audit that omits which model proposed what is not an audit."""
    target = tmp_path / "a.conf"
    torra = make_torra(scripted(plan_reply(
        "t", f"write a marker into {target}",
        [{"id": "w", "run": f"touch {target}", "touches": [str(target)]}])))

    done = torra.execute(torra.plan(f"create {target}"), {})
    payload = json.loads(Path(done.audit_path).read_text(encoding="utf-8"))

    assert payload["outcome"] == "completed"
    assert payload["turn"]["model"]
    assert payload["turn"]["intent"] == "plan"
    assert payload["plan"]["steps"][0]["verdict"]["level"]
    assert payload["plan"]["steps"][0]["undos"]
    assert payload["plan"]["coverage"]["coverage_ratio"] == 1.0
    stages = {d["stage"] for d in payload["decisions"]}
    assert {"converse", "gate", "execute"} <= stages


def test_every_finished_turn_is_recorded_to_telemetry(make_torra,
                                                       tmp_path: Path) -> None:
    """Found in a full-file audit: `torra.telemetry.records.TaskLogger`
    had a working `record()` and no caller anywhere in pipeline.py,
    agent.py, or tui.py -- `torra label`/`torra evidence` (cli.py)
    only ever read tasks.jsonl, and nothing ever wrote to it. Every
    `_finish()` call that writes an audit record must also log one here,
    including turns that never reach execute() at all."""
    target = tmp_path / "marker"
    torra = make_torra(scripted(
        {"intent": "answer", "message": "just a question"},
        plan_reply("t", f"touch {target}",
                  [{"id": "touch", "run": f"touch {target}"}])))

    answered = torra.plan("what is this machine")
    assert answered.outcome is Outcome.ANSWERED
    planned = torra.plan(f"create {target}")
    torra.execute(planned, {})

    records = torra.telemetry.read()
    assert len(records) == 2
    assert records[0].status == "answered"
    assert records[1].status == "completed"
    assert records[1].intent == "plan"


def test_the_telemetry_record_carries_the_anchors_the_clarifier_found(
        make_torra, tmp_path: Path) -> None:
    """Found in a full-file audit: TaskRecord.clarifier_anchors was
    declared but from_result() never assigned it, so every persisted
    record carried an empty list. LoopholeAudit only worked because it
    falls back to recomputing anchors from the request text -- this
    checks the field itself is now populated, so the log is
    self-contained without needing that fallback."""
    target = tmp_path / "app.conf"
    torra = make_torra(scripted(plan_reply(
        "t", f"write to {target}",
        [{"id": "w", "run": f"touch {target}", "touches": [str(target)]}])))

    torra.execute(torra.plan(f"write to {target}"), {})

    records = torra.telemetry.read()
    assert records[-1].clarifier_anchors, (
        f"an absolute path in {target!r} should have registered as an anchor"
    )
    assert "absolute path" in records[-1].clarifier_anchors


def test_a_precondition_blocked_plan_is_not_recorded_as_approved(
        make_torra, tmp_path: Path) -> None:
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
    torra = make_torra(scripted(plan_reply(
        "t", f"create {marker}", [{"id": "t", "run": f"touch {marker}"}])),
        verifier=verifier)

    result = torra.plan(f"create {marker}")

    assert result.outcome is Outcome.BLOCKED
    records = torra.telemetry.read()
    assert records[-1].approved is False


def test_a_normally_completed_plan_is_still_recorded_as_approved(
        make_torra, tmp_path: Path) -> None:
    target = tmp_path / "app.conf"
    torra = make_torra(scripted(plan_reply(
        "t", f"write to {target}",
        [{"id": "w", "run": f"touch {target}", "touches": [str(target)]}])))

    torra.execute(torra.plan(f"write to {target}"), {})

    assert torra.telemetry.read()[-1].approved is True


# --------------------------------------------------------------------------- #
# Approval
# --------------------------------------------------------------------------- #


def test_a_dangerous_step_does_not_run_unapproved(make_torra,
                                                  tmp_path: Path) -> None:
    """The whole point. An unapproved dangerous command is refused, not defaulted."""
    victim = tmp_path / "victim.txt"
    victim.write_text("still here", encoding="utf-8")
    torra = make_torra(scripted(plan_reply(
        "t", f"remove {victim}",
        [{"id": "rm", "run": f"rm -f {victim}", "touches": [str(victim)]}])))

    planned = torra.plan(f"delete {victim}")
    assert planned.plan is not None
    assert planned.plan.needs_confirmation, "rm should require confirmation"

    done = torra.execute(planned, approvals={})

    assert done.outcome is Outcome.REJECTED
    assert victim.exists(), "an unapproved command ran"
    assert done.results == []


def test_an_approved_step_runs(make_torra, tmp_path: Path) -> None:
    """Approval is per step id, and it is honoured."""
    victim = tmp_path / "victim.txt"
    victim.write_text("goodbye", encoding="utf-8")
    torra = make_torra(scripted(plan_reply(
        "t", f"remove {victim}",
        [{"id": "rm", "run": f"rm -f {victim}", "touches": [str(victim)]}])))

    planned = torra.plan(f"delete {victim}")
    done = torra.execute(planned, approvals={"rm": True})

    assert done.outcome is Outcome.COMPLETED
    assert not victim.exists()


def test_a_mixed_approval_runs_the_approved_steps_and_skips_the_declined_one(
        make_torra, tmp_path: Path) -> None:
    """The point of partial execution: one declined step used to reject
    the whole plan, including a sibling step that was already approved.
    Now the approved one runs and the declined one is skipped, not run."""
    keep = tmp_path / "keep.txt"
    delete = tmp_path / "delete.txt"
    keep.write_text("stay", encoding="utf-8")
    delete.write_text("go", encoding="utf-8")
    torra = make_torra(scripted(plan_reply(
        "t", "delete two files",
        [{"id": "rm-keep", "run": f"rm -f {keep}", "touches": [str(keep)]},
         {"id": "rm-delete", "run": f"rm -f {delete}", "touches": [str(delete)]}])))

    planned = torra.plan("delete two files")
    done = torra.execute(planned, approvals={"rm-keep": False, "rm-delete": True})

    assert done.outcome is Outcome.COMPLETED
    assert keep.exists(), "the declined step must not have run"
    assert not delete.exists(), "the approved step must still run"
    skipped = [r for r in done.results if r.step_id == "rm-keep"]
    assert skipped and "[skipped] not approved" in skipped[0].output
    ran = [r for r in done.results if r.step_id == "rm-delete"]
    assert ran and ran[0].ok


def test_declining_every_step_in_a_multi_step_plan_still_rejects_all_of_it(
        make_torra, tmp_path: Path) -> None:
    """Generalizes test_a_dangerous_step_does_not_run_unapproved to more
    than one step: REJECTED, with empty results, is only for the case
    where *nothing* ends up approved -- not just any single refusal."""
    v1, v2 = tmp_path / "v1.txt", tmp_path / "v2.txt"
    v1.write_text("a", encoding="utf-8")
    v2.write_text("b", encoding="utf-8")
    torra = make_torra(scripted(plan_reply(
        "t", "delete two files",
        [{"id": "rm1", "run": f"rm -f {v1}", "touches": [str(v1)]},
         {"id": "rm2", "run": f"rm -f {v2}", "touches": [str(v2)]}])))

    planned = torra.plan("delete two files")
    done = torra.execute(planned, approvals={})

    assert done.outcome is Outcome.REJECTED
    assert done.results == []
    assert v1.exists() and v2.exists()


def test_a_declined_steps_undo_is_not_staged_for_a_later_rollback_sweep(
        make_torra, tmp_path: Path) -> None:
    """_stage_rollback used to push every step's undo unconditionally --
    including a declined step that never ran. If an approved step further
    along then failed and triggered a rollback sweep, it would try to
    "restore" a file the declined step never touched in the first place.
    Needs a real undo actually staged (from the approved write step) or
    the sweep has nothing to report at all and the assertion proves
    nothing -- the declined step's rm alone would leave rollback empty."""
    written = tmp_path / "written.txt"
    written.write_text("original", encoding="utf-8")
    declined_victim = tmp_path / "declined.txt"
    declined_victim.write_text("never touched", encoding="utf-8")
    torra = make_torra(scripted(plan_reply(
        "t", "write, a declined delete, then a step that fails",
        [{"id": "write", "kind": "write",
          "run": f"printf 'changed\\n' > {written}", "touches": [str(written)]},
         {"id": "rm-declined", "run": f"rm -f {declined_victim}",
          "touches": [str(declined_victim)]},
         {"id": "will-fail", "run": "exit 1"}])))

    planned = torra.plan("mixed run")
    done = torra.execute(
        planned, approvals={"rm-declined": False, "will-fail": True})

    assert done.outcome is Outcome.FAILED
    assert declined_victim.exists(), "the declined step's file must be untouched"
    assert done.rollback is not None and done.rollback.results, \
        "the write step's real undo must have been staged and swept"
    descriptions = " ".join(r.description for r in done.rollback.results)
    assert str(declined_victim) not in descriptions, \
        "a declined step's undo must never be staged, let alone attempted"


def test_dependency_warnings_flags_an_approved_step_using_a_declined_paths_output(
        make_torra) -> None:
    """Best-effort, not a guarantee -- a plain substring check against
    `touches`, computed before execute() so the caller can show it while
    the operator can still change their mind. The declined step must
    genuinely need confirmation, or it is never "declined" at all --
    `dependency_warnings` treats a safe step as approved regardless of
    what the approvals dict says, same as `_apply_approvals` itself."""
    torra = make_torra(scripted(plan_reply(
        "t", "remove then reference it",
        [{"id": "rm-declined", "run": "rm -f /tmp/scratch-dir/data.txt",
          "touches": ["/tmp/scratch-dir/data.txt"]},
         {"id": "use-it", "run": "cat /tmp/scratch-dir/data.txt"}])))

    planned = torra.plan("remove then reference it")
    assert planned.plan is not None and planned.plan.needs_confirmation, \
        "rm must actually need confirmation for this test to mean anything"
    warnings = Torra.dependency_warnings(
        planned.plan, approvals={"rm-declined": False, "use-it": True})

    assert len(warnings) == 1
    assert "use-it" in warnings[0]
    assert "/tmp/scratch-dir/data.txt" in warnings[0]
    assert "rm-declined" in warnings[0]


def test_dependency_warnings_is_empty_when_nothing_was_declined(
        make_torra) -> None:
    torra = make_torra(scripted(plan_reply(
        "t", "two safe steps",
        [{"id": "a", "run": "echo one"}, {"id": "b", "run": "echo two"}])))

    planned = torra.plan("two safe steps")

    assert Torra.dependency_warnings(planned.plan, approvals={}) == []


def test_dependency_warnings_is_empty_when_no_approved_step_references_the_path(
        make_torra) -> None:
    torra = make_torra(scripted(plan_reply(
        "t", "unrelated steps",
        [{"id": "rm-declined", "run": "rm -f /tmp/scratch-dir/x.txt",
          "touches": ["/tmp/scratch-dir/x.txt"]},
         {"id": "unrelated", "run": "echo hello"}])))

    planned = torra.plan("unrelated steps")
    assert planned.plan is not None and planned.plan.needs_confirmation
    warnings = Torra.dependency_warnings(
        planned.plan, approvals={"rm-declined": False, "unrelated": True})

    assert warnings == []


def test_dependency_warnings_module_works_standalone_not_just_via_torra(
        make_torra) -> None:
    """`Torra.dependency_warnings` is `staticmethod(_dependency_warnings)`
    from torra.dependency_warnings -- imported and called directly here
    so the split-out module is exercised on its own, not only through the
    Torra re-export every other test in this file goes through."""
    from torra.dependency_warnings import dependency_warnings

    torra = make_torra(scripted(plan_reply(
        "t", "remove then reference it",
        [{"id": "rm-declined", "run": "rm -f /tmp/scratch-dir/data.txt",
          "touches": ["/tmp/scratch-dir/data.txt"]},
         {"id": "use-it", "run": "cat /tmp/scratch-dir/data.txt"}])))

    planned = torra.plan("remove then reference it")
    warnings = dependency_warnings(
        planned.plan, approvals={"rm-declined": False, "use-it": True})

    assert len(warnings) == 1
    assert "use-it" in warnings[0] and "rm-declined" in warnings[0]


def test_on_output_receives_live_chunks_tagged_with_the_step_id(
        make_torra, tmp_path: Path) -> None:
    """The mechanism behind the TUI's live step-output view -- see
    HANDOFF.md's entry on shell_output_sink for why this has to be an
    execute()-time callback rather than something baked into the step at
    plan time."""
    torra = make_torra(scripted(plan_reply(
        "t", "say hello",
        [{"id": "greet", "run": "echo hello; echo world"}])))

    planned = torra.plan("say hello")
    seen: list[tuple[str, str]] = []
    done = torra.execute(planned, {}, on_output=lambda step_id, chunk: seen.append((step_id, chunk)))

    assert done.outcome is Outcome.COMPLETED
    assert all(step_id == "greet" for step_id, _ in seen)
    assert "".join(chunk for _, chunk in seen) == "hello\nworld\n"


def test_on_output_tags_each_step_with_its_own_id(make_torra, tmp_path: Path) -> None:
    torra = make_torra(scripted(plan_reply(
        "t", "two steps",
        [{"id": "first", "run": "echo one"},
         {"id": "second", "run": "echo two"}])))

    planned = torra.plan("two steps")
    seen: list[tuple[str, str]] = []
    done = torra.execute(planned, {}, on_output=lambda step_id, chunk: seen.append((step_id, chunk)))

    assert done.outcome is Outcome.COMPLETED
    assert seen == [("first", "one\n"), ("second", "two\n")]


def test_on_output_is_never_called_for_a_dry_run(make_torra, tmp_path: Path) -> None:
    """Dry-run steps never call shell() at all -- they return a
    "[dry-run] ..." placeholder directly, so there is nothing for
    shell_output_sink to intercept."""
    torra = make_torra(scripted(plan_reply(
        "t", "say hello", [{"id": "greet", "run": "echo hello"}])))

    planned = torra.plan("say hello")
    seen: list[tuple[str, str]] = []
    torra.execute(planned, {}, dry_run=True, on_output=lambda step_id, chunk: seen.append((step_id, chunk)))

    assert seen == []


def test_a_failing_step_still_streams_its_output_before_raising(
        make_torra, tmp_path: Path) -> None:
    torra = make_torra(scripted(plan_reply(
        "t", "fail loudly", [{"id": "boom", "run": "echo trying; exit 1"}])))

    planned = torra.plan("fail loudly")
    seen: list[tuple[str, str]] = []
    done = torra.execute(planned, {}, on_output=lambda step_id, chunk: seen.append((step_id, chunk)))

    assert done.outcome is Outcome.FAILED
    assert seen == [("boom", "trying\n")]


def test_a_safe_step_needs_no_tick(make_torra, tmp_path: Path) -> None:
    """Asking for approval on `df -h` trains people to tick without reading."""
    torra = make_torra(scripted(plan_reply(
        "t", "report disk usage on /",
        [{"id": "df", "run": "df -h /"}])))

    planned = torra.plan("report disk usage on /")
    assert planned.plan is not None
    assert planned.plan.needs_confirmation == []
    assert torra.execute(planned, {}).outcome is Outcome.COMPLETED


def test_reject_never_runs_an_all_safe_plan(make_torra, tmp_path: Path) -> None:
    """Found in a full-file audit: the TUI's Reject button used to bypass
    the pipeline entirely (self.current = None, no audit, no telemetry).
    Routing it through execute(result, {}) instead would have been wrong
    in a different way -- a safe step's `approved` defaults to True
    regardless of the approvals dict, so an all-safe plan would actually
    *run* on "reject" rather than being declined. Torra.reject() must
    refuse unconditionally, independent of what would have needed a tick."""
    marker = tmp_path / "marker"
    torra = make_torra(scripted(plan_reply(
        "t", f"touch {marker}", [{"id": "touch", "run": f"touch {marker}"}])))

    planned = torra.plan(f"create {marker}")
    assert planned.plan is not None and planned.plan.needs_confirmation == []

    done = torra.reject(planned)

    assert done.outcome is Outcome.REJECTED
    assert not marker.exists(), "reject() ran an all-safe plan instead of declining it"
    assert done.audit_path
    records = torra.telemetry.read()
    assert records and records[-1].status == "rejected"


# --------------------------------------------------------------------------- #
# Failure and recovery
# --------------------------------------------------------------------------- #


def test_a_failed_step_reverts_a_file_nobody_registered(make_torra,
                                                        tmp_path: Path) -> None:
    """No caller-supplied undo anywhere, and the file still comes back."""
    conf = tmp_path / "app.conf"
    conf.write_text("ORIGINAL\n", encoding="utf-8")
    torra = make_torra(scripted(plan_reply(
        "t", f"rewrite {conf} then verify the service",
        [{"id": "write", "kind": "write", "run": f"printf 'NEW\\n' > {conf}",
          "touches": [str(conf)]},
         {"id": "boom", "run": "exit 7"}])))

    done = torra.execute(torra.plan(f"rewrite {conf} and verify"), {})

    assert done.outcome is Outcome.FAILED
    assert conf.read_text(encoding="utf-8") == "ORIGINAL\n"
    assert done.rollback is not None and done.rollback.ok
    assert all(r.verified is not False for r in done.rollback.results)


def test_a_successful_run_cleans_up_its_shadow_copies(make_torra,
                                                       tmp_path: Path) -> None:
    """Found in a full-file audit: UndoGenerator.cleanup() had no caller
    anywhere, so every run that snapshotted a file left its shadow
    directory on disk forever (14 accumulated on one real machine)."""
    conf = tmp_path / "app.conf"
    conf.write_text("original\n", encoding="utf-8")
    torra = make_torra(scripted(plan_reply(
        "t", f"rewrite {conf}",
        [{"id": "write", "kind": "write", "run": f"printf 'new\\n' > {conf}",
          "touches": [str(conf)]}])))

    done = torra.execute(torra.plan(f"rewrite {conf}"), {})

    assert done.outcome is Outcome.COMPLETED
    assert not any(torra.undo.shadow_dir.iterdir()), (
        "a completed run left its shadow copies behind")


def test_a_dry_run_keeps_its_shadow_copies(make_torra, tmp_path: Path) -> None:
    """A dry run's own snapshots (taken at propose time, before dry_run is
    even known) must survive it -- clicking Run right after still needs
    them for real rollback."""
    conf = tmp_path / "app.conf"
    conf.write_text("original\n", encoding="utf-8")
    torra = make_torra(scripted(plan_reply(
        "t", f"rewrite {conf}",
        [{"id": "write", "kind": "write", "run": f"printf 'new\\n' > {conf}",
          "touches": [str(conf)]}])))

    done = torra.execute(torra.plan(f"rewrite {conf}"), {}, dry_run=True)

    assert done.outcome is Outcome.COMPLETED
    assert any(torra.undo.shadow_dir.iterdir()), (
        "a dry run deleted the snapshots the real run would need"
    )


def test_a_completed_read_steps_output_feeds_back_into_agent_context(
        make_torra, tmp_path: Path) -> None:
    """§2.46: a step declaring `reads` gets its real output remembered,
    tagged by the path -- so a later turn can act on what it found
    instead of only remembering that the step was proposed."""
    target = tmp_path / "quirks.md"
    target.write_text("quirk #12: something specific", encoding="utf-8")
    torra = make_torra(scripted(plan_reply(
        "t", "read the quirks file",
        [{"id": "read", "run": f"cat {target}", "reads": [str(target)]}])))

    done = torra.execute(torra.plan("read the quirks file"), {})

    assert done.outcome is Outcome.COMPLETED
    assert any(str(target) in e.content and "quirk #12" in e.content
              for e in torra.agent.context.entries)


def test_a_dry_run_never_feeds_read_output_into_context(
        make_torra, tmp_path: Path) -> None:
    """A dry run's output is a `[dry-run] ...` placeholder, not real
    content -- feeding that into the model's memory as if it were the
    file's real content would be actively wrong, not just unhelpful.
    Checks for the distinctive `remember_read` tag, not a bare path
    substring -- see test_a_failed_read_steps_output_never_feeds_into_context
    for why a bare substring check would pass even when remember_read()
    correctly never fired."""
    target = tmp_path / "quirks.md"
    target.write_text("quirk #12: something specific", encoding="utf-8")
    torra = make_torra(scripted(plan_reply(
        "t", "read the quirks file",
        [{"id": "read", "run": f"cat {target}", "reads": [str(target)]}])))

    done = torra.execute(torra.plan("read the quirks file"), {}, dry_run=True)

    assert done.outcome is Outcome.COMPLETED
    assert not any(f"[read {target}]" in e.content for e in torra.agent.context.entries)


def test_a_failed_read_steps_output_never_feeds_into_context(
        make_torra) -> None:
    """Checks for the distinctive `remember_read` tag, not a bare path
    substring -- the path also legitimately appears in _remember()'s own
    "Proposed: read: cat /no/such/file" summary line regardless of
    whether the step ever ran, which would make a bare substring check
    pass even when remember_read() correctly never fired."""
    torra = make_torra(scripted(plan_reply(
        "t", "read a file that fails",
        [{"id": "read", "run": "cat /no/such/file", "reads": ["/no/such/file"]}])))

    done = torra.execute(torra.plan("read a file that fails"), {})

    assert done.outcome is Outcome.FAILED
    assert not any("[read /no/such/file]" in e.content for e in torra.agent.context.entries)


def test_a_declared_safe_rollback_is_still_classified(make_torra,
                                                       tmp_path: Path) -> None:
    """A declared `safe: true` cannot suppress a real CRITICAL classification.

    Before this was fixed, the exact command below ran automatically during
    the rollback sweep with zero classification and zero confirmation --
    see HANDOFF.md §6.4. Now it must be auto-declined (the default
    confirm_unsafe with nobody wired in to approve it), not silently run.
    """
    target = tmp_path / "should-survive"
    target.mkdir()
    torra = make_torra(scripted(plan_reply(
        "t", "provision then fail",
        [{"id": "boom", "run": "exit 7"}],
        rollback=[{"run": f"rm -rf {target}", "description": "cleanup",
                  "safe": True}])))

    done = torra.execute(torra.plan("provision then fail"), {})

    assert done.outcome is Outcome.FAILED
    assert done.rollback is not None
    assert [r.status for r in done.rollback.results] == ["cancelled"]
    assert target.exists(), "a CRITICAL command ran despite being declared safe"
    overrides = [d for d in done.decisions if d["decision"] == "rollback safety override"]
    assert len(overrides) == 1


class TestResume:
    """HANDOFF.md §6.4: ResumePoint.load() finally has a caller. A crash
    mid-task used to mean a retry re-ran every step from scratch, including
    ones that already had real side effects -- these prove a retry now
    skips genuinely-completed steps, and prove the several ways a resume
    point must be distrusted rather than a bare "file exists" check."""

    @staticmethod
    def _resume_path(tmp_path: Path, task_id: str) -> Path:
        return tmp_path / "wd" / "resume" / f"{task_id}.json"

    def test_a_resumed_run_skips_already_completed_steps(
            self, make_torra, tmp_path: Path) -> None:
        counter = tmp_path / "counter"
        counter.write_text("x", encoding="utf-8")  # the crashed attempt's real effect
        done = tmp_path / "done"
        torra = make_torra(scripted(plan_reply(
            "t", "two steps",
            [{"id": "first", "run": f"printf x >> {counter}"},
             {"id": "second", "run": f"touch {done}"}])))
        planned = torra.plan("two steps")

        resume_path = self._resume_path(tmp_path, "t")
        ResumePoint(task_id="t", completed=["first"], total=2,
                    digests={"first": command_digest(f"printf x >> {counter}")}).save(resume_path)

        result = torra.execute(planned, {})

        assert result.outcome is Outcome.COMPLETED
        assert counter.read_text(encoding="utf-8") == "x", \
            "the resumed step must not have run its command again"
        assert done.exists(), "the never-attempted step must still run for real"
        assert result.executed is True
        first, second = result.results
        assert first.ok is True and first.output == "[resumed] printf x >> " + str(counter)
        assert second.output.strip() == ""  # touch prints nothing -- ran for real

    def test_a_mismatched_step_sequence_is_not_trusted(
            self, make_torra, tmp_path: Path) -> None:
        """The saved resume point claims a step id this plan doesn't even
        have at that position -- evidence of an edited task file or an
        unrelated plan reusing the id, not a genuine retry."""
        marker = tmp_path / "marker"
        torra = make_torra(scripted(plan_reply(
            "t", "one step", [{"id": "real-step", "run": f"touch {marker}"}])))
        planned = torra.plan("one step")

        resume_path = self._resume_path(tmp_path, "t")
        ResumePoint(task_id="t", completed=["a-different-step-id"], total=1).save(resume_path)

        result = torra.execute(planned, {})

        assert result.outcome is Outcome.COMPLETED
        assert marker.exists(), "the mismatched resume point must be ignored, not trusted"
        assert result.results[0].output != "[resumed] " + result.results[0].output

    def test_a_resume_point_without_digests_is_not_trusted(
            self, make_torra, tmp_path: Path) -> None:
        """The older format named completed steps but not what they ran."""
        marker = tmp_path / "marker"
        torra = make_torra(scripted(plan_reply(
            "t", "one step", [{"id": "s", "run": f"touch {marker}"}])))
        planned = torra.plan("one step")
        ResumePoint(task_id="t", completed=["s"], total=1).save(self._resume_path(tmp_path, "t"))

        result = torra.execute(planned, {})

        assert marker.exists() and not result.results[0].output.startswith("[resumed]")

    def test_a_step_whose_command_changed_is_not_vouched_for(
            self, make_torra, tmp_path: Path) -> None:
        marker = tmp_path / "marker"
        torra = make_torra(scripted(plan_reply(
            "t", "one step", [{"id": "s", "run": f"touch {marker}"}])))
        planned = torra.plan("one step")
        ResumePoint(task_id="t", completed=["s"], total=1,
                    digests={"s": command_digest("something else entirely")}
                    ).save(self._resume_path(tmp_path, "t"))

        result = torra.execute(planned, {})

        assert marker.exists() and not result.results[0].output.startswith("[resumed]")

    def test_a_step_left_in_flight_by_a_crash_is_never_rerun_automatically(
            self, make_torra, tmp_path: Path) -> None:
        """The process died while `second` was running: its effect is unknown, so the next
        attempt stops and says so rather than running it again."""
        done = tmp_path / "done"
        torra = make_torra(scripted(plan_reply(
            "t", "two steps",
            [{"id": "first", "run": "true"},
             {"id": "second", "run": f"touch {done}"}])))
        planned = torra.plan("two steps")
        resume_path = self._resume_path(tmp_path, "t")
        ResumePoint(task_id="t", completed=["first"], total=2, in_progress="second",
                    digests={"first": command_digest("true")}).save(resume_path)

        result = torra.execute(planned, {})

        assert result.outcome is Outcome.BLOCKED
        assert "was running when torra stopped" in result.abort_reason
        assert str(resume_path) in result.abort_reason
        assert not done.exists()
        assert ResumePoint.load(resume_path) is not None, "the record of what finished is kept"

    def test_the_resume_file_marks_a_step_in_flight_while_it_runs(
            self, make_torra, tmp_path: Path) -> None:
        resume_path = self._resume_path(tmp_path, "t")
        seen: list[str] = []
        torra = make_torra(scripted(plan_reply(
            "t", "one step", [{"id": "s", "run": "true"}])))
        planned = torra.plan("one step")
        real = planned.plan.steps[0].step.run  # type: ignore[union-attr]

        def watching() -> str:
            point = ResumePoint.load(resume_path)
            seen.append(point.in_progress if point else "<no file>")
            return str(real())  # type: ignore[misc]

        planned.plan.steps[0].step.run = watching  # type: ignore[union-attr]
        torra.execute(planned, {})

        assert seen == ["s"]

    def test_a_failed_step_clears_its_in_flight_mark_and_keeps_the_finished_ones(
            self, make_torra, tmp_path: Path) -> None:
        resume_path = self._resume_path(tmp_path, "t")
        torra = make_torra(scripted(plan_reply(
            "t", "two steps", [{"id": "ok", "run": "true"}, {"id": "bad", "run": "exit 1"}])))

        done = torra.execute(torra.plan("two steps"), {})

        assert done.outcome is Outcome.FAILED
        point = ResumePoint.load(resume_path)
        assert point is not None and point.in_progress == "" and point.completed == ["ok"]

    def test_a_mismatched_total_is_not_trusted(
            self, make_torra, tmp_path: Path) -> None:
        marker = tmp_path / "marker"
        torra = make_torra(scripted(plan_reply(
            "t", "one step", [{"id": "s", "run": f"touch {marker}"}])))
        planned = torra.plan("one step")

        resume_path = self._resume_path(tmp_path, "t")
        # Same first step id, but claims a 3-step plan -- a differently
        # shaped attempt, not this one, even though "s" happens to match.
        ResumePoint(task_id="t", completed=["s"], total=3).save(resume_path)

        result = torra.execute(planned, {})

        assert result.outcome is Outcome.COMPLETED
        assert not result.results[0].output.startswith("[resumed]")

    def test_a_corrupt_resume_point_is_ignored_not_fatal(
            self, make_torra, tmp_path: Path) -> None:
        marker = tmp_path / "marker"
        torra = make_torra(scripted(plan_reply(
            "t", "one step", [{"id": "s", "run": f"touch {marker}"}])))
        planned = torra.plan("one step")

        resume_path = self._resume_path(tmp_path, "t")
        resume_path.parent.mkdir(parents=True, exist_ok=True)
        resume_path.write_text("not json at all", encoding="utf-8")

        result = torra.execute(planned, {})

        assert result.outcome is Outcome.COMPLETED
        assert marker.exists()

    def test_a_dry_run_never_writes_a_resume_point(
            self, make_torra, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = []
        monkeypatch.setattr(ResumePoint, "save",
                            lambda self, path: calls.append(path))
        torra = make_torra(scripted(plan_reply(
            "t", "two steps",
            [{"id": "a", "run": "true"}, {"id": "b", "run": "true"}])))
        planned = torra.plan("two steps")

        torra.execute(planned, {}, dry_run=True)

        assert calls == [], \
            "a dry run must never persist a resume point -- a crash mid-preview " \
            "must not later look like steps that genuinely ran"

    def test_a_dry_run_ignores_an_existing_resume_point(
            self, make_torra, tmp_path: Path) -> None:
        """A dry run previews every step regardless of what a resume point
        claims -- silently skipping some would defeat the point of asking
        "what would this plan do"."""
        torra = make_torra(scripted(plan_reply(
            "t", "one step", [{"id": "s", "run": "echo hi"}])))
        planned = torra.plan("one step")

        resume_path = self._resume_path(tmp_path, "t")
        ResumePoint(task_id="t", completed=["s"], total=1).save(resume_path)

        result = torra.execute(planned, {}, dry_run=True)

        assert result.results[0].output == "[dry-run] echo hi"

    def test_a_failed_run_with_rollback_deletes_the_resume_point(
            self, make_torra, tmp_path: Path) -> None:
        """A resume point surviving a rollback would let a later retry skip
        steps whose effects were just undone -- worse than not resuming at
        all. `_sweep_rollback` clears it the same as a clean success does."""
        target = tmp_path / "f.txt"
        torra = make_torra(scripted(plan_reply(
            "t", "one step",
            [{"id": "write", "kind": "write", "run": f"printf x > {target}",
              "touches": [str(target)]},
             {"id": "fail", "run": "false"}])))
        planned = torra.plan("one step")

        result = torra.execute(planned, {})

        assert result.outcome is Outcome.FAILED
        assert not self._resume_path(tmp_path, "t").exists()

    def test_a_completed_run_leaves_no_resume_point_for_a_future_retry_to_find(
            self, make_torra, tmp_path: Path) -> None:
        marker = tmp_path / "marker"
        torra = make_torra(scripted(plan_reply(
            "t", "one step", [{"id": "s", "run": f"touch {marker}"}])))
        planned = torra.plan("one step")

        torra.execute(planned, {})

        assert not self._resume_path(tmp_path, "t").exists()


def test_execute_twice_on_the_same_result_does_not_replay_it(make_torra,
                                                              tmp_path: Path) -> None:
    """Found live in an audit log: stages [...,"execute","report","execute"]
    and two result entries for one step. The TUI's Run button stays
    clickable mid-run, so a double-click dispatched execute() twice on the
    same already-completed RunResult, re-running every step's command a
    second time. A read-only `du` is merely wasteful to repeat; anything
    that mutates state would not be."""
    marker = tmp_path / "marker"
    torra = make_torra(scripted(plan_reply(
        "t", f"touch {marker}",
        [{"id": "touch", "run": f"touch {marker}"}])))

    planned = torra.plan(f"create {marker}")
    once = torra.execute(planned, {})
    assert once.outcome is Outcome.COMPLETED
    assert len(once.results) == 1

    twice = torra.execute(once, {})

    assert twice.outcome is Outcome.COMPLETED
    assert len(twice.results) == 1, "the step ran a second time"
    assert twice.stages.count("execute") == 1
    refusals = [d for d in twice.decisions if d["decision"] == "refused: already executed"]
    assert len(refusals) == 1


def test_ticking_the_box_after_a_rejection_still_runs(make_torra,
                                                       tmp_path: Path) -> None:
    """Found live right after the guard above shipped: a plan clicked Run
    before its checkbox was ticked gets REJECTED (nothing ran, by design --
    see 2.3), the operator ticks the box and clicks Run again, and the
    ordinary retry has to work. Keying the replay guard on `outcome !=
    AWAITING_APPROVAL` blocked this exact, completely normal flow, because
    REJECTED is a terminal outcome too even though zero commands ran."""
    victim = tmp_path / "victim.txt"
    victim.write_text("still here", encoding="utf-8")
    torra = make_torra(scripted(plan_reply(
        "t", f"remove {victim}",
        [{"id": "rm", "run": f"rm -f {victim}", "touches": [str(victim)]}])))

    planned = torra.plan(f"delete {victim}")
    rejected = torra.execute(planned, approvals={})
    assert rejected.outcome is Outcome.REJECTED
    assert victim.exists()

    approved = torra.execute(rejected, approvals={"rm": True})

    assert approved.outcome is Outcome.COMPLETED
    assert not victim.exists()


def test_reject_refuses_to_overwrite_a_plan_that_already_ran(make_torra,
                                                              tmp_path: Path) -> None:
    """Found in a full-file audit of the TUI's own presentation layer: the
    Reject button was never disabled after a run completed (unlike
    Run/Dry-run), and Torra.reject() had no guard against being called
    on an already-executed result. A stray click on Reject after a real
    run would have silently overwritten a COMPLETED outcome with
    REJECTED and written a fresh audit record claiming "nothing ran" --
    actively misrepresenting a run that may have had real, destructive
    side effects."""
    marker = tmp_path / "marker"
    torra = make_torra(scripted(plan_reply(
        "t", f"touch {marker}", [{"id": "touch", "run": f"touch {marker}"}])))

    done = torra.execute(torra.plan(f"touch {marker}"), {})
    assert done.outcome is Outcome.COMPLETED
    assert marker.exists()

    still_done = torra.reject(done)

    assert still_done.outcome is Outcome.COMPLETED, (
        "a completed run's outcome was overwritten by a late Reject"
    )
    assert marker.exists()
    refusals = [d for d in still_done.decisions if d["decision"] == "refused: already executed"]
    assert len(refusals) == 1


def test_reject_still_works_on_a_plan_that_never_ran(make_torra,
                                                      tmp_path: Path) -> None:
    """The ordinary case must keep working: nothing executed yet, so
    Reject is free to actually reject."""
    marker = tmp_path / "marker"
    torra = make_torra(scripted(plan_reply(
        "t", f"touch {marker}", [{"id": "touch", "run": f"touch {marker}"}])))

    planned = torra.plan(f"touch {marker}")
    rejected = torra.reject(planned)

    assert rejected.outcome is Outcome.REJECTED
    assert not marker.exists()


def test_a_dry_run_does_not_block_the_real_run_that_follows(make_torra,
                                                             tmp_path: Path) -> None:
    """Previewing a plan with Dry run must not count as "already executed"
    -- that would block the real Run a dry run is meant to precede."""
    marker = tmp_path / "marker"
    torra = make_torra(scripted(plan_reply(
        "t", f"touch {marker}",
        [{"id": "touch", "run": f"touch {marker}"}])))

    planned = torra.plan(f"create {marker}")
    dry = torra.execute(planned, {}, dry_run=True)
    assert dry.outcome is Outcome.COMPLETED
    assert not marker.exists(), "a dry run touched the filesystem"

    real = torra.execute(dry, {})

    assert real.outcome is Outcome.COMPLETED
    assert marker.exists()
    assert len(real.results) == 1


def test_gate_does_not_redundantly_walk_watch_roots(make_torra,
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
    torra = make_torra(scripted(plan_reply(
        "t", "touch a file", [{"id": "w", "run": "true"}],
        watch_roots=[str(watched)])), conflicts=detector)

    torra.plan("touch a file")

    assert detector.full_walks == 0, "baseline() alone must not trigger a _reconcile() walk"


def test_a_precondition_flip_stops_execution(make_torra,
                                             tmp_path: Path) -> None:
    """The probe-12 scenario: a service dies at step one, step two must not run."""
    marker = tmp_path / "should-not-exist"
    state = {"up": True}
    verifier = StateVerifier()
    verifier.add(Precondition("svc", lambda: state["up"], "must stay up",
                              ttl_seconds=0))

    torra = make_torra(scripted(plan_reply(
        "t", "restart the api and then write a marker",
        [{"id": "kill", "run": "true"},
         {"id": "second", "run": f"touch {marker}"}])), verifier=verifier)

    planned = torra.plan("restart ollama.service on popos-01 and verify")
    state["up"] = False
    done = torra.execute(planned, {})

    assert done.outcome is Outcome.BLOCKED
    assert "precondition went false mid-run" in done.abort_reason
    assert not marker.exists()


def test_a_block_before_any_step_ran_is_still_retryable(make_torra,
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
    torra = make_torra(scripted(plan_reply(
        "t", f"touch {marker}",
        [{"id": "touch", "run": f"touch {marker}"}])), verifier=verifier)

    planned = torra.plan(f"create {marker}")
    state["up"] = False
    blocked = torra.execute(planned, {})
    assert blocked.outcome is Outcome.BLOCKED
    assert blocked.results == []

    state["up"] = True
    retried = torra.execute(blocked, {})

    assert retried.outcome is Outcome.COMPLETED
    assert marker.exists()


def test_an_external_edit_stops_the_run(make_torra, tmp_path: Path) -> None:
    """Something moved under us. Continuing would clobber a human's work."""
    conf = tmp_path / "shared.conf"
    conf.write_text("original", encoding="utf-8")
    marker = tmp_path / "second-ran"

    torra = make_torra(scripted(plan_reply(
        "t", f"touch {conf} then write a marker",
        [{"id": "first", "run": "true", "touches": [str(conf)]},
         {"id": "second", "run": f"touch {marker}", "touches": [str(conf)]}])))

    planned = torra.plan(f"update {conf} in two steps")
    conf.write_text("EDITED BY A HUMAN", encoding="utf-8")
    done = torra.execute(planned, {})

    assert done.outcome is Outcome.BLOCKED
    assert "external file change" in done.abort_reason
    assert not marker.exists()


def test_a_block_before_any_step_ran_does_not_overwrite_the_external_edit(
        make_torra, tmp_path: Path) -> None:
    """HANDOFF.md §2.59. Every approved step's undo used to be staged before
    step one ran, so a conflict block went through the rollback sweep and
    copied the plan-time snapshot back over the very edit the conflict check
    had just caught. Nothing had run, so nothing may be reversed."""
    conf = tmp_path / "shared.conf"
    conf.write_text("original", encoding="utf-8")
    marker = tmp_path / "second-ran"
    torra = make_torra(scripted(plan_reply(
        "t", f"touch {conf} then write a marker",
        [{"id": "first", "run": "true", "touches": [str(conf)]},
         {"id": "second", "run": f"touch {marker}", "touches": [str(conf)]}])))

    planned = torra.plan(f"update {conf} in two steps")
    conf.write_text("EDITED BY A HUMAN", encoding="utf-8")
    done = torra.execute(planned, {})

    assert done.outcome is Outcome.BLOCKED
    assert conf.read_text(encoding="utf-8") == "EDITED BY A HUMAN"
    assert done.rollback is None, "no step ran, so no undo may run"
    assert not marker.exists()


def test_a_block_before_step_two_reverts_step_one_and_never_step_two(
        make_torra, tmp_path: Path) -> None:
    """Undos are staged just before their own step runs, so a block at step
    two reverses step one and leaves step two's file exactly as found."""
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("a-original", encoding="utf-8")
    b.write_text("b-original", encoding="utf-8")
    torra = make_torra(scripted(plan_reply(
        "t", "write a then b",
        [{"id": "one", "kind": "write",
          "run": f"printf 'a-new' > {a} && printf 'EXTERNAL' > {b}", "touches": [str(a)]},
         {"id": "two", "kind": "write",
          "run": f"printf 'b-new' > {b}", "touches": [str(b)]}])))

    planned = torra.plan("write a then b")
    done = torra.execute(planned, {})

    assert done.outcome is Outcome.BLOCKED
    assert a.read_text(encoding="utf-8") == "a-original", "step one is reversed"
    assert b.read_text(encoding="utf-8") == "EXTERNAL", "step two never ran, so is never undone"
    assert done.rollback is not None and done.rollback.ok


def test_a_blocked_dry_run_runs_no_undos(make_torra, tmp_path: Path) -> None:
    """A dry run changes nothing, so a block during one has nothing to undo."""
    conf = tmp_path / "shared.conf"
    conf.write_text("original", encoding="utf-8")
    torra = make_torra(scripted(plan_reply(
        "t", "edit conf", [{"id": "only", "run": "true", "touches": [str(conf)]}])))

    planned = torra.plan("edit conf")
    conf.write_text("EDITED BY A HUMAN", encoding="utf-8")
    done = torra.execute(planned, {}, dry_run=True)

    assert done.outcome is Outcome.BLOCKED
    assert conf.read_text(encoding="utf-8") == "EDITED BY A HUMAN"
    assert done.rollback is None


def test_a_declared_rollback_does_not_run_when_no_step_ran(
        make_torra, tmp_path: Path) -> None:
    """A declared rollback compensates for steps; with none run it is a
    cleanup of work that never happened (a declared `rm -rf` of a directory
    that already held someone's files, say)."""
    conf = tmp_path / "shared.conf"
    conf.write_text("original", encoding="utf-8")
    marker = tmp_path / "cleanup-ran"
    torra = make_torra(scripted(plan_reply(
        "t", "edit conf",
        [{"id": "only", "run": "true", "touches": [str(conf)]}],
        rollback=[{"run": f"touch {marker}", "description": "cleanup", "safe": True}])))

    planned = torra.plan("edit conf")
    conf.write_text("EDITED BY A HUMAN", encoding="utf-8")
    done = torra.execute(planned, {})

    assert done.outcome is Outcome.BLOCKED
    assert not marker.exists()


def test_a_declared_rollback_still_runs_when_a_real_step_failed(
        make_torra, tmp_path: Path) -> None:
    """The other side of the rule above: once a step has run, the declared
    cleanup is wanted, and it still runs before the derived undos."""
    marker = tmp_path / "cleanup-ran"
    torra = make_torra(scripted(plan_reply(
        "t", "fail",
        [{"id": "boom", "run": "exit 1"}],
        rollback=[{"run": f"touch {marker}", "description": "cleanup", "safe": True}])))

    done = torra.execute(torra.plan("fail"), {})

    assert done.outcome is Outcome.FAILED
    assert marker.exists()


def test_a_conflict_after_a_step_ran_does_not_overwrite_the_edited_file(
        make_torra, tmp_path: Path) -> None:
    """A human edits a file torra's own earlier step wrote. Restoring the
    plan-time snapshot would destroy that edit, so the file's undo is dropped
    and the decision log says so."""
    a = tmp_path / "a.txt"
    a.write_text("a-original", encoding="utf-8")
    marker = tmp_path / "second-ran"
    torra = make_torra(scripted(plan_reply(
        "t", "write a then a marker",
        [{"id": "one", "kind": "write", "run": f"printf 'torra-wrote' > {a}",
          "touches": [str(a)]},
         {"id": "two", "run": f"touch {marker}", "touches": [str(a)]}])))
    real_detect, calls = torra.conflicts.detect, []

    def detect_with_a_human_edit_landing_between_the_steps():
        calls.append(1)
        if len(calls) == 2:
            a.write_text("HUMAN EDIT", encoding="utf-8")
        return real_detect()

    torra.conflicts.detect = detect_with_a_human_edit_landing_between_the_steps  # type: ignore[method-assign]

    done = torra.execute(torra.plan("write a then a marker"), {})

    assert done.outcome is Outcome.BLOCKED
    assert a.read_text(encoding="utf-8") == "HUMAN EDIT"
    assert not marker.exists()
    assert any(d["decision"] == "undo skipped" and "changed outside torra" in d["reasoning"]
               for d in done.decisions)


def test_declared_undos_hold_a_dangerous_command_declared_safe_as_unsafe() -> None:
    """The collaborator on its own: a declared-safe entry that classifies
    DANGEROUS comes back unsafe, and the override is logged."""
    spec = TaskSpec(task_id="t", request="r", steps=[],
                    rollback=[{"run": "rm -rf /srv/api", "description": "wipe", "safe": True},
                              {"run": "true", "description": "noop", "safe": True}])
    decisions = DecisionLog()

    held = declared_undos(spec, SafetyClassifier(), decisions)

    assert [(a.description, a.safe) for a in held] == [("wipe", False), ("noop", True)]
    assert [r.decision for r in decisions.records] == ["rollback safety override"]


def test_sweep_pushes_declared_entries_only_when_a_real_step_ran() -> None:
    for ran, expected in ((False, []), (True, ["declared"])):
        ran_undos: list[str] = []
        rollback = VerifiedRollback()
        held = [UndoAction("declared", lambda log=ran_undos: log.append("declared"))]
        result = RunResult(request="r", outcome=Outcome.FAILED, executed=ran)

        sweep(rollback, DecisionLog(), result, held)

        assert ran_undos == expected
        assert (result.rollback is not None) is ran


def test_undos_left_by_an_earlier_halted_rollback_are_not_run_by_a_later_plan(
        make_torra, tmp_path: Path) -> None:
    """Each execute attempt starts from an empty undo stack."""
    conf = tmp_path / "shared.conf"
    conf.write_text("original", encoding="utf-8")
    torra = make_torra(scripted(plan_reply(
        "t", "edit conf", [{"id": "only", "run": "true", "touches": [str(conf)]}])))
    leaked: list[str] = []
    torra.rollback.add("left over from an earlier plan", lambda: leaked.append("ran"))

    planned = torra.plan("edit conf")
    conf.write_text("EDITED BY A HUMAN", encoding="utf-8")
    done = torra.execute(planned, {})

    assert done.outcome is Outcome.BLOCKED
    assert leaked == []


def test_a_dry_run_executes_nothing_but_gates_everything(make_torra,
                                                         tmp_path: Path) -> None:
    marker = tmp_path / "nope"
    torra = make_torra(scripted(plan_reply(
        "t", f"create {marker}", [{"id": "t", "run": f"touch {marker}"}])))

    done = torra.execute(torra.plan(f"create {marker}"), {}, dry_run=True)

    assert done.outcome is Outcome.COMPLETED
    assert not marker.exists()
    assert done.results and "[dry-run]" in done.results[0].output
    assert done.plan is not None and done.plan.steps[0].verdict.level


# --------------------------------------------------------------------------- #
# Non-plan outcomes
# --------------------------------------------------------------------------- #


def test_an_audit_write_failure_is_logged_not_raised(
        make_torra, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Telemetry can never break a run (invariant §2.5) -- the audit
    write is the same principle applied to the file it writes, not just
    the JSONL log."""
    marker = tmp_path / "marker"
    spec = TaskSpec.validate({
        "task_id": "audit-blocked", "request": f"create {marker}",
        "steps": [{"id": "touch", "run": f"touch {marker}"}]}, source="test")
    torra = make_torra(scripted({}))
    audit_path = torra.audit_dir / "audit-blocked.audit.json"
    real_write_text = Path.write_text

    def selective_broken_write_text(self, *args, **kwargs):
        if self == audit_path:
            raise OSError("simulated disk full")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", selective_broken_write_text)
    plan = torra._gate(spec)
    result = RunResult(request=spec.request, outcome=Outcome.AWAITING_APPROVAL, plan=plan)

    done = torra.execute(result, {})

    assert done.outcome is Outcome.COMPLETED, "the run itself must still succeed"
    assert done.audit_path == ""
    # Not done.decisions: result.decisions is snapshotted from the log
    # before this failure is logged (the same "_finish() snapshots too
    # early" shape as the summary-failure case in another test above),
    # so the record only shows up on torra's own live log.
    assert any(d.decision == "audit write failed" for d in torra.decisions.records)


def test_stats_reports_every_subsystem(make_torra) -> None:
    torra = make_torra(scripted({}))
    stats = torra.stats()
    assert set(stats) == {"agent", "conflicts", "verifier", "undo", "decisions"}


def test_close_releases_watches_without_raising(make_torra) -> None:
    torra = make_torra(scripted({}))
    torra.close()  # must not raise


def test_a_question_is_answered_not_planned(make_torra) -> None:
    torra = make_torra(scripted(
        {"intent": "answer", "message": "40 GB free on /, 60% used."}))
    result = torra.plan("how much disk is free")

    assert result.outcome is Outcome.ANSWERED
    assert result.plan is None
    assert "40 GB" in result.message


def test_run_returns_a_non_awaiting_outcome_without_calling_approve(make_torra) -> None:
    """The convenience run() wrapper's early exit -- nothing to gate means
    nothing to approve, so `approve` must never even be called."""
    torra = make_torra(scripted(
        {"intent": "answer", "message": "40 GB free on /, 60% used."}))
    calls = []

    result = torra.run("how much disk is free", approve=lambda plan: calls.append(plan))

    assert result.outcome is Outcome.ANSWERED
    assert calls == []


def test_answer_on_a_result_with_no_turn_is_a_no_op(make_torra) -> None:
    """`answer()` re-plans by reading the original turn's questions --
    a result built without ever going through the model (a hand-written
    task file's RunResult) has no turn to answer against."""
    torra = make_torra(scripted({}))
    result = RunResult(request="r", outcome=Outcome.NEEDS_ANSWER)

    same = torra.answer(result, ["some answer"])

    assert same is result


def test_a_resume_point_that_cannot_be_written_does_not_stop_the_run(
        make_torra, tmp_path: Path) -> None:
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
    torra = make_torra(scripted({}))
    resume_dir = torra.workdir / "resume" / "resume-blocked.json"
    resume_dir.mkdir(parents=True)

    plan = torra._gate(spec)
    result = RunResult(request=spec.request, outcome=Outcome.AWAITING_APPROVAL, plan=plan)
    done = torra.execute(result, {})

    assert done.outcome is Outcome.COMPLETED
    assert marker.exists(), "the real step still ran despite the resume-point failure"


def test_a_step_with_no_run_action_fails_instead_of_crashing(make_torra) -> None:
    """`TaskSpec.to_steps()` always attaches a real closure -- this guards
    a `Step` built some other way (a `Plan` assembled by hand, as this
    test does, rather than through `_gate()`)."""
    spec = TaskSpec.validate({
        "task_id": "t", "request": "r", "steps": [{"id": "s1", "run": "true"}]},
        source="test")
    step = Step(step_id="s1", command="true", run=None)
    sp = StepPlan(step=step, verdict=Verdict("true", RiskLevel.SAFE))
    plan = Plan(spec=spec, steps=[sp])
    torra = make_torra(scripted({}))
    result = RunResult(request=spec.request, outcome=Outcome.AWAITING_APPROVAL, plan=plan)

    done = torra.execute(result, {})

    assert done.outcome is Outcome.FAILED
    assert "has no run action attached" in done.results[0].error


class TestRemotePushEscalation:
    """`_gate()`'s unconditional CRITICAL floor for `remote_push` steps --
    HANDOFF.md §2.53. No test here touches a live network; the executor
    runner is always a fake dict-backed callable."""

    def test_remote_push_is_critical_even_though_its_own_text_classifies_safe(
            self, make_torra, tmp_path: Path) -> None:
        spec = TaskSpec.validate({
            "task_id": "t", "request": "r",
            "steps": [{"id": "push", "kind": "remote_push", "run": "ignored",
                      "remote_path": "/etc/nginx/nginx.conf",
                      "reads": [str(tmp_path / "local.conf")]}]}, source="test")
        torra = make_torra(scripted({}))
        torra._executor_runner = None  # unconfigured -- irrelevant to classification

        plan = torra._gate(spec)

        sp = plan.steps[0]
        assert sp.verdict.level is RiskLevel.CRITICAL
        assert any("less-guarded machine" in r for r in sp.verdict.reasons)
        # Confirms this is a floor, not a fluke: torra-authored command
        # text alone never reaches CRITICAL in SafetyClassifier.
        assert SafetyClassifier().classify(sp.step.command).level is not RiskLevel.CRITICAL

    def test_remote_pull_is_judged_by_the_local_file_it_writes(
            self, make_torra, tmp_path: Path) -> None:
        spec = TaskSpec.validate({
            "task_id": "t", "request": "r",
            "steps": [{"id": "pull", "kind": "remote_pull", "run": "ignored",
                      "remote_path": "/etc/nginx/nginx.conf",
                      "touches": [str(tmp_path / "local.conf")]}]}, source="test")
        torra = make_torra(scripted({}))
        torra._executor_runner = None

        plan = torra._gate(spec)

        assert plan.steps[0].verdict.level is RiskLevel.CAUTION
        assert not plan.steps[0].verdict.needs_confirmation

    def test_remote_pull_into_a_system_path_asks(self, make_torra) -> None:
        spec = TaskSpec.validate({
            "task_id": "t", "request": "r",
            "steps": [{"id": "pull", "kind": "remote_pull", "run": "ignored",
                      "remote_path": "/etc/nginx/nginx.conf",
                      "touches": ["/etc/nginx/nginx.conf"]}]}, source="test")
        torra = make_torra(scripted({}))
        torra._executor_runner = None

        assert torra._gate(spec).steps[0].verdict.needs_confirmation

    def test_end_to_end_pull_edit_push_with_a_real_snapshot_based_undo(
            self, make_torra, tmp_path: Path) -> None:
        remote = {"/etc/nginx/nginx.conf": "listen 80;\n"}
        local_path = tmp_path / "nginx.conf"

        def fake_runner(command: str, cwd: str | None, timeout: float | None) -> str:
            if command.startswith("test -e"):
                return "yes" if "/etc/nginx/nginx.conf" in remote else "no"
            if command.startswith("base64 -w0"):
                import base64 as b64
                return b64.b64encode(remote["/etc/nginx/nginx.conf"].encode("utf-8")).decode()
            if "base64 -d" in command:
                import base64
                encoded = command.split("echo ", 1)[1].split(" |", 1)[0].strip("'")
                remote["/etc/nginx/nginx.conf"] = base64.b64decode(encoded).decode("utf-8")
                return ""
            raise AssertionError(f"unexpected command: {command}")

        torra = make_torra(scripted(plan_reply(
            "t", "edit nginx on executor",
            [{"id": "pull", "kind": "remote_pull",
              "run": "ignored", "remote_path": "/etc/nginx/nginx.conf",
              "touches": [str(local_path)]},
             {"id": "edit", "kind": "shell",
              "run": f"echo 'listen 8080;' > {local_path}",
              "touches": [str(local_path)]},
             {"id": "push", "kind": "remote_push",
              "run": "ignored", "remote_path": "/etc/nginx/nginx.conf",
              "reads": [str(local_path)]}])),
            undo=UndoGenerator(shadow_dir=tmp_path / "shadow", executor_runner=fake_runner))
        torra._executor_runner = fake_runner
        planned = torra.plan("edit nginx on executor")

        push_step = next(sp for sp in planned.plan.steps if sp.step.kind == "remote_push")
        assert push_step.verdict.level is RiskLevel.CRITICAL
        assert len(push_step.undos) == 1, "a real snapshot must be captured before execute()"

        done = torra.execute(planned, {
            sp.step.step_id: True for sp in planned.plan.steps})

        assert done.outcome is Outcome.COMPLETED
        assert remote["/etc/nginx/nginx.conf"] == "listen 8080;\n"

    def test_a_push_with_a_failed_snapshot_is_uncovered_and_still_needs_approval(
            self, make_torra, tmp_path: Path) -> None:
        def always_fails(command: str, cwd: str | None, timeout: float | None) -> str:
            raise RuntimeError("executor unreachable")

        spec = TaskSpec.validate({
            "task_id": "t", "request": "r",
            "steps": [{"id": "push", "kind": "remote_push", "run": "ignored",
                      "remote_path": "/etc/nginx/nginx.conf",
                      "reads": [str(tmp_path / "local.conf")]}]}, source="test")
        torra = make_torra(
            scripted({}),
            undo=UndoGenerator(shadow_dir=tmp_path / "shadow", executor_runner=always_fails))
        torra._executor_runner = always_fails

        plan = torra._gate(spec)

        sp = plan.steps[0]
        assert sp.undos == [], "a failed snapshot must mean genuinely no undo"
        assert sp.verdict.level is RiskLevel.CRITICAL, \
            "still needs an explicit tick even with no undo coverage"
        assert plan.coverage.get("coverage_ratio", 1.0) < 1.0

    def test_a_failing_push_step_triggers_the_normal_rollback_sweep(
            self, make_torra, tmp_path: Path) -> None:
        """No new mid-run recovery path needed -- a step whose run() raises
        already sweeps every already-run step's undo in reverse, and that
        composes correctly here with zero changes."""
        local_path = tmp_path / "nginx.conf"
        local_path.write_text("original\n", encoding="utf-8")

        def push_always_fails(command: str, cwd: str | None, timeout: float | None) -> str:
            raise RuntimeError("connection reset")

        torra = make_torra(scripted(plan_reply(
            "t", "push a change",
            [{"id": "edit", "kind": "shell",
              "run": f"echo changed > {local_path}", "touches": [str(local_path)]},
             {"id": "push", "kind": "remote_push", "run": "ignored",
              "remote_path": "/etc/nginx/nginx.conf", "reads": [str(local_path)]}])),
            undo=UndoGenerator(shadow_dir=tmp_path / "shadow"))
        torra._executor_runner = push_always_fails
        planned = torra.plan("push a change")

        done = torra.execute(planned, {
            sp.step.step_id: True for sp in planned.plan.steps})

        assert done.outcome is Outcome.FAILED
        assert done.rollback is not None
        assert local_path.read_text(encoding="utf-8") == "original\n", \
            "the local edit must have been rolled back by the normal sweep"


def test_execute_on_a_result_with_no_plan_fails_rather_than_crashing(make_torra) -> None:
    """execute() is only ever called on an AWAITING_APPROVAL result with a
    plan attached in normal use -- this is the defensive guard for a
    caller that skips straight to execute() on something else, e.g. an
    ANSWERED result."""
    torra = make_torra(scripted(
        {"intent": "answer", "message": "40 GB free on /, 60% used."}))
    answered = torra.plan("how much disk is free")

    done = torra.execute(answered, {})

    assert done.outcome is Outcome.FAILED
    assert done.abort_reason == "nothing to execute"


def test_a_vague_request_becomes_a_question(make_torra) -> None:
    """The model asks, informed by what the clarifier found missing."""
    torra = make_torra(scripted(
        {"intent": "ask", "message": "Which host?",
         "questions": ["Which host do you mean?"]}))
    result = torra.plan("tidy up the box")

    assert result.outcome is Outcome.NEEDS_ANSWER
    assert result.turn is not None
    assert result.turn.questions
    assert result.turn.clarifier_gaps, "the clarifier fed the model its gaps"


def test_a_refusal_is_reported(make_torra) -> None:
    torra = make_torra(scripted(
        {"intent": "refuse", "message": "That would destroy the boot partition."}))
    assert torra.plan("wipe /boot").outcome is Outcome.REFUSED


def test_a_dead_backend_is_reported_with_recovery(make_torra) -> None:
    def dead(path, payload, timeout):
        raise ConnectionRefusedError("connection refused")

    torra = make_torra(dead)
    result = torra.plan("do anything")

    assert result.outcome is Outcome.BACKEND_DOWN
    assert "generation failed" in result.abort_reason
    assert len(torra.ollama.recovery_options()) == 3


class _DeadAltBackend:
    """A ModelBackend that always fails, with backend-specific recovery advice."""

    def chat(self, model: str, messages: object, json_mode: bool = False,
              temperature: float = 0.2, on_thinking: object | None = None) -> str:
        raise OllamaError("openrouter is down")

    def recovery_options(self, current_model: str = "unknown") -> list[RecoveryOption]:
        return [RecoveryOption("check-key", "Check OPENROUTER_API_KEY", "detail", "action")]

    def stats(self) -> dict[str, object]:
        return {}


def test_recovery_options_come_from_the_backend_that_actually_failed(make_torra) -> None:
    """"Restart Ollama" is exactly the wrong advice for a failed
    openrouter/... request -- the logged recovery options must come from
    the backend that was actually attempted, not always self.ollama."""
    torra = make_torra(scripted({}), backends={"openrouter": _DeadAltBackend()})

    result = torra.plan("do anything", model="openrouter/some-model")

    assert result.outcome is Outcome.BACKEND_DOWN
    logged = result.decisions[-1]["evidence"]["recovery"]
    assert any("OPENROUTER_API_KEY" in o["title"] for o in logged)
    assert not any("Ollama" in o["title"] for o in logged)


def test_torra_backends_reach_the_agent(make_torra) -> None:
    fake = _DeadAltBackend()
    torra = make_torra(scripted({}), backends={"openrouter": fake})
    assert torra.agent.backends["openrouter"] is fake


def test_a_failed_precondition_blocks_before_anything_runs(make_torra,
                                                           tmp_path: Path) -> None:
    marker = tmp_path / "nope"
    verifier = StateVerifier()
    verifier.add(Precondition("impossible", lambda: False, "never passes"))

    torra = make_torra(scripted(plan_reply(
        "t", f"create {marker}", [{"id": "t", "run": f"touch {marker}"}])),
        verifier=verifier)
    result = torra.plan(f"create {marker}")

    assert result.outcome is Outcome.BLOCKED
    assert "preconditions failed" in result.abort_reason
    assert not marker.exists()


# --------------------------------------------------------------------------- #
# The run() convenience and hand-written files
# --------------------------------------------------------------------------- #


def test_run_approves_through_a_callback(make_torra, tmp_path: Path) -> None:
    """What the CLI and the TUI both use."""
    victim = tmp_path / "v.txt"
    victim.write_text("x", encoding="utf-8")
    torra = make_torra(scripted(plan_reply(
        "t", f"remove {victim}",
        [{"id": "rm", "run": f"rm -f {victim}", "touches": [str(victim)]}])))

    seen = {}

    def approve(plan) -> dict:
        seen["worst"] = plan.worst_level.value
        seen["needs"] = [sp.step.step_id for sp in plan.needs_confirmation]
        return {sp.step.step_id: True for sp in plan.needs_confirmation}

    done = torra.run(f"delete {victim}", approve=approve)

    assert done.outcome is Outcome.COMPLETED
    assert seen["needs"] == ["rm"]
    assert seen["worst"] in ("dangerous", "critical")
    assert not victim.exists()


def test_run_refuses_when_no_approver_is_given(make_torra,
                                               tmp_path: Path) -> None:
    """Unattended defaults to refusing, which is the safe direction."""
    victim = tmp_path / "v.txt"
    victim.write_text("x", encoding="utf-8")
    torra = make_torra(scripted(plan_reply(
        "t", f"remove {victim}",
        [{"id": "rm", "run": f"rm -f {victim}", "touches": [str(victim)]}])))

    assert torra.run(f"delete {victim}").outcome is Outcome.REJECTED
    assert victim.exists()


def test_a_hand_written_file_still_works_without_a_model(make_torra,
                                                         tmp_path: Path) -> None:
    """When the model is down or wrong, the gates must still be reachable."""
    marker = tmp_path / "made"
    spec = TaskSpec.validate({
        "task_id": "manual", "request": f"create {marker}",
        "steps": [{"id": "t", "run": f"touch {marker}"}]}, source="test")

    torra = make_torra(scripted({}))
    plan = torra._gate(spec)

    from torra.pipeline import RunResult
    result = RunResult(request=spec.request, outcome=Outcome.AWAITING_APPROVAL,
                       stages=["gate", "present"], plan=plan)
    done = torra.execute(result, {})

    assert done.outcome is Outcome.COMPLETED
    assert marker.exists()
    assert torra.ollama.calls == 0, "no model should have been consulted"


def test_a_malicious_task_id_cannot_write_the_resume_file_outside_the_workdir(
        make_torra, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Found live: the resume-file path was built straight from
    `plan.spec.task_id` with no sanitizing at all -- unlike the audit path
    built in the very same function, which already sanitized. A task_id of
    "../../evil-poc" wrote a real file outside workdir/resume/ on any run
    where a later step failed before _finish_success()'s cleanup ran (which
    only fires on a fully successful run). task_id is fully model- or
    human-controlled -- TaskSpec.validate() does `task_id=str(data[...])`
    with no character restriction at all.

    Spies on ResumePoint.save to capture the path it was actually given,
    rather than checking the file still exists after execute() returns:
    HANDOFF.md §6.4's resume fix now deletes the resume file on *any*
    terminal outcome including this test's own induced failure (a rollback
    means the file's "these steps are done" claim is no longer true), so
    post-hoc existence is no longer the right thing to assert on."""
    from torra.core.resume import ResumePoint

    seen_paths: list[Path] = []
    real_save = ResumePoint.save

    def spy_save(self: ResumePoint, path: Path) -> Path:
        seen_paths.append(Path(path))
        return real_save(self, path)

    monkeypatch.setattr(ResumePoint, "save", spy_save)

    evil_id = "../../evil-poc"
    torra = make_torra(scripted(plan_reply(
        evil_id, "do something",
        [{"id": "ok", "run": "true"}, {"id": "bad", "run": "false"}])))

    planned = torra.plan("do something")
    done = torra.execute(planned, {})

    assert done.outcome is Outcome.FAILED
    escaped = tmp_path / "evil-poc.json"
    assert not escaped.exists(), "resume file escaped torra's own workdir"
    resume_dir = tmp_path / "wd" / "resume"
    assert seen_paths, "the ok step should have written a resume point before bad failed"
    assert all(p.parent == resume_dir for p in seen_paths), \
        "every resume write should target the sanitized path inside workdir/resume/"


def test_knowledge_dir_defaults_from_the_environment(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """cli.py/tui.py construct Torra() with no knowledge_dir argument at
    all -- this is the only place that has to read TORRA_KNOWLEDGE_DIR,
    so they need no changes to pick up a configured library."""
    monkeypatch.setenv("TORRA_KNOWLEDGE_DIR", str(tmp_path))
    torra = Torra(workdir=tmp_path / "wd")
    assert torra.knowledge_dir == str(tmp_path)


def test_an_explicit_knowledge_dir_overrides_the_environment(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TORRA_KNOWLEDGE_DIR", "/from/env")
    torra = Torra(workdir=tmp_path / "wd", knowledge_dir=str(tmp_path))
    assert torra.knowledge_dir == str(tmp_path)


def test_an_explicit_empty_knowledge_dir_disables_it_even_with_env_set(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TORRA_KNOWLEDGE_DIR", "/from/env")
    torra = Torra(workdir=tmp_path / "wd", knowledge_dir="")
    assert torra.knowledge_dir is None


def test_knowledge_dir_is_none_by_default_with_nothing_configured(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("TORRA_KNOWLEDGE_DIR", raising=False)
    torra = Torra(workdir=tmp_path / "wd")
    assert torra.knowledge_dir is None


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


def test_a_completed_run_gets_a_synthesized_summary(make_torra, tmp_path: Path) -> None:
    """The gap this closes: `torra ask` used to print only ok/fail per
    step, never what a read step actually found."""
    target = tmp_path / "data.txt"
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")
    plan_content = plan_reply(
        "read-file", f"read {target}",
        [{"id": "read", "kind": "read", "run": f"cat {target}"}])

    torra = make_torra(_chat_transport(
        plan_content, "The file has three lines: one, two, three."))
    planned = torra.plan(f"how many lines are in {target}")
    assert planned.outcome is Outcome.AWAITING_APPROVAL

    done = torra.execute(planned, {})

    assert done.outcome is Outcome.COMPLETED
    assert done.summary == "The file has three lines: one, two, three."
    assert done.message == done.summary


def test_a_dry_run_never_gets_a_summary(make_torra, tmp_path: Path) -> None:
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

    torra = make_torra(_chat_transport(
        plan_content, "The file has three lines: one, two, three."))
    planned = torra.plan(f"how many lines are in {target}")

    done = torra.execute(planned, {}, dry_run=True)

    assert done.outcome is Outcome.COMPLETED
    assert done.results[0].output == f"[dry-run] cat {target}"
    assert done.summary == ""


def test_a_hand_written_plan_gets_no_summary_and_consults_no_model(
        make_torra, tmp_path: Path) -> None:
    """The same invariant `test_a_hand_written_file_still_works_without_a_model`
    guards, checked from the summary field's own side: a plan nobody's
    model wrote must not become the trigger for the first model call."""
    marker = tmp_path / "made"
    spec = TaskSpec.validate({
        "task_id": "manual", "request": f"create {marker}",
        "steps": [{"id": "t", "run": f"touch {marker}"}]}, source="test")

    torra = make_torra(scripted({}))
    plan = torra._gate(spec)
    result = RunResult(request=spec.request, outcome=Outcome.AWAITING_APPROVAL,
                       stages=["gate", "present"], plan=plan)
    done = torra.execute(result, {})

    assert done.summary == ""
    assert torra.ollama.calls == 0


def test_a_failed_summarize_call_does_not_break_the_run(
        make_torra, tmp_path: Path) -> None:
    """Same rule as telemetry: a reporting step that can fail a real run
    would make torra less reliable than not reporting at all."""
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

    torra = make_torra(transport)
    planned = torra.plan(f"what is in {target}")
    done = torra.execute(planned, {})

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
        make_torra, tmp_path: Path) -> None:
    marker = tmp_path / "touched"
    spec = TaskSpec.validate({
        "task_id": "t", "request": f"create {marker}",
        "steps": [{"id": "touch", "run": f"touch {marker}", "touches": [str(marker)]},
                 {"id": "no-undo", "run": "true"}]}, source="test")
    torra = make_torra(scripted({}))

    plan = torra._gate(spec)

    by_id = {sp.step.step_id: sp for sp in plan.steps}
    assert by_id["touch"].has_undo is True
    assert by_id["no-undo"].has_undo is False


def test_run_result_ok_is_true_only_when_completed(make_torra, tmp_path: Path) -> None:
    marker = tmp_path / "made"
    spec = TaskSpec.validate({
        "task_id": "t", "request": f"create {marker}",
        "steps": [{"id": "touch", "run": f"touch {marker}"}]}, source="test")
    torra = make_torra(scripted({}))
    planned = RunResult(request=spec.request, outcome=Outcome.AWAITING_APPROVAL,
                        plan=torra._gate(spec))

    done = torra.execute(planned, {})

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


def test_two_torras_sharing_a_workdir_do_not_collide_on_shadow_files(
        tmp_path: Path) -> None:
    """`UndoGenerator`'s default shadow_dir is keyed only by (workdir, pid)
    -- two Torra instances sharing a workdir in one process (exactly
    what tabs are) would collide on it, and one session's cleanup() could
    wipe another session's shadow copies mid-run. The fix is session-scoped:
    each tab's Torra gets an explicit undo=UndoGenerator(shadow_dir=...)
    keyed by session id instead of the shared default. This proves the
    pattern actually prevents the collision, not just that construction
    succeeds."""
    session_a_file = tmp_path / "a.conf"
    session_a_file.write_text("session a content", encoding="utf-8")
    torra_a = Torra(
        workdir=tmp_path, ollama=OllamaClient(transport=lambda p, pay, t: {}),
        undo=UndoGenerator(shadow_dir=tmp_path / "torra-shadow-session-a"))
    torra_b = Torra(
        workdir=tmp_path, ollama=OllamaClient(transport=lambda p, pay, t: {}),
        undo=UndoGenerator(shadow_dir=tmp_path / "torra-shadow-session-b"))

    assert torra_a.undo.shadow_dir != torra_b.undo.shadow_dir

    spec_a = TaskSpec.validate({
        "task_id": "t", "request": f"edit {session_a_file}",
        "steps": [{"id": "edit", "run": f"echo overwritten > {session_a_file}",
                  "touches": [str(session_a_file)]}]}, source="test")
    # _gate() snapshots at propose time -- before execute() runs, and
    # regardless of dry-run/real, matching how the real pipeline works
    # (see pipeline.py's own comment on why cleanup() only fires on a
    # successful real execute(), not here).
    torra_a._gate(spec_a)
    assert torra_a.undo.shadow_dir.exists()
    shadow_files_before = list(torra_a.undo.shadow_dir.iterdir())
    assert shadow_files_before, "session a should have a real shadow copy on disk"

    torra_b.undo.cleanup()

    assert list(torra_a.undo.shadow_dir.iterdir()) == shadow_files_before, (
        "session b's cleanup must never touch session a's shadow directory"
    )


def test_two_sessions_non_plan_turns_do_not_collide_on_the_same_audit_file(
        tmp_path: Path) -> None:
    """`_finish()`'s audit filename for a non-plan turn (answer/ask/refuse)
    used to be `turn-{len(self.agent.turns)}` -- no task_id_prefix at all.
    Two tabs' first such turn both landed on "turn-1", one silently
    overwriting the other's audit record. Found by a full-codebase audit
    right after the multi-tab feature shipped, reproduced directly against
    two live Torra instances before this test was written."""
    torra_a = Torra(
        workdir=tmp_path, ollama=OllamaClient(transport=scripted(
            {"intent": "answer", "message": "reply for a"})))
    torra_b = Torra(
        workdir=tmp_path, ollama=OllamaClient(transport=scripted(
            {"intent": "answer", "message": "reply for b"})))
    torra_a.agent.task_id_prefix = "session-a-"
    torra_b.agent.task_id_prefix = "session-b-"

    result_a = torra_a.plan("what is the disk usage")
    result_b = torra_b.plan("what is the memory usage")

    assert result_a.audit_path != result_b.audit_path
    on_disk = json.loads(Path(result_a.audit_path).read_text(encoding="utf-8"))
    assert on_disk["request"] == "what is the disk usage"


def test_a_new_plan_call_starts_a_fresh_decision_trail(tmp_path: Path) -> None:
    """A `Torra` reused across many turns (a long-lived TUI tab) used to
    accumulate every prior turn's decisions into each new turn's audit
    record and result.decisions, growing without bound for the tab's whole
    life. plan() now clears the trail as its first act; execute() of the
    *same* task must still see that task's own plan-stage decisions."""
    torra = Torra(
        workdir=tmp_path, ollama=OllamaClient(transport=scripted(
            {"intent": "answer", "message": "first"},
            {"intent": "answer", "message": "second"})))

    result1 = torra.plan("first request")
    assert len(result1.decisions) == 1

    result2 = torra.plan("second request")
    assert len(result2.decisions) == 1, \
        "turn 2's decisions must not include turn 1's"


def test_plan_then_execute_of_the_same_task_still_share_one_decision_trail(
        make_torra, tmp_path: Path) -> None:
    """plan()'s fresh-trail clear() must not also wipe out plan-stage
    decisions by the time the *same* task reaches execute() -- only a new
    plan() call is a new task boundary, not execute() continuing one."""
    marker = tmp_path / "made"
    torra = make_torra(scripted(plan_reply(
        "t", f"create {marker}", [{"id": "touch", "run": f"touch {marker}"}])))

    planned = torra.plan(f"create {marker}")
    assert len(planned.decisions) >= 1, "plan-stage decisions must be recorded"
    plan_stage_count = len(planned.decisions)

    done = torra.execute(planned, {})
    assert len(done.decisions) >= plan_stage_count, \
        "execute() must build on plan()'s trail for the same task, not replace it"


def test_ctrl_c_during_a_step_is_a_failed_step_and_rolls_back(make_torra, tmp_path: Path) -> None:
    """KeyboardInterrupt used to escape the broad except and skip the rollback sweep."""
    target = tmp_path / "f.txt"
    target.write_text("original", encoding="utf-8")
    torra = make_torra(scripted(plan_reply(
        "t", "write then get interrupted",
        [{"id": "one", "kind": "write", "run": f"printf 'changed' > {target}",
          "touches": [str(target)]},
         {"id": "two", "run": "true"}])))
    planned = torra.plan("write then get interrupted")

    def interrupted() -> str:
        raise KeyboardInterrupt

    planned.plan.steps[1].step.run = interrupted  # type: ignore[union-attr]
    done = torra.execute(planned, {})

    assert done.outcome is Outcome.FAILED
    assert "KeyboardInterrupt" in done.abort_reason
    assert target.read_text(encoding="utf-8") == "original"


def test_a_real_run_marks_the_observed_machine_state_stale(make_torra) -> None:
    """The state was observed once per agent and never refreshed, so the next turn reasoned
    from disk and service facts a step had just changed (HANDOFF.md §2.69)."""
    torra = make_torra(scripted(plan_reply("t", "touch", [{"id": "s", "run": "true"}])))
    planned = torra.plan("touch")
    assert torra.agent.state is not None

    torra.execute(planned, {}, dry_run=True)
    assert torra.agent.state is not None, "a dry run changed nothing"

    torra.execute(planned, {})
    assert torra.agent.state is None, "the next turn must observe the machine again"
