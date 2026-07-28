#!/usr/bin/env python3
"""
harness_cli.py
Phase 6. The command line entry point.

Five phases built gates. None of them made the harness usable, and the Phase 5
recommendation (run it on real work for a month) was never going to happen while
one task meant writing a Python script.

    harness_cli.py run task.yaml            run a task file
    harness_cli.py run task.yaml --dry-run  every gate, no execution
    harness_cli.py check task.yaml          classify without running anything
    harness_cli.py label                    interactive labeling of real traffic
    harness_cli.py evidence                 the report
    harness_cli.py drift                    clarifier drift against the baseline
    harness_cli.py loophole                 is the known gap costing anything
    harness_cli.py rotate                   rotate the task log now
    harness_cli.py systemd                  generate the weekly report units
    harness_cli.py doctor                   check the environment

Exit codes are meaningful, because this ends up in a shell script eventually:

    0  the task completed, or the command succeeded
    1  a usage or configuration error
    2  the task was stopped by a gate (clarification, precondition, safety)
    3  the task executed and failed
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness_core import OllamaFallbackHandler  # noqa: E402
from core_behaviors import Precondition  # noqa: E402
from core_behaviors_v2 import ContinuousVerifyState, ParsingSafetyClassifier  # noqa: E402
from core_behaviors_v4 import StructuralClarifier  # noqa: E402
from harness_ops import RotatingTaskLogger, SystemdUnitBuilder, TaskSpec, TaskSpecError  # noqa: E402
from harness_telemetry import LabelStore  # noqa: E402
from phase1_integrated_executor import PipelineStatus  # noqa: E402
from phase5_integrated_executor import Phase5IntegratedExecutor  # noqa: E402

__all__ = ["build_parser", "main"]

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_BLOCKED = 2
EXIT_FAILED = 3

DEFAULT_WORKDIR = Path.home() / ".harness"

#: Pipeline status to exit code. A gate stopping a task is not the same thing as
#: a task failing, and a shell script should be able to tell them apart.
STATUS_EXIT: Dict[str, int] = {
    "completed": EXIT_OK,
    "needs_clarification": EXIT_BLOCKED,
    "precondition_failed": EXIT_BLOCKED,
    "safety_cancelled": EXIT_BLOCKED,
    "backend_unavailable": EXIT_BLOCKED,
    "execution_failed": EXIT_FAILED,
}


# --------------------------------------------------------------------------- #
# Construction
# --------------------------------------------------------------------------- #


def build_pipeline(
    workdir: Path,
    watch_roots: Sequence[str] = (),
    interactive: bool = True,
    assume_yes: bool = False,
    offline: bool = False,
) -> Phase5IntegratedExecutor:
    """Assemble the full stack with terminal prompts wired in.

    Args:
        workdir: Harness working directory.
        watch_roots: Directories watched in full by the conflict detector.
        interactive: Prompt on stdin for clarification and dangerous commands.
            When False, both default to refusing, which is the safe direction
            for a cron job.
        assume_yes: Auto-approve dangerous commands. Requires interactive=False
            to be meaningful and should be used with a specific task file only.
        offline: Stub the Ollama probe as healthy, for testing without a daemon.
    """
    def ask(ambiguity: Any) -> Optional[str]:
        """Put one clarifying question to the operator on stdin."""
        if not interactive:
            return None
        try:
            print(f"\n  ? {ambiguity.question}")
            print(f"    (why: {ambiguity.why})")
            answer = input("    > ").strip()
            return answer or None
        except (EOFError, KeyboardInterrupt):
            return None

    def confirm(verdict: Any) -> bool:
        """Show a dangerous command and ask whether to proceed."""
        if assume_yes:
            print(f"  [{verdict.level.value.upper()}] auto-approved: {verdict.command}")
            return True
        if not interactive:
            print(f"  [{verdict.level.value.upper()}] refused (non-interactive): "
                  f"{verdict.command}")
            return False
        try:
            print(f"\n  [{verdict.level.value.upper()}] {verdict.command}")
            for reason in verdict.reasons[:4]:
                print(f"    {reason}")
            return input("    proceed? [y/N] ").strip().lower() in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return False

    verifier = ContinuousVerifyState()
    verifier.add(Precondition("disk:/", lambda: ContinuousVerifyState.disk_free_gb("/") > 1.0,
                              "at least 1 GB free on /"), ttl_seconds=30)

    return Phase5IntegratedExecutor(
        workdir=workdir,
        watch_roots=list(watch_roots),
        clarifier=StructuralClarifier(ask=ask),
        safety=ParsingSafetyClassifier(confirm=confirm),
        verifier=verifier,
        logger=RotatingTaskLogger(workdir / "telemetry" / "tasks.jsonl"),
        labels=LabelStore(workdir / "telemetry" / "labels.jsonl"),
        ollama=(OllamaFallbackHandler(probe=lambda: True, timeout=0.1)
                if offline else OllamaFallbackHandler()),
    )


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def cmd_run(args: argparse.Namespace) -> int:
    """Run a task file through all 8 stages."""
    try:
        spec = TaskSpec.load(args.task_file)
    except TaskSpecError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    pipe = build_pipeline(
        Path(args.workdir), spec.watch_roots,
        interactive=not args.non_interactive,
        assume_yes=args.yes, offline=args.offline,
    )

    print(f"task   : {spec.task_id}")
    print(f"request: {spec.request}")
    print(f"steps  : {len(spec.steps)}" + ("  [DRY RUN]" if args.dry_run else ""))

    result = pipe.run(
        task_id=spec.task_id,
        task=spec.request,
        steps=spec.to_steps(dry_run=args.dry_run),
        commands=spec.commands(),
        rollback_actions=spec.rollback_actions(),
    )

    print(f"\nstatus : {result.status.value}")
    print(f"stages : {' -> '.join(result.stages_completed)}")
    if result.model_choice:
        print(f"model  : {result.model_choice.model} "
              f"(rule {result.model_choice.matched_rule})")
    if result.clarification and result.clarification.ambiguities:
        print("questions:")
        for amb in result.clarification.ambiguities:
            print(f"  {amb.code}: {amb.question}")
    if result.execution and result.execution.abort_reason:
        print(f"abort  : {result.execution.abort_reason}")
    if result.rollback:
        print(f"rollback: {len(result.rollback.undone)} undone, "
              f"{len(result.rollback.cancelled)} cancelled, "
              f"{len(result.rollback.failed)} failed, "
              f"verified={result.rollback_verified}")
    if result.undo_coverage.get("uncovered"):
        print(f"no auto-undo for: {result.undo_coverage['uncovered']} "
              "(declare a rollback: block for these)")
    print(f"audit  : {result.audit_path}")

    return STATUS_EXIT.get(result.status.value, EXIT_FAILED)


def cmd_check(args: argparse.Namespace) -> int:
    """Classify a task file without running or prompting for anything."""
    try:
        spec = TaskSpec.load(args.task_file)
    except TaskSpecError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    clarifier = StructuralClarifier()
    safety = ParsingSafetyClassifier()

    print(f"task   : {spec.task_id}\nrequest: {spec.request}\n")
    print(clarifier.explain(spec.request))

    print("\ncommands:")
    blocked = 0
    for command in spec.commands():
        verdict = safety.classify(command)
        flag = "BLOCK" if verdict.requires_confirmation else "  ok "
        blocked += 1 if verdict.requires_confirmation else 0
        print(f"  {flag} {verdict.level.value:9} {command}")
        if args.verbose:
            for reason in verdict.reasons[:3]:
                print(f"             {reason}")

    ambiguities = clarifier.detect(spec.request)
    print(f"\n{len(ambiguities)} question(s), {blocked} command(s) need confirmation")
    return EXIT_BLOCKED if (ambiguities or blocked) else EXIT_OK


def cmd_label(args: argparse.Namespace) -> int:
    """Label recorded traffic, most frequent phrasings first."""
    pipe = build_pipeline(Path(args.workdir), interactive=False, offline=True)
    pending = pipe.unlabeled()
    if not pending:
        print("nothing to label. Run some tasks first.")
        return EXIT_OK

    print(f"{len(pending)} unlabeled request(s), most frequent first.")
    print("a = ambiguous (needs a question), s = specific, k = skip, q = quit\n")

    labeled = 0
    for request in pending[: args.limit]:
        clarifier_says = "would ask" if pipe.clarifier.detect(request) else "would run"
        print(f"  {request!r}\n    clarifier: {clarifier_says}")
        try:
            answer = input("    [a/s/k/q] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if answer == "q":
            break
        if answer == "a":
            why = input("    why is it ambiguous? ").strip()
            pipe.labels.label(request, True, why, labeler=os.environ.get("USER", ""))
            labeled += 1
        elif answer == "s":
            pipe.labels.label(request, False, labeler=os.environ.get("USER", ""))
            labeled += 1
        print()

    total = len(pipe.labels.all())
    print(f"labeled {labeled} this session, {total} total.")
    if total < 30:
        print(f"{30 - total} more needed before drift() will report a rate.")
    return EXIT_OK


def cmd_evidence(args: argparse.Namespace) -> int:
    """Print the full evidence report."""
    pipe = build_pipeline(Path(args.workdir), interactive=False, offline=True)
    print(pipe.evidence_report())
    if args.json:
        payload = {
            "records": len(pipe.records()),
            "drift": pipe.drift(args.baseline_fn, args.baseline_fp).to_dict(),
            "loophole": pipe.loophole().to_dict(),
            "logger": pipe.logger.stats(),
        }
        print("\n" + json.dumps(payload, indent=2, default=str))
    return EXIT_OK


def cmd_drift(args: argparse.Namespace) -> int:
    """Score the live clarifier against labeled traffic."""
    pipe = build_pipeline(Path(args.workdir), interactive=False, offline=True)
    print(pipe.drift(args.baseline_fn, args.baseline_fp).render())
    return EXIT_OK


def cmd_loophole(args: argparse.Namespace) -> int:
    """Quantify the anchored-but-goalless gap in recorded traffic."""
    pipe = build_pipeline(Path(args.workdir), interactive=False, offline=True)
    report = pipe.loophole()
    print(f"tasks recorded    : {report.total}")
    print(f"passed clarifier  : {report.passed_clarifier}")
    print(f"anchored, goalless: {report.anchored_but_goalless}")
    if report.goalless_failure_rate is not None:
        print(f"goalless failure  : {report.goalless_failure_rate:.0%}")
    if report.anchored_failure_rate is not None:
        print(f"with-outcome fail : {report.anchored_failure_rate:.0%}")
    print(f"verdict           : {report.verdict}")
    for example in report.examples[:5]:
        print(f"  example: {example!r}")
    return EXIT_OK


def cmd_rotate(args: argparse.Namespace) -> int:
    """Rotate the task log now."""
    pipe = build_pipeline(Path(args.workdir), interactive=False, offline=True)
    logger = pipe.logger
    before = logger.stats()
    report = logger.rotate(force=args.force) if hasattr(logger, "rotate") else None
    if report is None:
        print("this logger does not rotate")
        return EXIT_USAGE
    if not report.rotated:
        print(f"no rotation needed: {before['active_bytes']} bytes, "
              f"threshold {before['max_bytes']}")
        return EXIT_OK
    print(f"rotated {report.bytes_moved} bytes to {report.to_path}")
    if report.archives_removed:
        print(f"removed {len(report.archives_removed)} archive(s) beyond keep")
    for error in report.errors:
        print(f"error: {error}", file=sys.stderr)
    return EXIT_OK if not report.errors else EXIT_FAILED


def cmd_systemd(args: argparse.Namespace) -> int:
    """Generate the weekly evidence report units."""
    builder = SystemdUnitBuilder(
        venv_python=args.python or sys.executable,
        harness_dir=Path(__file__).resolve().parent,
        workdir=Path(args.workdir),
        on_calendar=args.on_calendar,
    )
    if args.write:
        service, timer = builder.write(Path(args.write))
        print(f"wrote {service}\nwrote {timer}\n")
    else:
        print(f"--- {args.name}.service ---\n{builder.service()}")
        print(f"--- {args.name}.timer ---\n{builder.timer()}")
    print(builder.install_hint(args.name))
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check that the environment can actually run this."""
    workdir = Path(args.workdir)
    checks: List[tuple[str, bool, str]] = []

    checks.append(("python >= 3.10", sys.version_info >= (3, 10),
                   ".".join(map(str, sys.version_info[:3]))))
    try:
        import yaml  # noqa: F401
        checks.append(("pyyaml", True, "importable"))
    except ImportError:
        checks.append(("pyyaml", False, "missing: pip install pyyaml"))
    try:
        import inotify_simple  # noqa: F401
        checks.append(("inotify_simple", True, "fast tree watching available"))
    except ImportError:
        checks.append(("inotify_simple", False,
                       "missing: falls back to full walks, still correct"))

    checks.append(("workdir writable", os.access(workdir.parent, os.W_OK)
                   if workdir.parent.exists() else True, str(workdir)))
    checks.append(("systemctl", shutil.which("systemctl") is not None,
                   shutil.which("systemctl") or "not on PATH"))

    try:
        probe = subprocess.run(["cp", "--reflink=always", "--help"],
                               capture_output=True, text=True, timeout=5)
        checks.append(("cp --reflink", probe.returncode == 0,
                       "shadow snapshots may clone instead of copy"))
    except Exception as exc:
        checks.append(("cp --reflink", False, f"{type(exc).__name__}"))

    ollama = OllamaFallbackHandler()
    status = ollama.health_check()
    checks.append((f"ollama at {ollama.host}", status.value == "healthy",
                   status.value + (f" ({ollama.last_error})"
                                   if ollama.last_error else "")))

    width = max(len(name) for name, _ok, _detail in checks)
    failed = 0
    for name, ok, detail in checks:
        failed += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'WARN'}  {name:<{width}}  {detail}")

    print(f"\n{len(checks) - failed}/{len(checks)} checks passed")
    if failed:
        print("warnings above are not fatal: every one of them has a documented "
              "fallback. Missing pyyaml is the only one that blocks `run`.")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for every subcommand."""
    parser = argparse.ArgumentParser(
        prog="harness",
        description="Terminal agent harness for Pop-OS automation.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("exit codes: 0 ok, 1 usage error, 2 stopped by a gate, "
                "3 executed and failed"),
    )
    parser.add_argument("--workdir", default=str(DEFAULT_WORKDIR),
                        help=f"harness working directory (default {DEFAULT_WORKDIR})")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run a task file")
    run.add_argument("task_file")
    run.add_argument("--dry-run", action="store_true",
                     help="every gate runs, nothing executes")
    run.add_argument("--non-interactive", action="store_true",
                     help="never prompt; clarification and dangerous commands are refused")
    run.add_argument("--yes", action="store_true",
                     help="auto-approve dangerous commands (use with one known task file)")
    run.add_argument("--offline", action="store_true",
                     help="skip the real Ollama probe")
    run.set_defaults(func=cmd_run)

    check = sub.add_parser("check", help="classify a task file without running it")
    check.add_argument("task_file")
    check.add_argument("-v", "--verbose", action="store_true")
    check.set_defaults(func=cmd_check)

    label = sub.add_parser("label", help="label recorded traffic")
    label.add_argument("--limit", type=int, default=25)
    label.set_defaults(func=cmd_label)

    evidence = sub.add_parser("evidence", help="print the evidence report")
    evidence.add_argument("--json", action="store_true")
    evidence.add_argument("--baseline-fn", type=float, default=0.0)
    evidence.add_argument("--baseline-fp", type=float, default=0.10)
    evidence.set_defaults(func=cmd_evidence)

    drift = sub.add_parser("drift", help="clarifier drift against the baseline")
    drift.add_argument("--baseline-fn", type=float, default=0.0)
    drift.add_argument("--baseline-fp", type=float, default=0.10)
    drift.set_defaults(func=cmd_drift)

    loophole = sub.add_parser("loophole", help="is the known clarifier gap costing anything")
    loophole.set_defaults(func=cmd_loophole)

    rotate = sub.add_parser("rotate", help="rotate the task log")
    rotate.add_argument("--force", action="store_true")
    rotate.set_defaults(func=cmd_rotate)

    systemd = sub.add_parser("systemd", help="generate the weekly report units")
    systemd.add_argument("--write", help="directory to write the units into")
    systemd.add_argument("--name", default="harness-evidence")
    systemd.add_argument("--on-calendar", default="Mon 09:00")
    systemd.add_argument("--python", help="absolute venv interpreter path")
    systemd.set_defaults(func=cmd_systemd)

    doctor = sub.add_parser("doctor", help="check the environment")
    doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point. Returns an exit code rather than calling sys.exit."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return EXIT_USAGE
    except TaskSpecError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except Exception as exc:  # no silent failures, even at the top level
        print(f"unhandled {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
