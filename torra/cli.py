#!/usr/bin/env python3
"""Entry points: torra / ask / plan / run / check / doctor / evidence / label.

Depends on: torra.core, torra.gates, torra.pipeline, torra.telemetry,
python-dotenv (optional `.env` loading for OPENROUTER_API_KEY and similar).

`run` and `check` take a hand-written YAML file and never touch the model, so
the gates stay available when the model is down, wrong, or simply not needed.
Exit codes: 0 ok, 1 usage error, 2 stopped by a gate or unapproved, 3 failed.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path

from dotenv import load_dotenv

from .core import (
    ModelBackend,
    OllamaClient,
    OllamaStatus,
    OpenRouterClient,
    TaskSpec,
    TaskSpecError,
)
from .gates import Clarifier, Precondition, SafetyClassifier, StateVerifier
from .iterate import iterate
from .pipeline import Outcome, Plan, RunResult, Torra
from .telemetry import (
    CorpusExporter,
    CrashReporter,
    LabelStore,
    LoopholeAudit,
    TaskLogger,
    crash_summary,
    health_summary,
)

__all__ = ["build_parser", "main"]

EXIT_OK, EXIT_USAGE, EXIT_BLOCKED, EXIT_FAILED = 0, 1, 2, 3
DEFAULT_WORKDIR = Path.home() / ".torra"

#: Every Outcome value maps to an exit code, including "needs_answer" --
#: unreachable at every current .get() call site (cmd_ask's while loop
#: always resolves it to a different outcome first), kept anyway so this
#: stays a complete Outcome -> exit code table rather than one that is
#: silently wrong for the one member no current caller happens to hit.
OUTCOME_EXIT: dict[str, int] = {
    "completed": EXIT_OK,
    "answered": EXIT_OK,
    "needs_answer": EXIT_BLOCKED,
    "awaiting_approval": EXIT_BLOCKED,
    "rejected": EXIT_BLOCKED,
    "refused": EXIT_BLOCKED,
    "blocked": EXIT_BLOCKED,
    "failed": EXIT_FAILED,
    "backend_down": EXIT_FAILED,
}


def build_torra(workdir: Path, offline: bool = False) -> Torra:
    """Assemble the stack with a sensible default precondition.

    "openrouter" is registered unconditionally, key or no key -- found live
    by manual testing, not by a test: registering it only when
    `OPENROUTER_API_KEY` was set meant an unconfigured `--model
    openrouter/...` request didn't match any prefix in `Agent.backends`, so
    `Agent.resolve_backend` fell through to `self.ollama` with the literal
    string "openrouter/..." as the model name -- which `converse()`'s
    degraded-daemon fallback then silently swapped for a real local model,
    with no error at all. The operator's explicit backend choice vanished
    without a trace. `OpenRouterClient.chat()` already raises a specific
    "OPENROUTER_API_KEY is not set" error when actually called without one
    -- registering it always is what lets that error fire, instead of the
    request being silently misrouted before it ever gets there (HANDOFF.md
    §2.27). Constructing it is free either way: no network call happens
    until `.chat()` is actually invoked, so this is harmless in `offline`
    mode too (`torra run`, which never calls the model at all). This is
    the only place that constructs it: `torra tui` must reach it through
    here (`cmd_tui`), not build its own `Torra` independently, or it
    would silently never see it either.
    """
    verifier = StateVerifier()
    verifier.add(Precondition(
        "disk:/", lambda: StateVerifier.disk_free_gb("/") > 1.0,
        "at least 1 GB free on /", ttl_seconds=30))
    ollama = (OllamaClient(transport=lambda p, pay, t: {"models": []})
              if offline else OllamaClient())
    backends: dict[str, ModelBackend] = {"openrouter": OpenRouterClient()}
    return Torra(workdir=workdir, ollama=ollama, verifier=verifier, backends=backends)


def print_plan(plan: Plan) -> None:
    """Render a gated plan for a terminal."""
    print(f"\ntask   : {plan.spec.task_id}")
    print(f"request: {plan.spec.request}")
    print(f"model  : {plan.model}  worst: {plan.worst_level.value}"
          + (f"  ({plan.repairs} repair(s))" if plan.repairs else ""))
    print(f"undo   : {plan.coverage.get('coverage_ratio', 0):.0%} coverage"
          + (f", none for {plan.uncovered}" if plan.uncovered else ""))
    print()
    for index, sp in enumerate(plan.steps, start=1):
        flag = "CONFIRM" if sp.needs_confirmation else "   ok  "
        print(f"  {index}. [{flag}] {sp.verdict.level.value:9} {sp.step.step_id}")
        print(f"       {sp.step.command}")
        if sp.step.description and sp.step.description != sp.step.command:
            print(f"       -- {sp.step.description}")
        if sp.undos:
            print(f"       undo: {sp.undos[0].description[:70]}")
        else:
            print("       undo: none derivable")


def print_dependency_warnings(plan: Plan, approvals: dict[str, bool]) -> None:
    """Show any Torra.dependency_warnings() hits before execute() runs --
    the whole point is the operator sees this while they can still change
    their mind, not buried in the execution report after the fact."""
    for warning in Torra.dependency_warnings(plan, approvals):
        print(f"  warning: {warning}")


def approve_at_prompt(plan: Plan) -> dict[str, bool]:
    """Ask on stdin about every step that needs confirmation."""
    approvals: dict[str, bool] = {}
    for sp in plan.needs_confirmation:
        print(f"\n  [{sp.verdict.level.value.upper()}] {sp.step.command}")
        if sp.step.description and sp.step.description != sp.step.command:
            print(f"    -- {sp.step.description}")
        for reason in sp.verdict.reasons[:3]:
            print(f"    {reason}")
        if not sp.undos:
            print("    no automatic undo for this step")
        try:
            answer = input("    approve? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            # Bail out of the whole loop, not just this step -- matches
            # _collect_answers' identical keystroke handling elsewhere in
            # this file. Every step from here on (including this one)
            # stays refused simply by never being added to the map:
            # Torra._apply_approvals() already treats "absent" the same
            # as "False" (§2.3), so no explicit refusal is needed.
            print()
            break
        approvals[sp.step.step_id] = answer in ("y", "yes")
    return approvals


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def cmd_tui(args: argparse.Namespace) -> int:
    """Launch the terminal interface."""
    try:
        from .tui import run_tui
    except ImportError:
        print("error: textual is not installed. pip install textual",
              file=sys.stderr)
        return EXIT_USAGE
    workdir = Path(args.workdir)
    # build_torra(), not a bare Torra(workdir=...) -- that used to be
    # `run_tui`'s own default (`torra=None`), a second, independent
    # construction path that skipped both this function's disk-free
    # precondition and, since HANDOFF.md §2.27, OpenRouter registration
    # entirely. `torra tui` is the actual default entry point; it must go
    # through the one place that reads OPENROUTER_API_KEY, not a copy of it.
    run_tui(workdir=workdir, torra=build_torra(workdir))
    return EXIT_OK


def _collect_answers(result: RunResult, non_interactive: bool) -> tuple[list[str], int | None]:
    """Prompt for answers to the current NEEDS_ANSWER round's questions.

    Returns (answers, early_exit_code); the caller must return
    early_exit_code immediately, without calling torra.answer(), when
    it's not None.
    """
    print("\nquestions:")
    for question in (result.turn.questions if result.turn else []):
        print(f"  ? {question}")
    if non_interactive:
        return [], EXIT_BLOCKED
    answers = []
    for question in (result.turn.questions if result.turn else []):
        try:
            answers.append(input(f"  {question}\n  > ").strip())
        except (EOFError, KeyboardInterrupt):
            return [], EXIT_BLOCKED
    return answers, None


def _print_execution_report(done: RunResult) -> None:
    print(f"\noutcome: {done.outcome.value}")
    for step_result in done.results:
        print(f"  {'ok  ' if step_result.ok else 'FAIL'} {step_result.step_id}"
              + (f": {step_result.error}" if step_result.error else ""))
    if done.summary:
        print(f"\n{done.summary}")
    if done.abort_reason:
        print(f"reason : {done.abort_reason}")
    if done.rollback:
        # `cancelled` (an unsafe undo the operator declined) used to be silently
        # missing from this line entirely -- since no caller anywhere wires up
        # RollbackManager's confirm_unsafe, every unsafe rollback action is
        # auto-declined by that constructor's own default (`lambda action:
        # False`), and this line reported "0 undone, 0 unverified, 0 failed"
        # for that run -- reading as fully clean when a declared rollback
        # action never ran at all. Found live: a task.yaml with an explicit
        # `rm -f <path>` rollback entry (classified critical, so `safe` in the
        # spec was overridden to effectively unsafe) left the target file in
        # place after a deliberate failure, invisible in this summary and
        # visible only in the audit JSON's rollback.results[].status.
        print(f"rollback: {len(done.rollback.undone)} undone, "
              f"{len(done.rollback.unverified)} unverified, "
              f"{len(done.rollback.cancelled)} declined, "
              f"{len(done.rollback.failed)} failed")
    if done.audit_path:
        print(f"audit  : {done.audit_path}")


def _approve_for_ask(args: argparse.Namespace) -> Callable[[Plan], dict[str, bool]]:
    """The same approval policy the single-round path below uses."""
    return (lambda plan: {}) if args.non_interactive else approve_at_prompt


def cmd_ask_with_verify(torra: Torra, args: argparse.Namespace) -> int:
    """`--verify`: the request, then a bounded (verify, fix)* cycle -- see
    iterate.py for why this is still just ordinary plan()/execute() turns
    on the same conversation, not a second way to run a command."""
    report = iterate(
        torra, args.request, args.verify, _approve_for_ask(args),
        model=args.model, dry_run=args.dry_run,
        max_iterations=args.max_iterations,
        on_round=lambda r: _print_execution_report(r.last) if r.last else None)
    if report.fix_attempts:
        print(f"\n{report.fix_attempts} fix attempt(s) made")
    if report.verified:
        print("verified: yes")
        return EXIT_OK
    print(f"verified: no ({report.give_up_reason})")
    return EXIT_FAILED


def cmd_ask(args: argparse.Namespace) -> int:
    """One request, planned and approved at the prompt."""
    torra = build_torra(Path(args.workdir))

    if getattr(args, "verify", None):
        return cmd_ask_with_verify(torra, args)

    result = torra.plan(args.request, model=args.model)

    if result.turn and result.turn.message:
        print(f"\n{result.turn.message}")

    # A loop, not a single check: torra.answer() re-plans through the full
    # pipeline, and a model unsatisfied by the first round's answers can
    # come back with NEEDS_ANSWER again -- found in a full-file audit, see
    # _collect_answers.
    while result.outcome is Outcome.NEEDS_ANSWER:
        answers, early_exit = _collect_answers(result, args.non_interactive)
        if early_exit is not None:
            return early_exit
        result = torra.answer(result, answers, model=args.model)

    if result.outcome is not Outcome.AWAITING_APPROVAL or result.plan is None:
        print(f"\noutcome: {result.outcome.value}")
        if result.abort_reason:
            print(f"reason : {result.abort_reason}")
        return OUTCOME_EXIT.get(result.outcome.value, EXIT_FAILED)

    print_plan(result.plan)
    if args.plan_only:
        print("\n(plan only, nothing run)")
        return EXIT_BLOCKED

    approvals = {} if args.non_interactive else approve_at_prompt(result.plan)
    print_dependency_warnings(result.plan, approvals)
    done = torra.execute(result, approvals, dry_run=args.dry_run)
    _print_execution_report(done)
    return OUTCOME_EXIT.get(done.outcome.value, EXIT_FAILED)


def cmd_plan(args: argparse.Namespace) -> int:
    """Plan only, print it, run nothing."""
    args.plan_only = True
    args.dry_run = False
    args.non_interactive = True
    return cmd_ask(args)


def cmd_run(args: argparse.Namespace) -> int:
    """Run a hand-written task file, with no model involved."""
    try:
        spec = TaskSpec.load(args.task_file)
    except TaskSpecError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    torra = build_torra(Path(args.workdir), offline=True)
    started = time.perf_counter()
    plan = torra._gate(spec)
    print_plan(plan)

    result = RunResult(request=spec.request, outcome=Outcome.AWAITING_APPROVAL,
                       stages=["gate", "present"], plan=plan)
    if plan.preconditions and not plan.preconditions.passed:
        result.outcome = Outcome.BLOCKED
        result.abort_reason = "preconditions failed: " + ", ".join(
            r.name for r in plan.preconditions.blocking_failures)
        # Same choke point Torra.plan() uses for the identical case --
        # calling _finish() (not printing and returning) is what actually
        # writes the audit record and telemetry entry; skipping it here
        # silently discarded both for every precondition-blocked run file.
        done = torra._finish(result, started)
        _print_execution_report(done)
        return EXIT_BLOCKED

    approvals = {} if args.non_interactive else approve_at_prompt(plan)
    print_dependency_warnings(plan, approvals)
    done = torra.execute(result, approvals, dry_run=args.dry_run)
    _print_execution_report(done)
    return OUTCOME_EXIT.get(done.outcome.value, EXIT_FAILED)


def cmd_check(args: argparse.Namespace) -> int:
    """Classify a task file without running or prompting."""
    try:
        spec = TaskSpec.load(args.task_file)
    except TaskSpecError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    clarifier, safety = Clarifier(), SafetyClassifier()
    print(f"task   : {spec.task_id}\nrequest: {spec.request}\n")
    print(clarifier.explain(spec.request))
    print("\ncommands:")
    blocked = 0
    for command in spec.commands():
        verdict = safety.classify(command)
        blocked += 1 if verdict.needs_confirmation else 0
        print(f"  {'BLOCK' if verdict.needs_confirmation else '  ok '} "
              f"{verdict.level.value:9} {command}")
        if args.verbose:
            for reason in verdict.reasons[:3]:
                print(f"             {reason}")
    gaps = clarifier.detect(spec.request)
    print(f"\n{len(gaps)} question(s), {blocked} command(s) need confirmation")
    return EXIT_BLOCKED if (gaps or blocked) else EXIT_OK


def _check_python_and_modules() -> list[tuple[str, bool, str]]:
    checks: list[tuple[str, bool, str]] = [
        ("python >= 3.10", sys.version_info >= (3, 10),
         ".".join(map(str, sys.version_info[:3]))),
    ]
    for module, note in (("yaml", "required for task files"),
                         ("textual", "required for the terminal interface"),
                         ("inotify_simple", "optional, falls back to full walks")):
        try:
            __import__(module)
            checks.append((module, True, note))
        except ImportError:
            checks.append((module, False, f"missing: {note}"))
    return checks


def _check_workdir_writable(workdir: Path) -> tuple[str, bool, str]:
    """Walk up to the nearest existing ancestor rather than defaulting to
    True when workdir.parent doesn't exist -- that default reported "ok"
    for a workdir whose whole directory chain was missing and might not
    even be creatable (e.g. under a read-only ancestor), without ever
    checking whether creating it would actually succeed. Found in a
    full-file audit.
    """
    nearest = workdir.parent
    while not nearest.exists() and nearest != nearest.parent:
        nearest = nearest.parent
    return "workdir writable", os.access(nearest, os.W_OK), str(workdir)


def _check_reflink() -> tuple[str, bool, str]:
    try:
        probe = subprocess.run(["cp", "--reflink=always", "--help"],
                               capture_output=True, timeout=5)
        return ("cp --reflink", probe.returncode == 0,
               "shadow snapshots may clone instead of copy")
    except Exception as exc:
        return "cp --reflink", False, type(exc).__name__


def _check_ollama() -> tuple[str, bool, str]:
    ollama = OllamaClient()
    status = ollama.health()
    models = ollama.models() if status is OllamaStatus.HEALTHY else []
    detail = (", ".join(models) if models
             else status.value + (f" ({ollama.last_error})" if ollama.last_error else ""))
    return f"ollama at {ollama.host}", status is OllamaStatus.HEALTHY, detail


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check that the environment can run this."""
    workdir = Path(args.workdir)
    checks = _check_python_and_modules()
    checks.append(_check_workdir_writable(workdir))
    checks.append(_check_reflink())
    checks.append(_check_ollama())

    width = max(len(n) for n, _o, _d in checks)
    failed = sum(0 if ok else 1 for _n, ok, _d in checks)
    for name, ok, detail in checks:
        print(f"  {'ok  ' if ok else 'WARN'}  {name:<{width}}  {detail}")
    print(f"\n{len(checks) - failed}/{len(checks)} checks passed")
    if failed:
        print("yaml blocks task files, textual blocks the interface, and a "
              "missing Ollama blocks planning but not `run`. Everything else "
              "has a documented fallback.")
    return EXIT_OK


