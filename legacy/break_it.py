"""
break_it.py
Adversarial probes. This file is not a pass/fail suite. It hunts for the places
where Phase 1 does the wrong thing under conditions the 18 acceptance cases do
not cover, and prints a verdict per probe.

Run:  python3 break_it.py
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

from harness_core import (
    AdaptiveCheckpointing,
    CheckpointError,
    ConflictDetector,
    ConflictKind,
    ContextBudgetError,
    ContextCompressor,
    CoreExecutor,
    OllamaFallbackHandler,
    Step,
)
from core_behaviors import (
    AskClarifyingQuestionsBehavior,
    ModelSelector,
    Precondition,
    RiskLevel,
    RollbackOnFailBehavior,
    SafetyFirstBehavior,
    VerifyStateBehavior,
)
from phase1_integrated_executor import Phase1IntegratedExecutor, PipelineStatus

FINDINGS: List[Tuple[str, str, str]] = []  # (severity, probe, note)


def finding(severity: str, probe: str, note: str) -> None:
    FINDINGS.append((severity, probe, note))
    print(f"  [{severity}] {note}")


def healthy() -> OllamaFallbackHandler:
    return OllamaFallbackHandler(probe=lambda: True, timeout=0.1)


# --------------------------------------------------------------------------- #


def probe_crash_and_resume() -> None:
    """Kill a run mid-flight, then resume. Does it repeat side effects?"""
    print("\nPROBE 1: crash mid-run then resume")
    side_effects: List[str] = []

    def make(n: int, explode_at: int | None) -> List[Step]:
        def body(i: int) -> str:
            if explode_at is not None and i == explode_at:
                raise RuntimeError("simulated crash")
            side_effects.append(f"step{i}")
            return f"out{i}"
        return [
            Step(f"s{i}", command=f"echo {i}", description=f"step {i}",
                 estimated_seconds=180.0, run=lambda i=i: body(i))
            for i in range(1, n + 1)
        ]

    with tempfile.TemporaryDirectory() as tmp:
        cp = AdaptiveCheckpointing(Path(tmp))
        ex = CoreExecutor(cp, ollama=healthy())
        r1 = ex.execute("resume-test", make(10, explode_at=6))
        assert not r1.ok
        first_pass = list(side_effects)

        mid_run_state = cp.load_state("resume-test")  # read BEFORE the resume run

        cp2 = AdaptiveCheckpointing(Path(tmp))
        ex2 = CoreExecutor(cp2, ollama=healthy())
        r2 = ex2.execute("resume-test", make(10, explode_at=None), resume=True)

        repeated = [s for s in first_pass if side_effects.count(s) > 1]
        if repeated:
            finding("HIGH", "crash+resume",
                    f"side effects repeated on resume: {sorted(set(repeated))}")
        else:
            finding("OK", "crash+resume", "no side effect replayed on resume")

        if r2.ok and len(r2.results) == 5:
            finding("OK", "crash+resume",
                    f"resumed at the right step, ran {len(r2.results)} of 10")
        else:
            finding("HIGH", "crash+resume",
                    f"resume ran {len(r2.results)} steps, ok={r2.ok}")

        # The failing step itself: was it marked complete before it threw?
        saved = mid_run_state
        if saved and "s6" in saved.completed_step_ids:
            finding("HIGH", "crash+resume",
                    "the step that threw was recorded as completed")
        else:
            finding("OK", "crash+resume", "failed step not marked complete")


def probe_conflict_midrun() -> None:
    """External edit between steps. Does the executor stop before clobbering?"""
    print("\nPROBE 2: external file edit mid-run")
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "nginx.conf"
        target.write_text("original\n", encoding="utf-8")

        def sabotage() -> str:
            target.write_text("EDITED BY A HUMAN\n", encoding="utf-8")
            return "sabotaged"

        steps = [
            Step("s1", command="echo a", description="a", estimated_seconds=180.0,
                 touches=[str(target)], run=lambda: "a"),
            Step("s2", command="echo b", description="b", estimated_seconds=180.0,
                 run=sabotage),  # note: does NOT declare it touches the file
            Step("s3", command="echo c", description="c", estimated_seconds=180.0,
                 touches=[str(target)], run=lambda: "c"),
        ]
        cp = AdaptiveCheckpointing(Path(tmp) / "cp")
        ex = CoreExecutor(cp, ollama=healthy())
        report = ex.execute("conflict-test", steps)

        if report.ok:
            finding("HIGH", "conflict-midrun",
                    "external edit was NOT caught, run completed anyway")
        else:
            finding("OK", "conflict-midrun",
                    f"aborted at {report.aborted_at}: {report.abort_reason[:70]}")

        # Untracked files are invisible. That is the real gap.
        untracked = Path(tmp) / "not_declared.conf"
        untracked.write_text("x", encoding="utf-8")
        det = ConflictDetector()
        det.baseline_files([str(target)])
        untracked.write_text("y", encoding="utf-8")
        if not det.detect():
            finding("MED", "conflict-midrun",
                    "files a step forgets to declare in touches are never checked")


def probe_safety_evasion() -> None:
    """Can a destructive command slip past the classifier?"""
    print("\nPROBE 3: safety classifier evasion")
    safety = SafetyFirstBehavior()
    evasions = [
        ("rm -fr /var/lib/x", "flag order swapped"),
        ("rm  -r  -f  /var/lib/x", "flags split"),
        ("/bin/rm -rf /var/lib/x", "absolute path"),
        ("R=rm; $R -rf /var/lib/x", "variable indirection"),
        ("echo 'cm0gLXJmIC8=' | base64 -d | sh", "base64 pipe to shell"),
        ("find /var -delete", "find with -delete"),
        ("sudo -- sh -c 'rm -rf /var/lib/x'", "wrapped in sh -c"),
        ("python3 -c \"import shutil;shutil.rmtree('/var/lib/x')\"", "python rmtree"),
        ("git checkout -- .", "silent discard of local edits"),
        ("> /etc/fstab", "truncate by redirect"),
        ("systemctl  stop  ollama", "double spaces"),
        ("SYSTEMCTL=1 systemctl stop ollama", "env prefix"),
    ]
    missed: List[str] = []
    for command, label in evasions:
        v = safety.classify(command)
        if v.level in (RiskLevel.SAFE, RiskLevel.CAUTION):
            missed.append(f"{label}: {command!r} -> {v.level.value}")
    for m in missed:
        finding("HIGH", "safety-evasion", f"NOT flagged as dangerous: {m}")
    if not missed:
        finding("OK", "safety-evasion", "all evasion attempts flagged")
    print(f"  caught {len(evasions) - len(missed)}/{len(evasions)}")


def probe_context_pathologies() -> None:
    """Compressor behavior at the edges."""
    print("\nPROBE 4: context compressor edge cases")
    comp = ContextCompressor(max_tokens=1000)
    try:
        comp.add_step("huge", "assistant", "z" * 100_000)
        finding("HIGH", "context", "an oversized step was accepted silently")
    except ContextBudgetError:
        finding("OK", "context", "oversized single step raises loudly")

    # Many tiny steps: does compression actually free anything?
    small = ContextCompressor(max_tokens=2000, trigger_ratio=0.9)
    i = 0
    while not small.at_capacity() and i < 5000:
        i += 1
        small.add_step(f"t{i}", "assistant", "ok")
    res = small.compress()
    if res.triggered and res.reduction_ratio < 0.2:
        finding("MED", "context",
                f"many-tiny-steps case only freed {res.reduction_ratio:.1%}; "
                "summary line overhead dominates")
    else:
        finding("OK", "context",
                f"tiny-step case freed {res.reduction_ratio:.1%}")

    # Repeated compression: does it converge or thrash?
    fat = ContextCompressor(max_tokens=8000)
    body = "line\n" + ("word " * 400)
    passes = 0
    for k in range(400):
        fat.add_step(f"k{k}", "assistant", body)
        if fat.maybe_compress().triggered:
            passes += 1
    finding("OK" if passes < 60 else "MED", "context",
            f"{passes} compression passes over 400 steps, "
            f"final usage {fat.usage_ratio:.0%}")

    # Does the summary of summaries keep meaning anything?
    nested = sum(1 for s in fat.steps if s.compressed)
    if nested > 1:
        finding("MED", "context",
                f"{nested} stacked summary blocks; history becomes lossy fast")


def probe_checkpoint_corruption() -> None:
    """A truncated or garbage checkpoint must not silently resume from nothing."""
    print("\nPROBE 5: checkpoint corruption")
    with tempfile.TemporaryDirectory() as tmp:
        cp = AdaptiveCheckpointing(Path(tmp))
        steps = [Step(f"s{i}", estimated_seconds=180.0, run=lambda: "ok")
                 for i in range(1, 4)]
        CoreExecutor(cp, ollama=healthy()).execute("corrupt", steps)
        path = cp.path_for("corrupt")

        path.write_text('{"task_id": "corrupt", "step_ind', encoding="utf-8")
        try:
            cp.load_state("corrupt")
            finding("HIGH", "checkpoint", "truncated checkpoint loaded without error")
        except CheckpointError:
            finding("OK", "checkpoint", "truncated checkpoint raises CheckpointError")

        path.write_text('{"task_id": "corrupt", "bogus_field": 1}', encoding="utf-8")
        try:
            cp.load_state("corrupt")
            finding("HIGH", "checkpoint", "checkpoint with unknown schema loaded")
        except CheckpointError:
            finding("OK", "checkpoint", "schema mismatch raises CheckpointError")

        # Read-only checkpoint dir: does the run die or degrade?
        ro = Path(tmp) / "ro"
        ro.mkdir()
        cp2 = AdaptiveCheckpointing(ro)
        os.chmod(ro, 0o500)
        try:
            rep = CoreExecutor(cp2, ollama=healthy()).execute("ro-task", steps)
            if rep.ok and cp2.errors:
                finding("HIGH", "checkpoint",
                        "run reported ok while every checkpoint write failed: "
                        f"{cp2.errors[0][:70]}")
            elif cp2.errors:
                finding("MED", "checkpoint",
                        "checkpoint writes failed and the run continued unresumable")
            else:
                finding("OK", "checkpoint", "read-only dir handled")
        finally:
            os.chmod(ro, 0o700)


def probe_concurrent_task_ids() -> None:
    """Two executors on one task id. Last writer wins, silently."""
    print("\nPROBE 6: task id collision")
    with tempfile.TemporaryDirectory() as tmp:
        cp_a = AdaptiveCheckpointing(Path(tmp))
        cp_b = AdaptiveCheckpointing(Path(tmp))
        steps_a = [Step(f"a{i}", estimated_seconds=180.0, run=lambda: "a")
                   for i in range(1, 4)]
        steps_b = [Step(f"b{i}", estimated_seconds=180.0, run=lambda: "b")
                   for i in range(1, 4)]
        CoreExecutor(cp_a, ollama=healthy()).execute("same-id", steps_a)
        rep_b = CoreExecutor(cp_b, ollama=healthy()).execute("same-id", steps_b)
        saved = cp_a.load_state("same-id")
        overwritten = bool(saved and saved.completed_step_ids[0].startswith("b"))
        warned = any("already has a checkpoint" in w for w in rep_b.warnings)
        if overwritten and not warned:
            finding("HIGH", "task-id-collision",
                    "second run silently overwrote the first run's checkpoint, "
                    "no lock and no warning")
        elif overwritten and warned:
            finding("MED", "task-id-collision",
                    "collision is warned about but not prevented; there is still "
                    "no lock file, so two concurrent runs race on one checkpoint")
        else:
            finding("OK", "task-id-collision", "collision prevented")


def probe_pipeline_rollback() -> None:
    """Does a failing step actually unwind the stack through the pipeline?"""
    print("\nPROBE 7: pipeline rollback on execution failure")
    undone: List[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        steps = [
            Step("migrate", command="alembic upgrade head", description="migrate",
                 estimated_seconds=40.0, run=lambda: "ok"),
            Step("restart", command="systemctl restart api", description="restart",
                 estimated_seconds=20.0,
                 run=lambda: (_ for _ in ()).throw(RuntimeError("unit failed"))),
        ]
        pipe = Phase1IntegratedExecutor(
            workdir=Path(tmp),
            clarifier=AskClarifyingQuestionsBehavior(ask=lambda a: "staging"),
            safety=SafetyFirstBehavior(confirm=lambda v: True),
            ollama=healthy(),
        )
        result = pipe.run(
            "rb-1", "deploy the api stack to staging", steps,
            rollback_actions=[("downgrade the migration",
                               lambda: undone.append("migration"), True)],
        )
        if result.status is not PipelineStatus.EXECUTION_FAILED:
            finding("HIGH", "pipeline-rollback",
                    f"expected EXECUTION_FAILED, got {result.status.value}")
        elif undone == ["migration"] and result.rollback and result.rollback.ok:
            finding("OK", "pipeline-rollback", "stack unwound cleanly on failure")
        else:
            finding("HIGH", "pipeline-rollback",
                    f"rollback did not run: undone={undone}")

        # The forward step that succeeded is not itself undone by the executor,
        # only by whatever the caller pushed. Worth stating out loud.
        finding("MED", "pipeline-rollback",
                "undo actions are caller supplied; a step that changes state "
                "without pushing an undo is unrecoverable by design")


def probe_estimate_accuracy() -> None:
    """How wrong is the duration estimate, and what does that cost?"""
    print("\nPROBE 8: duration estimate accuracy")
    with tempfile.TemporaryDirectory() as tmp:
        cp = AdaptiveCheckpointing(Path(tmp))
        # A task the estimator thinks is short but which is actually long.
        steps = [Step(f"s{i}", kind="shell", description="d", run=lambda: "x")
                 for i in range(1, 21)]  # 20 shell steps, 10s each -> 3.3 min
        policy = cp.decide_frequency(steps)
        if policy.every_n_steps == 0:
            finding("HIGH", "estimate",
                    f"20 unknown-duration shell steps classed {policy.length.value} "
                    f"({policy.estimated_minutes} min) so a 2 hour run in reality "
                    "would checkpoint zero times")

        # No re-evaluation once running.
        finding("HIGH", "estimate",
                "the policy is fixed at t=0 and never revised, so an estimate "
                "that is wrong is wrong for the whole run")


def probe_clarifier_false_positives() -> None:
    """Does the ambiguity detector block perfectly clear requests?"""
    print("\nPROBE 9: clarifier false positives")
    clar = AskClarifyingQuestionsBehavior()
    clear_requests = [
        "restart ollama.service and confirm it reports active",
        "tail the last 200 lines of /var/log/syslog",
        "run pytest tests/test_phase1.py and report failures",
        "check free disk on /home and report the number",
        "remove /tmp/build-cache-2024 and nothing else",
        "deploy commit a1b2c3d to staging",
    ]
    blocked = []
    for req in clear_requests:
        r = clar.clarify(req)
        if r.needs_clarification:
            blocked.append((req, [a.code for a in r.ambiguities]))
    for req, codes in blocked:
        finding("MED", "clarifier-fp",
                f"clear request blocked by {codes}: {req!r}")
    if not blocked:
        finding("OK", "clarifier-fp", "no false positives on clear requests")


def probe_model_routing_collisions() -> None:
    """Tasks that match two rules. Does first-match give the right answer?"""
    print("\nPROBE 10: model routing collisions")
    sel = ModelSelector()
    collisions = [
        ("debug the failing deploy to production", "qwen3:32b"),
        ("deploy the fix for the crash", "qwen3:32b"),
        ("review the deploy script before we ship it", "qwen3:32b"),
        ("check why the release is broken", "qwen3:32b"),
    ]
    wrong = []
    for task, expected in collisions:
        got = sel.select(task)
        if got.model != expected:
            wrong.append(f"{task!r} -> {got.model} via {got.matched_rule} "
                         f"(expected {expected})")
    for w in wrong:
        finding("MED", "routing-collision", w)
    if not wrong:
        finding("OK", "routing-collision", "collisions resolved sensibly")


def probe_checkpoint_growth() -> None:
    """Checkpoint size across a long run. Linear or quadratic?"""
    print("\nPROBE 11: checkpoint size growth")
    sizes: List[int] = []
    with tempfile.TemporaryDirectory() as tmp:
        for n in (25, 50, 100, 200):
            cp = AdaptiveCheckpointing(Path(tmp) / f"n{n}")
            steps = [Step(f"s{i}", description="d" * 200, estimated_seconds=180.0,
                          run=lambda: "y" * 2048) for i in range(1, n + 1)]
            CoreExecutor(cp, ollama=healthy()).execute(f"grow-{n}", steps)
            sizes.append(cp.stats()["bytes_written"])
            print(f"  {n:>4} steps -> {cp.stats()['bytes_written']/1024:>9.1f} KB total, "
                  f"{cp.stats()['writes']} writes")
    ratio = sizes[-1] / sizes[0] if sizes[0] else 0
    step_ratio = 200 / 25
    if ratio > step_ratio * 1.8:
        finding("MED", "checkpoint-growth",
                f"total bytes grew {ratio:.1f}x for an {step_ratio:.0f}x step increase, "
                "so cost is superlinear in run length")
    else:
        finding("OK", "checkpoint-growth",
                f"total bytes grew {ratio:.1f}x for an {step_ratio:.0f}x step increase")


def probe_precondition_staleness() -> None:
    """Preconditions run once at stage 3 and are never rechecked."""
    print("\nPROBE 12: precondition staleness")
    state = {"up": True}
    verify = VerifyStateBehavior()
    verify.add(Precondition("service", lambda: state["up"], "must be up"))

    with tempfile.TemporaryDirectory() as tmp:
        def kill_service() -> str:
            state["up"] = False
            return "service died"

        steps = [
            Step("s1", command="echo a", description="a", estimated_seconds=180.0,
                 run=kill_service),
            Step("s2", command="echo b", description="b", estimated_seconds=180.0,
                 run=lambda: "still running"),
        ]
        pipe = Phase1IntegratedExecutor(
            workdir=Path(tmp), verifier=verify,
            clarifier=AskClarifyingQuestionsBehavior(ask=lambda a: "staging"),
            safety=SafetyFirstBehavior(confirm=lambda v: True),
            ollama=healthy(),
        )
        result = pipe.run("stale", "restart ollama.service on popos-01 and verify", steps)
        if result.status is PipelineStatus.COMPLETED and state["up"] is False:
            finding("HIGH", "precondition-staleness",
                    "the precondition became false mid-run and the pipeline "
                    "finished anyway, because stage 3 never runs again")


def main() -> None:
    probes: List[Callable[[], None]] = [
        probe_crash_and_resume,
        probe_conflict_midrun,
        probe_safety_evasion,
        probe_context_pathologies,
        probe_checkpoint_corruption,
        probe_concurrent_task_ids,
        probe_pipeline_rollback,
        probe_estimate_accuracy,
        probe_clarifier_false_positives,
        probe_model_routing_collisions,
        probe_checkpoint_growth,
        probe_precondition_staleness,
    ]
    for p in probes:
        try:
            p()
        except Exception as exc:
            finding("CRASH", p.__name__, f"probe itself raised {type(exc).__name__}: {exc}")

    print("\n" + "=" * 78)
    print("FINDINGS SUMMARY")
    print("=" * 78)
    for severity in ("CRASH", "HIGH", "MED", "OK"):
        rows = [f for f in FINDINGS if f[0] == severity]
        if not rows:
            continue
        print(f"\n{severity} ({len(rows)}):")
        for _, probe, note in rows:
            print(f"  - {probe}: {note}")


if __name__ == "__main__":
    main()
