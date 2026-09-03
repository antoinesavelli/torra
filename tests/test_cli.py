"""cli.py: the actual command-line entry points a user runs.

Originally scoped to just the LoopholeAudit wiring found in a full-file
audit -- this file now covers the rest of cli.py's surface too
(cmd_ask/plan/run/check/doctor/label, build_parser, main), closing a gap
this file's own docstring used to flag as separate, pending work.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import pytest
import yaml
from conftest import plan_replies_with_summaries, plan_reply, scripted

import harness.cli as cli
from harness.core import OllamaClient, TaskSpec
from harness.pipeline import Harness
from harness.telemetry import CrashReporter, LabelStore, TaskLogger, TaskRecord

# --------------------------------------------------------------------------- #
# Shared plumbing
# --------------------------------------------------------------------------- #


def patch_ollama(monkeypatch, transport) -> None:
    """Route every OllamaClient cli.py constructs through a scripted transport.

    build_harness()/cmd_doctor() call the real OllamaClient() with no
    injected transport, which would otherwise hit the real network. Patches
    the name cli.py imported, not the class itself, so other modules are
    unaffected.
    """
    def factory(*args, **kwargs):
        return OllamaClient(transport=transport, probe_timeout=0.1)
    monkeypatch.setattr(cli, "OllamaClient", factory)


def patch_observe(monkeypatch, hostname: str = "pop-os") -> None:
    """Keep Agent.observe() off the real machine -- see agent.py's default
    observer, which calls this name at call time from its own module."""
    from harness.core import MachineState
    monkeypatch.setattr("harness.agent.observe_machine",
                        lambda **kwargs: MachineState(hostname=hostname))


def evidence_args(workdir: Path) -> argparse.Namespace:
    return argparse.Namespace(workdir=str(workdir), baseline_fn=0.0, baseline_fp=0.10)


def write_task_file(path: Path, task_id: str, request: str,
                    steps: list[dict], **extra) -> Path:
    data = {"task_id": task_id, "request": request, "steps": steps, **extra}
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# build_harness (OpenRouter registration)
# --------------------------------------------------------------------------- #


def test_build_harness_registers_openrouter_when_the_env_var_is_set(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_ollama(monkeypatch, scripted({}))
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")

    harness = cli.build_harness(tmp_path)

    assert "openrouter" in harness.agent.backends
    assert harness.agent.backends["openrouter"].api_key == "k"


def test_build_harness_registers_openrouter_even_without_the_env_var(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Found live by manual testing: registering it only when the env var
    was set meant an unconfigured `--model openrouter/...` request matched
    no prefix in Agent.backends, so it silently fell through to
    self.ollama -- which converse()'s degraded-daemon fallback then
    silently swapped for a real local model, with no error at all
    (HANDOFF.md §2.27). Registering it unconditionally is what lets
    OpenRouterClient.chat()'s own "OPENROUTER_API_KEY is not set" error
    actually fire, instead of the request being silently misrouted."""
    patch_ollama(monkeypatch, scripted({}))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    harness = cli.build_harness(tmp_path)

    assert "openrouter" in harness.agent.backends
    assert harness.agent.backends["openrouter"].api_key == ""