def cmd_evidence(args: argparse.Namespace) -> int:
    """Print what torra has recorded about itself."""
    workdir = Path(args.workdir)
    logger = TaskLogger(workdir / "telemetry" / "tasks.jsonl")
    labels = LabelStore(workdir / "telemetry" / "labels.jsonl")
    records = logger.read()
    exporter = CorpusExporter(labels)

    # Found live: this only ever counted intent == "plan"/"ask", silently
    # leaving "answer" and "refuse" out of a summary that reads as complete
    # -- on one real machine's traffic, "answered" was 80% of all recorded
    # turns and nowhere in this breakdown. A Counter over whatever intents
    # actually occurred can't go stale the way a hardcoded pair of lines did.
    print(f"tasks recorded : {len(records)}")
    intent_labels = {"plan": "planned", "ask": "asked",
                     "answer": "answered", "refuse": "refused"}
    intents = Counter(r.intent for r in records if r.intent)
    for intent, count in intents.most_common():
        print(f"  {intent_labels.get(intent, intent or 'unknown'):<12} : {count}")
    print(f"  completed    : {sum(1 for r in records if r.status == 'completed')}")
    print(f"  repairs      : {sum(r.repairs for r in records)}")
    print()
    print(health_summary(records))
    print()
    clarifier = Clarifier()
    print(exporter.drift(records, args.baseline_fn, args.baseline_fp).render())
    print()
    loophole = LoopholeAudit(clarifier).audit(records)
    print(f"anchored-but-goalless: {loophole.anchored_but_goalless} of "
         f"{loophole.passed_clarifier} passed request(s)")
    print(f"  verdict: {loophole.verdict}")
    if loophole.examples:
        print("  examples:")
        for request in loophole.examples[:5]:
            print(f"    {request!r}")
    pending = labels.unlabeled(records)
    if pending:
        print(f"\nunlabeled ({len(pending)}), most frequent first:")
        for request in pending[:10]:
            print(f"  {request!r}")
    print()
    reporter = CrashReporter(workdir / "telemetry" / "crashes.jsonl")
    print(crash_summary(reporter))
    return EXIT_OK


