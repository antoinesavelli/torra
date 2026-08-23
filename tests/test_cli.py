"""cli.py: the actual command-line entry points a user runs.

Originally scoped to just the LoopholeAudit wiring found in a full-file
audit -- this file now covers the rest of cli.py's surface too
(cmd_ask/plan/run/check/doctor/label, build_parser, main), closing a gap
this file's own docstring used to flag as separate, pending work.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest
import yaml
from conftest import plan_reply, scripted

import harness.cli as cli
from harness.core import OllamaClient
from harness.telemetry import LabelStore, TaskLogger, TaskRecord

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


# --------------------------------------------------------------------------- #
# ask / plan
# --------------------------------------------------------------------------- #


def ask_args(workdir: Path, request: str, *, model=None, dry_run=False,
             non_interactive=False, plan_only=False) -> argparse.Namespace:
    return argparse.Namespace(workdir=str(workdir), request=request, model=model,
                              dry_run=dry_run, non_interactive=non_interactive,
                              plan_only=plan_only)


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


# --------------------------------------------------------------------------- #
# tui dispatch
# --------------------------------------------------------------------------- #


def test_tui_launches_when_textual_is_importable(tmp_path, monkeypatch) -> None:
    called = {}
    monkeypatch.setattr("harness.tui.run_tui", lambda workdir=None, harness=None:
                        called.setdefault("workdir", workdir))

    exit_code = cli.cmd_tui(argparse.Namespace(workdir=str(tmp_path)))

    assert exit_code == cli.EXIT_OK
    assert called["workdir"] == tmp_path


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


def test_main_converts_an_unhandled_exception_to_a_failed_exit(
        monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    def boom(args):
        raise RuntimeError("something broke")

    monkeypatch.setattr(cli, "cmd_doctor", boom)

    exit_code = cli.main(["doctor"])

    err = capsys.readouterr().err
    assert exit_code == cli.EXIT_FAILED
    assert "RuntimeError" in err


def test_main_reports_interrupted_on_ctrl_c(monkeypatch,
                                            capsys: pytest.CaptureFixture[str]) -> None:
    def interrupt(args):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "cmd_doctor", interrupt)

    exit_code = cli.main(["doctor"])

    err = capsys.readouterr().err
    assert exit_code == cli.EXIT_USAGE
    assert "interrupted" in err