def test_build_harness_registers_openrouter_even_when_offline(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """offline is for `harness run` -- a hand-written task file that never
    calls the model at all, so this is harmless either way: constructing
    OpenRouterClient makes no network call, only chat() would."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")

    harness = cli.build_harness(tmp_path, offline=True)

    assert "openrouter" in harness.agent.backends


# --------------------------------------------------------------------------- #
# approve_at_prompt
# --------------------------------------------------------------------------- #


def test_approve_at_prompt_aborts_on_interrupt_instead_of_asking_again(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Found by an independent audit: Ctrl+C/Ctrl+D on the approve prompt
    used to decline only the current step and keep prompting for the next
    one -- confusing for an operator who expected the keystroke to bail
    out entirely, and inconsistent with _collect_answers' handling of the
    identical exception pair elsewhere in this file."""
    victim1, victim2 = tmp_path / "v1.txt", tmp_path / "v2.txt"
    victim1.write_text("keep me", encoding="utf-8")
    victim2.write_text("keep me too", encoding="utf-8")
    spec = TaskSpec.validate({
        "task_id": "t", "request": "delete two files",
        "steps": [
            {"id": "rm1", "run": f"rm -f {victim1}", "touches": [str(victim1)]},
            {"id": "rm2", "run": f"rm -f {victim2}", "touches": [str(victim2)]},
        ]}, source="test")
    harness = Harness(workdir=tmp_path / "wd",
                      ollama=OllamaClient(transport=lambda p, pay, t: {}))
    plan = harness._gate(spec)
    assert len(plan.needs_confirmation) == 2, "both rm steps must need confirmation"

    calls = {"n": 0}

    def fake_input(prompt: str) -> str:
        calls["n"] += 1
        raise EOFError

    monkeypatch.setattr("builtins.input", fake_input)

    approvals = cli.approve_at_prompt(plan)

    assert calls["n"] == 1, "must not prompt again for the remaining step"
    assert approvals == {}


def test_approve_at_prompt_records_a_real_yes_and_no(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """The ordinary path: an operator actually typing y/n at the prompt,
    not hitting Ctrl+D. Also covers the "no automatic undo" notice for a
    dangerous step that declares no `touches`."""
    victim = tmp_path / "v.txt"
    victim.write_text("keep me", encoding="utf-8")
    spec = TaskSpec.validate({
        "task_id": "t", "request": "two dangerous steps",
        "steps": [
            {"id": "no-undo", "run": "rm -f /nonexistent-path-xyz"},
            {"id": "has-undo", "run": f"rm -f {victim}", "touches": [str(victim)]},
        ]}, source="test")
    harness = Harness(workdir=tmp_path / "wd",
                      ollama=OllamaClient(transport=lambda p, pay, t: {}))
    plan = harness._gate(spec)
    assert len(plan.needs_confirmation) == 2

    answers = iter(["y", "n"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    approvals = cli.approve_at_prompt(plan)

    assert approvals == {"no-undo": True, "has-undo": False}
    assert "no automatic undo for this step" in capsys.readouterr().out


def test_approve_at_prompt_shows_the_steps_description(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """The operator needs the *why* right where they're deciding, not just
    the raw command -- found live: neither this prompt nor print_plan ever
    printed `step.description` at all, so a model that wrote real
    reasoning into it had nowhere for that reasoning to reach the operator."""
    spec = TaskSpec.validate({
        "task_id": "t", "request": "delete a stale cache",
        "steps": [{"id": "rm", "run": "rm -rf /tmp/build-cache",
                  "description": "clears the cache confirmed stale by "
                                 "the earlier du check"}]}, source="test")
    harness = Harness(workdir=tmp_path / "wd",
                      ollama=OllamaClient(transport=lambda p, pay, t: {}))
    plan = harness._gate(spec)

    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    cli.approve_at_prompt(plan)

    assert "confirmed stale by the earlier du check" in capsys.readouterr().out


def test_approve_at_prompt_does_not_print_a_description_that_only_repeats_run(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    spec = TaskSpec.validate({
        "task_id": "t", "request": "delete a file",
        "steps": [{"id": "rm", "run": "rm -f /tmp/x",
                  "description": "rm -f /tmp/x"}]}, source="test")
    harness = Harness(workdir=tmp_path / "wd",
                      ollama=OllamaClient(transport=lambda p, pay, t: {}))
    plan = harness._gate(spec)

    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    cli.approve_at_prompt(plan)

    out = capsys.readouterr().out
    assert "-- rm -f /tmp/x" not in out, "the command must not double as its own description"


# --------------------------------------------------------------------------- #
# print_plan
# --------------------------------------------------------------------------- #


def test_print_plan_shows_a_steps_description(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    spec = TaskSpec.validate({
        "task_id": "t", "request": "check disk usage",
        "steps": [{"id": "df", "run": "df -h /mnt/ParamoStorage",
                  "description": "confirms free space before the larger "
                                 "copy in the next step"}]}, source="test")
    harness = Harness(workdir=tmp_path / "wd",
                      ollama=OllamaClient(transport=lambda p, pay, t: {}))
    plan = harness._gate(spec)

    cli.print_plan(plan)

    assert "confirms free space before the larger copy" in capsys.readouterr().out


def test_print_plan_does_not_repeat_the_command_as_a_description(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    spec = TaskSpec.validate({
        "task_id": "t", "request": "check disk usage",
        "steps": [{"id": "df", "run": "df -h /mnt/ParamoStorage"}]},
        source="test")
    harness = Harness(workdir=tmp_path / "wd",
                      ollama=OllamaClient(transport=lambda p, pay, t: {}))
    plan = harness._gate(spec)

    cli.print_plan(plan)

    out = capsys.readouterr().out
    assert out.count("df -h /mnt/ParamoStorage") == 1


# --------------------------------------------------------------------------- #
# print_dependency_warnings
# --------------------------------------------------------------------------- #


def test_print_dependency_warnings_shows_a_real_hit_before_execute_would_run(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The whole point of calling this before execute(): the operator sees
    it while they can still change what they approved, not buried in the
    execution report after the commands already ran."""
    spec = TaskSpec.validate({
        "task_id": "t", "request": "remove then reference it",
        "steps": [
            {"id": "rm-declined", "run": "rm -f /tmp/scratch-dir/data.txt",
             "touches": ["/tmp/scratch-dir/data.txt"]},
            {"id": "use-it", "run": "cat /tmp/scratch-dir/data.txt"},
        ]}, source="test")
    harness = Harness(workdir=tmp_path / "wd",
                      ollama=OllamaClient(transport=lambda p, pay, t: {}))
    plan = harness._gate(spec)

    cli.print_dependency_warnings(plan, {"rm-declined": False, "use-it": True})

    out = capsys.readouterr().out
    assert "use-it" in out and "rm-declined" in out


def test_print_dependency_warnings_prints_nothing_when_there_is_nothing_to_flag(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    spec = TaskSpec.validate({
        "task_id": "t", "request": "two unrelated safe steps",
        "steps": [{"id": "a", "run": "echo one"}, {"id": "b", "run": "echo two"}]},
        source="test")
    harness = Harness(workdir=tmp_path / "wd",
                      ollama=OllamaClient(transport=lambda p, pay, t: {}))
    plan = harness._gate(spec)

    cli.print_dependency_warnings(plan, {})

    assert capsys.readouterr().out == ""


# --------------------------------------------------------------------------- #
# evidence (pre-existing)
# --------------------------------------------------------------------------- #


def test_evidence_intent_breakdown_does_not_silently_drop_answer_and_refuse(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Found live: the breakdown only ever counted intent == "plan"/"ask",
    so "answer" and "refuse" -- on one real machine's traffic, "answered"
    was 80% of everything recorded -- never appeared anywhere in a summary
    that reads as a complete accounting of "tasks recorded"."""
    logger = TaskLogger(tmp_path / "telemetry" / "tasks.jsonl")
    for intent in ["answer", "answer", "answer", "ask", "plan", "refuse"]:
        logger.record(TaskRecord(task_id="t", request="x", at=1.0, intent=intent))
    LabelStore(tmp_path / "telemetry" / "labels.jsonl")

    exit_code = cli.cmd_evidence(evidence_args(tmp_path))

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "answered     : 3" in out
    assert "refused      : 1" in out
    assert "asked        : 1" in out
    assert "planned      : 1" in out


def test_evidence_prints_the_loophole_audit_alongside_drift(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Found in a full-file audit: LoopholeAudit (harness/telemetry/audit.py)
    was fully implemented and exported but had no caller anywhere -- unlike
    its sibling CorpusExporter.drift(), which cmd_evidence already printed.
    A well-designed, working measurement with zero way to actually see its
    output is effectively dead code."""
    logger = TaskLogger(tmp_path / "telemetry" / "tasks.jsonl")
    for request in ["fix the api", "fix the api", "restart ollama.service"]:
        logger.record(TaskRecord(task_id="t", request=request, at=1.0))
    LabelStore(tmp_path / "telemetry" / "labels.jsonl")

    exit_code = cli.cmd_evidence(evidence_args(tmp_path))

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "anchored-but-goalless" in out
    assert "verdict" in out


def test_evidence_health_summary_shows_every_outcome_not_just_completed(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Before this, only `status == "completed"` was ever counted anywhere
    in cmd_evidence's output -- blocked/refused/backend_down/failed tasks
    were invisible in a summary that read as a complete accounting."""
    logger = TaskLogger(tmp_path / "telemetry" / "tasks.jsonl")
    for status in ["completed", "blocked", "blocked", "refused", "backend_down", "failed"]:
        logger.record(TaskRecord(task_id="t", request="x", at=1.0, status=status))
    LabelStore(tmp_path / "telemetry" / "labels.jsonl")

    exit_code = cli.cmd_evidence(evidence_args(tmp_path))

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "blocked          : 2" in out
    assert "refused          : 1" in out
    assert "backend_down     : 1" in out
    assert "failed           : 1" in out


def test_evidence_health_summary_reports_backend_and_latency(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    logger = TaskLogger(tmp_path / "telemetry" / "tasks.jsonl")
    logger.record(TaskRecord(task_id="a", request="x", at=1.0,
                             status="completed", model="devstral:24b", seconds=4.0))
    logger.record(TaskRecord(task_id="b", request="x", at=1.0, status="completed",
                             model="openrouter/anthropic/claude-3.5-sonnet", seconds=2.0))
    LabelStore(tmp_path / "telemetry" / "labels.jsonl")

    exit_code = cli.cmd_evidence(evidence_args(tmp_path))

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "backend: 1 local, 1 openrouter" in out
    assert "mean turn time: 3.00s over 2 timed task(s)" in out


def test_evidence_with_no_tasks_says_so_instead_of_an_empty_section(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli.cmd_evidence(evidence_args(tmp_path))
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "no tasks recorded yet" in out


def test_evidence_reports_crash_history(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    reporter = CrashReporter(tmp_path / "telemetry" / "crashes.jsonl")
    try:
        raise ValueError("simulated crash")
    except ValueError as exc:
        reporter.record(exc, site="tui")

    exit_code = cli.cmd_evidence(evidence_args(tmp_path))

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "1 crash(es) recorded" in out
    assert "ValueError (tui)" in out
    assert "simulated crash" in out


def test_evidence_with_no_crashes_says_so(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli.cmd_evidence(evidence_args(tmp_path))
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "no crashes recorded" in out


def test_evidence_prints_loophole_examples_when_present(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`loophole.examples` -- untested before this despite the loophole
    count itself being covered above: a request that clears the clarifier
    (it has a path anchor) but is goalless (vague verb, no stated outcome)."""
    logger = TaskLogger(tmp_path / "telemetry" / "tasks.jsonl")
    logger.record(TaskRecord(task_id="t", request="fix /etc/nginx/nginx.conf", at=1.0))

    exit_code = cli.cmd_evidence(evidence_args(tmp_path))

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "examples:" in out
    assert "fix /etc/nginx/nginx.conf" in out


# --------------------------------------------------------------------------- #
# ask / plan
# --------------------------------------------------------------------------- #


def ask_args(workdir: Path, request: str, *, model=None, dry_run=False,
             non_interactive=False, plan_only=False, verify=None,
             max_iterations=3) -> argparse.Namespace:
    return argparse.Namespace(workdir=str(workdir), request=request, model=model,
                              dry_run=dry_run, non_interactive=non_interactive,
                              plan_only=plan_only, verify=verify,
                              max_iterations=max_iterations)


def test_ask_runs_a_safe_plan_end_to_end(tmp_path, monkeypatch,
                                         capsys: pytest.CaptureFixture[str]) -> None:
    patch_observe(monkeypatch)
    marker = tmp_path / "marker"
    patch_ollama(monkeypatch, scripted(plan_reply(
        "t", f"touch {marker}", [{"id": "touch", "run": f"touch {marker}"}])))

    exit_code = cli.cmd_ask(ask_args(tmp_path, f"touch {marker}", non_interactive=True))

    out = capsys.readouterr().out
    assert exit_code == cli.EXIT_OK
    assert "outcome: completed" in out
    assert marker.exists()


def test_ask_verify_fixes_a_failing_check_and_reports_success(
        tmp_path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    patch_observe(monkeypatch)
    # The placeholder request text below reads as underspecified to
    # Clarifier.detect(), which would otherwise spend a scripted reply on
    # a repair round -- see test_iterate.py's _no_repairs for why.
    monkeypatch.setattr("harness.agent.Agent.MAX_REPAIRS", 0)
    marker = tmp_path / "fixed"
    patch_ollama(monkeypatch, scripted(*plan_replies_with_summaries(
        plan_reply("edit", "add the feature", [{"id": "edit", "run": "true"}]),
        plan_reply("verify1", "run the tests",
                  [{"id": "verify", "run": f"test -f {marker}"}]),
        plan_reply("fix", "fix it", [{"id": "fix", "run": f"touch {marker}"}]),
        plan_reply("verify2", "run the tests",
                  [{"id": "verify", "run": f"test -f {marker}"}]),
    )))

    exit_code = cli.cmd_ask(ask_args(
        tmp_path, "add the feature", non_interactive=True,
        verify="run the tests"))

    out = capsys.readouterr().out
    assert exit_code == cli.EXIT_OK
    assert "1 fix attempt(s) made" in out
    assert "verified: yes" in out
    assert marker.exists()


def test_ask_verify_reports_failure_once_iterations_are_exhausted(
        tmp_path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    patch_observe(monkeypatch)
    monkeypatch.setattr("harness.agent.Agent.MAX_REPAIRS", 0)
    patch_ollama(monkeypatch, scripted(*plan_replies_with_summaries(
        plan_reply("edit", "add x", [{"id": "edit", "run": "true"}]),
        plan_reply("verify1", "check", [{"id": "verify", "run": "false"}]),
        plan_reply("fix", "fix", [{"id": "fix", "run": "true"}]),
        plan_reply("verify2", "check", [{"id": "verify", "run": "false"}]),
    )))

    exit_code = cli.cmd_ask(ask_args(
        tmp_path, "add x", non_interactive=True, verify="check", max_iterations=1))

    out = capsys.readouterr().out
    assert exit_code == cli.EXIT_FAILED
    assert "verified: no" in out
    assert "exhausted" in out


def test_ask_plan_only_prints_the_plan_and_runs_nothing(
        tmp_path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    patch_observe(monkeypatch)
    marker = tmp_path / "marker"
    patch_ollama(monkeypatch, scripted(plan_reply(
        "t", f"touch {marker}", [{"id": "touch", "run": f"touch {marker}"}])))

    exit_code = cli.cmd_ask(ask_args(tmp_path, f"touch {marker}",
                                     non_interactive=True, plan_only=True))

    out = capsys.readouterr().out
    assert exit_code == cli.EXIT_BLOCKED
    assert "plan only, nothing run" in out
    assert not marker.exists()


def test_ask_non_interactive_blocks_on_the_first_question(
        tmp_path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    patch_observe(monkeypatch)
    patch_ollama(monkeypatch, scripted(
        {"intent": "ask", "message": "need more info",
         "questions": ["Which host?"]}))

    exit_code = cli.cmd_ask(ask_args(tmp_path, "tidy up the box", non_interactive=True))

    out = capsys.readouterr().out
    assert exit_code == cli.EXIT_BLOCKED
    assert "Which host?" in out


def test_ask_loops_across_multiple_rounds_of_questions(
        tmp_path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Regression for the bug found in a full-file audit: a second
    NEEDS_ANSWER round used to fall through to a bare "outcome:
    needs_answer" with the new questions never printed at all."""
    patch_observe(monkeypatch)
    marker = tmp_path / "marker"
    patch_ollama(monkeypatch, scripted(
        {"intent": "ask", "message": "which host?", "questions": ["Which host?"]},
        {"intent": "ask", "message": "which path?", "questions": ["Which path?"]},
        plan_reply("t", f"touch {marker}",
                  [{"id": "touch", "run": f"touch {marker}"}]),
    ))
    monkeypatch.setattr("builtins.input", lambda prompt="": "popos-01")

    exit_code = cli.cmd_ask(ask_args(tmp_path, "tidy up the box"))

    out = capsys.readouterr().out
    assert "Which host?" in out
    assert "Which path?" in out, "the second round's question was dropped"
    assert exit_code == cli.EXIT_OK
    assert marker.exists()


def test_plan_command_is_always_plan_only_and_noninteractive(
        tmp_path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    patch_observe(monkeypatch)
    marker = tmp_path / "marker"
    patch_ollama(monkeypatch, scripted(plan_reply(
        "t", f"touch {marker}", [{"id": "touch", "run": f"touch {marker}"}])))
    args = argparse.Namespace(workdir=str(tmp_path), request=f"touch {marker}",
                              model=None)

    exit_code = cli.cmd_plan(args)

    out = capsys.readouterr().out
    assert exit_code == cli.EXIT_BLOCKED
    assert "plan only" in out
    assert not marker.exists()


def test_ask_answer_intent_prints_the_bare_outcome_and_exits_ok(
        tmp_path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    """No plan at all -- intent 'answer' -- must not reach print_plan()
    or the approval prompt, just report the outcome and stop."""
    patch_observe(monkeypatch)
    patch_ollama(monkeypatch, scripted({"intent": "answer", "message": "40 GB free"}))

    exit_code = cli.cmd_ask(ask_args(tmp_path, "how much disk is free"))

    out = capsys.readouterr().out
    assert exit_code == cli.EXIT_OK
    assert "outcome: answered" in out


def test_ask_reports_the_abort_reason_alongside_the_outcome(
        tmp_path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    """The early-outcome branch's `reason :` line -- an unparseable model
    reply is the simplest way to reach a non-AWAITING_APPROVAL outcome
    that also carries a non-empty abort_reason."""
    patch_observe(monkeypatch)

    def transport(path, payload, timeout):
        if path == "/api/tags":
            return {"models": [{"name": "qwen3:8b"}]}
        return {"message": {"content": "I cannot help with that."}}

    patch_ollama(monkeypatch, transport)

    exit_code = cli.cmd_ask(ask_args(tmp_path, "do something"))

    out = capsys.readouterr().out
    assert exit_code == cli.EXIT_FAILED
    assert "outcome: failed" in out
    assert "reason :" in out


def test_ask_interactive_gives_up_on_eof_while_answering_a_question(
        tmp_path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Interactive mode (not non_interactive) hitting Ctrl+D mid-question
    must bail out the same way non_interactive's early-exit does, rather
    than crash or hang."""
    patch_observe(monkeypatch)
    patch_ollama(monkeypatch, scripted(
        {"intent": "ask", "message": "which host?", "questions": ["Which host?"]}))
    monkeypatch.setattr("builtins.input", lambda prompt="": (_ for _ in ()).throw(EOFError))

    exit_code = cli.cmd_ask(ask_args(tmp_path, "tidy up the box"))

    assert exit_code == cli.EXIT_BLOCKED
    assert "Which host?" in capsys.readouterr().out


def test_ask_reports_a_dangerous_step_awaiting_approval_when_declined(
        tmp_path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Non-interactive mode with an unapproved dangerous step must refuse,
    not silently run it -- the CLI's own version of invariant 2.3."""
    patch_observe(monkeypatch)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me", encoding="utf-8")
    patch_ollama(monkeypatch, scripted(plan_reply(
        "t", f"delete {victim}",
        [{"id": "rm", "run": f"rm -f {victim}", "touches": [str(victim)]}])))

    exit_code = cli.cmd_ask(ask_args(tmp_path, f"delete {victim}", non_interactive=True))

    assert exit_code == cli.EXIT_BLOCKED
    assert victim.exists()


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #


def run_args(workdir: Path, task_file: Path, *, dry_run=False,
             non_interactive=True) -> argparse.Namespace:
    return argparse.Namespace(workdir=str(workdir), task_file=str(task_file),
                              dry_run=dry_run, non_interactive=non_interactive)


def test_run_executes_a_hand_written_task_file_with_no_model(
        tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    """The whole point of `run`: it must work with Ollama untouched."""
    marker = tmp_path / "marker"
    task_file = write_task_file(
        tmp_path / "task.yaml", "t", f"touch {marker}",
        [{"id": "touch", "run": f"touch {marker}"}])

    exit_code = cli.cmd_run(run_args(tmp_path, task_file))

    out = capsys.readouterr().out
    assert exit_code == cli.EXIT_OK
    assert "outcome: completed" in out


def test_run_reports_per_step_results_and_the_audit_path(
        tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    """`cmd_run` used to hand-roll its own post-execution print instead of
    reusing `_print_execution_report` -- it showed only the bare outcome,
    with no per-step ok/FAIL breakdown and no audit path, unlike `harness
    ask`. A failed or partially-run task file gave no way to tell which
    step failed without opening the audit JSON by hand."""
    marker = tmp_path / "marker"
    task_file = write_task_file(
        tmp_path / "task.yaml", "t", f"touch {marker}",
        [{"id": "touch", "run": f"touch {marker}"}])

    exit_code = cli.cmd_run(run_args(tmp_path, task_file))

    out = capsys.readouterr().out
    assert exit_code == cli.EXIT_OK
    assert "ok   touch" in out
    assert "audit  :" in out
    assert marker.exists()


def test_run_prints_the_rollback_summary_line_after_a_failure(
        tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    """`_print_execution_report`'s rollback line -- untested before this,
    despite failure/rollback itself being covered at the pipeline level
    (test_pipeline.py::test_a_failed_step_reverts_a_file_nobody_registered)."""
    conf = tmp_path / "app.conf"
    conf.write_text("ORIGINAL\n", encoding="utf-8")
    task_file = write_task_file(
        tmp_path / "task.yaml", "t", f"rewrite {conf} then fail",
        [{"id": "write", "kind": "write", "run": f"printf 'NEW\\n' > {conf}",
          "touches": [str(conf)]},
         {"id": "boom", "run": "exit 7"}])

    exit_code = cli.cmd_run(run_args(tmp_path, task_file))

    out = capsys.readouterr().out
    assert exit_code == cli.EXIT_FAILED
    assert "rollback:" in out
    assert "undone" in out
    assert conf.read_text(encoding="utf-8") == "ORIGINAL\n"


def test_run_still_writes_an_audit_record_when_preconditions_fail(
        tmp_path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """`cmd_run` used to print "preconditions failed: ..." and return
    directly, never calling `_finish()` -- the same choke point
    `Harness.plan()` uses for the identical case. The full decision trail
    `_gate()` already built (every per-step classification, the precondition
    failure itself) was silently discarded: no audit JSON, no telemetry
    record, for every precondition-blocked run file."""
    from harness.gates import StateVerifier

    monkeypatch.setattr(StateVerifier, "disk_free_gb", staticmethod(lambda path="/": 0.0))
    marker = tmp_path / "marker"
    task_file = write_task_file(
        tmp_path / "task.yaml", "t", f"touch {marker}",
        [{"id": "touch", "run": f"touch {marker}"}])

    exit_code = cli.cmd_run(run_args(tmp_path, task_file))

    out = capsys.readouterr().out
    assert exit_code == cli.EXIT_BLOCKED
    assert "audit  :" in out
    audit_files = list((tmp_path / "audit").glob("*.audit.json"))
    assert len(audit_files) == 1
    telemetry = (tmp_path / "telemetry" / "tasks.jsonl").read_text(encoding="utf-8")
    assert '"status": "blocked"' in telemetry
    assert not marker.exists()


def test_run_rejects_a_malformed_task_file(
        tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump({"task_id": "t"}), encoding="utf-8")

    exit_code = cli.cmd_run(run_args(tmp_path, bad))

    err = capsys.readouterr().err
    assert exit_code == cli.EXIT_USAGE
    assert "missing required key" in err


def test_run_refuses_an_unapproved_dangerous_step(
        tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me", encoding="utf-8")
    task_file = write_task_file(
        tmp_path / "task.yaml", "t", f"delete {victim}",
        [{"id": "rm", "run": f"rm -f {victim}", "touches": [str(victim)]}])

    exit_code = cli.cmd_run(run_args(tmp_path, task_file))

    assert exit_code == cli.EXIT_BLOCKED
    assert victim.exists()


def test_run_dry_run_does_not_touch_the_filesystem(tmp_path) -> None:
    marker = tmp_path / "marker"
    task_file = write_task_file(
        tmp_path / "task.yaml", "t", f"touch {marker}",
        [{"id": "touch", "run": f"touch {marker}"}])

    exit_code = cli.cmd_run(run_args(tmp_path, task_file, dry_run=True))

    assert exit_code == cli.EXIT_OK
    assert not marker.exists()


# --------------------------------------------------------------------------- #
# check
# --------------------------------------------------------------------------- #


def check_args(task_file: Path, *, verbose=False) -> argparse.Namespace:
    return argparse.Namespace(task_file=str(task_file), verbose=verbose)


def test_check_rejects_a_malformed_task_file(
        tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump({"task_id": "t"}), encoding="utf-8")

    exit_code = cli.cmd_check(check_args(bad))

    err = capsys.readouterr().err
    assert exit_code == cli.EXIT_USAGE
    assert "missing required key" in err


def test_check_passes_a_clean_specific_task(
        tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    marker = tmp_path / "marker"
    task_file = write_task_file(
        tmp_path / "task.yaml", "t", f"touch {marker}",
        [{"id": "touch", "run": f"touch {marker}"}])

    exit_code = cli.cmd_check(check_args(task_file))

    out = capsys.readouterr().out
    assert exit_code == cli.EXIT_OK
    assert "0 question(s), 0 command(s) need confirmation" in out
    assert not marker.exists(), "check must never run anything"


def test_check_flags_a_dangerous_command_and_a_vague_request(
        tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    task_file = write_task_file(
        tmp_path / "task.yaml", "t", "clean up the old stuff",
        [{"id": "rm", "run": "rm -rf /var/lib/something"}])

    exit_code = cli.cmd_check(check_args(task_file))

    out = capsys.readouterr().out
    assert exit_code == cli.EXIT_BLOCKED
    assert "BLOCK" in out
    assert "question(s)" in out


def test_check_verbose_prints_the_classifier_reasons(
        tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    task_file = write_task_file(
        tmp_path / "task.yaml", "t", "delete the log",
        [{"id": "rm", "run": "rm -rf /var/log/app"}])

    cli.cmd_check(check_args(task_file, verbose=True))

    out = capsys.readouterr().out
    assert "recursive" in out.lower() or "delete" in out.lower()


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #


def test_doctor_reports_python_and_ollama_status(
        tmp_path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    patch_ollama(monkeypatch, scripted(models=["qwen3:32b"]))
    args = argparse.Namespace(workdir=str(tmp_path / "wd"))

    exit_code = cli.cmd_doctor(args)

    out = capsys.readouterr().out
    assert exit_code == cli.EXIT_OK
    assert "python >= 3.10" in out
    assert "ollama at" in out


def test_doctor_reports_a_missing_optional_module(
        tmp_path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    """`inotify_simple` genuinely is installed in this venv, so its
    ImportError branch never fires without simulating the absence."""
    import builtins

    patch_ollama(monkeypatch, scripted(models=["qwen3:32b"]))
    real_import = builtins.__import__

    def broken_import(name, *args, **kwargs):
        if name == "inotify_simple":
            raise ImportError("simulated: not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", broken_import)
    args = argparse.Namespace(workdir=str(tmp_path / "wd"))

    exit_code = cli.cmd_doctor(args)

    out = capsys.readouterr().out
    assert exit_code == cli.EXIT_OK
    assert "missing: optional, falls back to full walks" in out


def test_doctor_reports_a_failing_reflink_probe(
        tmp_path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    patch_ollama(monkeypatch, scripted(models=["qwen3:32b"]))

    def broken_run(*args, **kwargs):
        raise FileNotFoundError("cp not found")

    monkeypatch.setattr("harness.cli.subprocess.run", broken_run)
    args = argparse.Namespace(workdir=str(tmp_path / "wd"))

    exit_code = cli.cmd_doctor(args)

    out = capsys.readouterr().out
    assert exit_code == cli.EXIT_OK
    assert "cp --reflink" in out
    assert "FileNotFoundError" in out


def test_doctor_prints_the_fallback_note_when_a_check_fails(
        tmp_path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    def broken_transport(path, payload, timeout):
        raise ConnectionRefusedError("connection refused")

    patch_ollama(monkeypatch, broken_transport)
    args = argparse.Namespace(workdir=str(tmp_path / "wd"))

    exit_code = cli.cmd_doctor(args)

    out = capsys.readouterr().out
    assert exit_code == cli.EXIT_OK
    assert "blocks planning but not" in out


def test_doctor_workdir_check_walks_up_to_an_existing_ancestor(
        tmp_path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Found in a full-file audit: a missing workdir.parent used to report
    "ok" unconditionally, without checking whether the nearest real
    ancestor could actually have it created inside it."""
    patch_ollama(monkeypatch, scripted(models=[]))
    deeply_missing = tmp_path / "does" / "not" / "exist" / "wd"
    args = argparse.Namespace(workdir=str(deeply_missing))

    exit_code = cli.cmd_doctor(args)

    out = capsys.readouterr().out
    assert exit_code == cli.EXIT_OK
    assert "workdir writable" in out
    assert "ok" in out.lower()


# --------------------------------------------------------------------------- #
# label
# --------------------------------------------------------------------------- #


def label_args(workdir: Path, *, limit=25) -> argparse.Namespace:
    return argparse.Namespace(workdir=str(workdir), limit=limit)


def test_label_with_nothing_pending_says_so(
        tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli.cmd_label(label_args(tmp_path))

    out = capsys.readouterr().out
    assert exit_code == cli.EXIT_OK
    assert "nothing to label" in out


def test_label_records_ambiguous_and_specific_answers(
        tmp_path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    logger = TaskLogger(tmp_path / "telemetry" / "tasks.jsonl")
    logger.record(TaskRecord(task_id="t1", request="clean up the box", at=1.0))
    logger.record(TaskRecord(task_id="t2", request=f"touch {tmp_path}/x", at=1.0))
    answers = iter(["a", "vague target", "s"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    exit_code = cli.cmd_label(label_args(tmp_path))

    out = capsys.readouterr().out
    assert exit_code == cli.EXIT_OK
    assert "labeled 2 this session" in out
    labels = LabelStore(tmp_path / "telemetry" / "labels.jsonl").all()
    assert len(labels) == 2


def test_label_quit_stops_early_without_losing_prior_labels(
        tmp_path, monkeypatch) -> None:
    logger = TaskLogger(tmp_path / "telemetry" / "tasks.jsonl")
    logger.record(TaskRecord(task_id="t1", request="clean up the box", at=1.0))
    logger.record(TaskRecord(task_id="t2", request="tidy the thing", at=1.0))
    answers = iter(["q"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    exit_code = cli.cmd_label(label_args(tmp_path))

    assert exit_code == cli.EXIT_OK
    assert LabelStore(tmp_path / "telemetry" / "labels.jsonl").all() == {}


def test_label_eof_on_the_verdict_prompt_stops_without_losing_prior_labels(
        tmp_path, monkeypatch) -> None:
    logger = TaskLogger(tmp_path / "telemetry" / "tasks.jsonl")
    logger.record(TaskRecord(task_id="t1", request="clean up the box", at=1.0))
    logger.record(TaskRecord(task_id="t2", request="tidy the thing", at=1.0))
    answers = iter(["s"])

    def fake_input(prompt: str = "") -> str:
        try:
            return next(answers)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", fake_input)

    exit_code = cli.cmd_label(label_args(tmp_path))

    assert exit_code == cli.EXIT_OK
    assert len(LabelStore(tmp_path / "telemetry" / "labels.jsonl").all()) == 1


def test_label_eof_on_the_why_prompt_stops_without_losing_prior_labels(
        tmp_path, monkeypatch) -> None:
    logger = TaskLogger(tmp_path / "telemetry" / "tasks.jsonl")
    logger.record(TaskRecord(task_id="t1", request="clean up the box", at=1.0))
    logger.record(TaskRecord(task_id="t2", request="tidy the thing", at=1.0))
    answers = iter(["s", "a"])

    def fake_input(prompt: str = "") -> str:
        try:
            return next(answers)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", fake_input)

    exit_code = cli.cmd_label(label_args(tmp_path))

    assert exit_code == cli.EXIT_OK
    assert len(LabelStore(tmp_path / "telemetry" / "labels.jsonl").all()) == 1


# --------------------------------------------------------------------------- #
# tui dispatch
# --------------------------------------------------------------------------- #


def test_tui_launches_when_textual_is_importable(tmp_path, monkeypatch) -> None:
    called: dict[str, object] = {}
    monkeypatch.setattr("harness.tui.run_tui", lambda workdir=None, harness=None:
                        called.setdefault("workdir", workdir))

    exit_code = cli.cmd_tui(argparse.Namespace(workdir=str(tmp_path)))

    assert exit_code == cli.EXIT_OK
    assert called["workdir"] == tmp_path


def test_cmd_tui_builds_the_harness_through_build_harness(tmp_path, monkeypatch) -> None:
    """`harness tui` used to build its own bare Harness(), a second,
    independent construction path from build_harness() -- silently skipping
    both the disk-free precondition and OpenRouter registration
    (HANDOFF.md §2.27). Proven here by checking for the precondition only
    build_harness() attaches, not by re-asserting build_harness()'s own
    internals."""
    captured: dict[str, object] = {}
    monkeypatch.setattr("harness.tui.run_tui", lambda workdir=None, harness=None:
                        captured.setdefault("harness", harness))

    cli.cmd_tui(argparse.Namespace(workdir=str(tmp_path)))

    harness = captured["harness"]
    assert isinstance(harness, Harness)
    assert any(p.name == "disk:/" for p in harness.verifier.preconditions)


def test_tui_reports_usage_error_when_textual_is_missing(tmp_path, monkeypatch,
                                                          capsys) -> None:
    monkeypatch.setitem(__import__("sys").modules, "harness.tui", None)

    exit_code = cli.cmd_tui(argparse.Namespace(workdir=str(tmp_path)))

    err = capsys.readouterr().err
    assert exit_code == cli.EXIT_USAGE
    assert "textual is not installed" in err


# --------------------------------------------------------------------------- #
# parser and main()
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("argv,expected_func", [
    (["ask", "do a thing"], cli.cmd_ask),
    (["plan", "do a thing"], cli.cmd_plan),
    (["run", "task.yaml"], cli.cmd_run),
    (["check", "task.yaml"], cli.cmd_check),
    (["doctor"], cli.cmd_doctor),
    (["evidence"], cli.cmd_evidence),
    (["label"], cli.cmd_label),
    (["tui"], cli.cmd_tui),
])
def test_build_parser_dispatches_every_subcommand(argv, expected_func) -> None:
    args = cli.build_parser().parse_args(argv)
    assert args.func is expected_func


def test_main_with_no_subcommand_defaults_to_tui(monkeypatch) -> None:
    called = {}

    def fake_cmd_tui(args):
        called["ran"] = True
        return 0

    monkeypatch.setattr(cli, "cmd_tui", fake_cmd_tui)

    exit_code = cli.main([])

    assert exit_code == 0
    assert called.get("ran")


def test_main_converts_a_task_spec_error_to_usage_exit(
        tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump({"task_id": "t"}), encoding="utf-8")

    exit_code = cli.main(["--workdir", str(tmp_path), "run", str(bad)])

    assert exit_code == cli.EXIT_USAGE


def test_main_catches_a_task_spec_error_that_escapes_a_command(
        monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    """`main()`'s own TaskSpecError handler is a second, outer safety net --
    every current command that can raise one (`run`, `check`) already
    catches it internally and never lets it reach here. Monkeypatches a
    command to raise directly, the same way the unhandled-exception test
    below does for a bare Exception."""
    from harness.core import TaskSpecError

    def boom(args):
        raise TaskSpecError("simulated: escaped a command's own catch")

    monkeypatch.setattr(cli, "cmd_doctor", boom)

    exit_code = cli.main(["doctor"])

    err = capsys.readouterr().err
    assert exit_code == cli.EXIT_USAGE
    assert "simulated: escaped" in err


def test_main_converts_an_unhandled_exception_to_a_failed_exit(
        monkeypatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Explicit --workdir, not the bare ["doctor"] this test used to pass --
    once main()'s except block unconditionally constructs a CrashReporter
    against args.workdir, omitting it here would write a real file under
    this machine's actual ~/.harness/telemetry/crashes.jsonl on every run."""
    def boom(args):
        raise RuntimeError("something broke")

    monkeypatch.setattr(cli, "cmd_doctor", boom)

    exit_code = cli.main(["--workdir", str(tmp_path), "doctor"])

    err = capsys.readouterr().err
    assert exit_code == cli.EXIT_FAILED
    assert "RuntimeError" in err
    assert (tmp_path / "telemetry" / "crashes.jsonl").exists(), \
        "the crash should have been logged under the given workdir"
    assert f"logged to {tmp_path / 'telemetry' / 'crashes.jsonl'}" in err


def test_main_logs_the_full_crash_record_with_site_and_traceback(
        monkeypatch, tmp_path: Path) -> None:
    def boom(args):
        raise RuntimeError("something broke")

    monkeypatch.setattr(cli, "cmd_ask", boom)

    cli.main(["--workdir", str(tmp_path), "ask", "do something"])

    record = CrashReporter(tmp_path / "telemetry" / "crashes.jsonl").read()[0]
    assert record.site == "cli:ask"
    assert record.exception_type == "RuntimeError"
    assert record.message == "something broke"
    assert "Traceback" in record.traceback


def test_main_with_no_subcommand_that_crashes_logs_site_tui(
        monkeypatch, tmp_path: Path) -> None:
    def boom(args):
        raise RuntimeError("tui blew up")

    monkeypatch.setattr(cli, "cmd_tui", boom)

    cli.main(["--workdir", str(tmp_path)])

    record = CrashReporter(tmp_path / "telemetry" / "crashes.jsonl").read()[0]
    assert record.site == "cli:tui"


def test_main_reports_interrupted_on_ctrl_c(monkeypatch,
                                            capsys: pytest.CaptureFixture[str]) -> None:
    def interrupt(args):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "cmd_doctor", interrupt)

    exit_code = cli.main(["doctor"])

    err = capsys.readouterr().err
    assert exit_code == cli.EXIT_USAGE
    assert "interrupted" in err


class TestLoadEnvFile:
    """cli._load_env_file(): OPENROUTER_API_KEY (and anything else) from a
    .env file at the repo root -- never required, never overrides a
    variable already set in the real environment."""

    def test_a_variable_not_already_set_gets_loaded(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        (tmp_path / ".env").write_text("OPENROUTER_API_KEY=from-dotenv\n")

        cli._load_env_file(tmp_path / ".env")

        assert os.environ["OPENROUTER_API_KEY"] == "from-dotenv"

    def test_an_explicit_export_is_never_overridden(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "from-the-real-shell")
        (tmp_path / ".env").write_text("OPENROUTER_API_KEY=from-dotenv\n")

        cli._load_env_file(tmp_path / ".env")

        assert os.environ["OPENROUTER_API_KEY"] == "from-the-real-shell"

    def test_a_missing_file_is_a_silent_no_op(self, tmp_path: Path) -> None:
        cli._load_env_file(tmp_path / "does-not-exist.env")  # must not raise

    def test_main_loads_it_before_doing_anything_else(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[bool] = []
        monkeypatch.setattr(cli, "_load_env_file", lambda *a, **k: calls.append(True))
        monkeypatch.setattr(cli, "cmd_doctor", lambda args: 0)

        cli.main(["doctor"])

        assert calls == [True]