def cmd_label(args: argparse.Namespace) -> int:
    """Label recorded traffic, most frequent phrasings first."""
    workdir = Path(args.workdir)
    logger = TaskLogger(workdir / "telemetry" / "tasks.jsonl")
    labels = LabelStore(workdir / "telemetry" / "labels.jsonl")
    clarifier = Clarifier()
    pending = labels.unlabeled(logger.read())
    if not pending:
        print("nothing to label. Ask for something first.")
        return EXIT_OK

    print(f"{len(pending)} unlabeled request(s).")
    print("a = ambiguous, s = specific, k = skip, q = quit\n")
    labeled = 0
    for request in pending[: args.limit]:
        verdict = "would ask" if clarifier.detect(request) else "would plan"
        print(f"  {request!r}\n    clarifier: {verdict}")
        try:
            answer = input("    [a/s/k/q] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            break
        if answer == "q":
            break
        if answer == "a":
            try:
                why = input("    why? ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            labels.label(request, True, why, os.environ.get("USER", ""))
            labeled += 1
        elif answer == "s":
            labels.label(request, False, labeler=os.environ.get("USER", ""))
            labeled += 1
        print()
    total = len(labels.all())
    print(f"labeled {labeled} this session, {total} total.")
    if total < 30:
        print(f"{30 - total} more before drift() will report a rate.")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="torra",
        description="A local model proposes, five gates decide, you approve.",
        epilog="exit codes: 0 ok, 1 usage, 2 stopped by a gate, 3 failed")
    parser.add_argument("--workdir", default=str(DEFAULT_WORKDIR))
    sub = parser.add_subparsers(dest="command")

    tui = sub.add_parser("tui", help="launch the terminal interface (default)")
    tui.set_defaults(func=cmd_tui)

    ask = sub.add_parser("ask", help="one request, planned and approved at the prompt")
    ask.add_argument("request")
    ask.add_argument("--model")
    ask.add_argument("--dry-run", action="store_true")
    ask.add_argument("--non-interactive", action="store_true")
    ask.add_argument("--plan-only", action="store_true")
    ask.add_argument("--verify",
                     help="after the request completes, repeat this check "
                          "(e.g. 'run pytest tests/') and, if it fails, ask "
                          "the model to fix it and re-check -- up to "
                          "--max-iterations times")
    ask.add_argument("--max-iterations", type=int, default=3,
                     help="fix attempts allowed under --verify (default: 3)")
    ask.set_defaults(func=cmd_ask)

    plan = sub.add_parser("plan", help="plan only, run nothing")
    plan.add_argument("request")
    plan.add_argument("--model")
    plan.set_defaults(func=cmd_plan)

    run = sub.add_parser("run", help="run a hand-written task file, no model")
    run.add_argument("task_file")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--non-interactive", action="store_true")
    run.set_defaults(func=cmd_run)

    check = sub.add_parser("check", help="classify a task file without running it")
    check.add_argument("task_file")
    check.add_argument("-v", "--verbose", action="store_true")
    check.set_defaults(func=cmd_check)

    doctor = sub.add_parser("doctor", help="check the environment")
    doctor.set_defaults(func=cmd_doctor)

    evidence = sub.add_parser("evidence", help="what torra has recorded")
    evidence.add_argument("--baseline-fn", type=float, default=0.0)
    evidence.add_argument("--baseline-fp", type=float, default=0.10)
    evidence.set_defaults(func=cmd_evidence)

    label = sub.add_parser("label", help="label recorded traffic")
    label.add_argument("--limit", type=int, default=25)
    label.set_defaults(func=cmd_label)

    return parser


def _load_env_file(path: os.PathLike[str] | str | None = None) -> None:
    """Load `OPENROUTER_API_KEY` (and anything else) from a `.env` file at
    the repo root, if one exists. `path` is only ever overridden by tests.

    An explicit path, not `load_dotenv()`'s own default upward-search from
    the current directory: `torra` is meant to be run from wherever the
    operator happens to be (README's own `alias torra=...` example), not
    from inside this repo, so the default search could just as easily pick
    up an unrelated `.env` from whatever *other* project's directory the
    operator happens to be sitting in. Never overrides a variable already
    set in the real environment (`load_dotenv`'s own default, not passed
    `override=True` here) -- an explicit `export` always wins over the
    file. Silent no-op when the file is absent, the same way `OllamaClient`
    degrades when the daemon isn't there: this must never become a
    required file for every other command that has nothing to do with
    OpenRouter.
    """
    load_dotenv(path or Path(__file__).resolve().parent.parent / ".env")


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Bare `torra` launches the interface."""
    _load_env_file()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args.func = cmd_tui
        args.command = "tui"
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return EXIT_USAGE
    except TaskSpecError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except Exception as exc:  # no silent failures, even at the top level
        crash_path = Path(args.workdir) / "telemetry" / "crashes.jsonl"
        CrashReporter(crash_path).record(exc, site=f"cli:{args.command or 'unknown'}")
        print(f"unhandled {type(exc).__name__}: {exc}\nlogged to {crash_path}",
              file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
