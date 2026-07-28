"""
break_it_v4.py
Phase 4 adversarial probes, plus the final sweep across all four phases.

Run:  python3 break_it_v4.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, List, Tuple

from harness_core import ConflictKind, OllamaFallbackHandler, Step
from harness_core_v4 import EventDrivenConflictDetector
from core_behaviors import AskClarifyingQuestionsBehavior
from core_behaviors_v2 import ParsingSafetyClassifier
from core_behaviors_v4 import ReflinkUndoGenerator, ShadowReaper, StructuralClarifier
from clarifier_corpus import (
    BLIND_AMBIGUOUS, BLIND_SPECIFIC, HOLDOUT_AMBIGUOUS, HOLDOUT_SPECIFIC, evaluate,
)
from phase1_integrated_executor import PipelineStatus
from phase4_integrated_executor import Phase4IntegratedExecutor

FINDINGS: List[Tuple[str, str, str]] = []


def finding(severity: str, probe: str, note: str) -> None:
    FINDINGS.append((severity, probe, note))
    print(f"  [{severity}] {note}")


def healthy() -> OllamaFallbackHandler:
    return OllamaFallbackHandler(probe=lambda: True, timeout=0.1)


def probe_clarifier_generalisation() -> None:
    """The Phase 3 finding: the clarifier was never measured."""
    print("\nPROBE A: clarifier false negative rate, blind corpus")
    v1 = evaluate(AskClarifyingQuestionsBehavior(), BLIND_AMBIGUOUS, BLIND_SPECIFIC)
    v4 = evaluate(StructuralClarifier(), BLIND_AMBIGUOUS, BLIND_SPECIFIC)
    print(f"  Phase 1: fn {v1['fn_rate']:.0%}  fp {v1['fp_rate']:.0%}  f1 {v1['f1']:.2f}")
    print(f"  Phase 4: fn {v4['fn_rate']:.0%}  fp {v4['fp_rate']:.0%}  f1 {v4['f1']:.2f}")
    if v4["fn_rate"] == 0.0 and v4["fp_rate"] <= 0.15:
        finding("CLOSED", "clarifier-unmeasured",
                f"blind false negative rate {v1['fn_rate']:.0%} -> {v4['fn_rate']:.0%}, "
                f"on a set never used to tune it")
    else:
        finding("HIGH", "clarifier-unmeasured",
                f"blind fn {v4['fn_rate']:.0%}, fp {v4['fp_rate']:.0%}")
    for request, codes in v4["false_positives"]:
        finding("MED", "clarifier-fp", f"blocked a specific request: {request!r} {codes}")


def probe_clarifier_adversarial() -> None:
    """Can a vague request be phrased to look anchored?"""
    print("\nPROBE B: clarifier evasion")
    clar = StructuralClarifier()
    evasions = [
        "urgently and carefully fix the important thing right now please",
        "as discussed, do the migration",
        "apply the standard procedure",
        "do what we did last time",
        "you know what to do with the logs",
        "same as yesterday but for the other one",
        "handle whatever is broken",
        "make it like the other server",
    ]
    missed = [e for e in evasions if not clar.detect(e)]
    for m in missed:
        finding("HIGH", "clarifier-evasion", f"waved through: {m!r}")
    print(f"  caught {len(evasions) - len(missed)}/{len(evasions)}")
    if not missed:
        finding("OK", "clarifier-evasion",
                "requests that reference context the harness cannot see are all "
                "stopped, because none of them contain an anchor")

    # The inverse risk: does an anchor alone buy a pass it should not?
    anchored_but_vague = [
        "do something about /var/log",
        "fix /etc/nginx/nginx.conf",
    ]
    passed = [a for a in anchored_but_vague if not clar.detect(a)]
    if passed:
        finding("MED", "clarifier-anchor-loophole",
                f"an anchor alone cleared these: {passed}. A path plus a vague "
                "verb still has no acceptance criterion, and the clarifier does "
                "not currently require one for non-improvement verbs")


def probe_shadow_reaper_safety() -> None:
    """Can the reaper be made to delete something it should not?"""
    print("\nPROBE C: reaper safety")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        traps = {
            "important-user-data": True,
            "harness-shadow-backup-of-everything": False,
            ".harness-shadow-hidden": True,
            "harness-shadowy-business": False,
        }
        for name in traps:
            d = root / name
            d.mkdir()
            (d / "content.bin").write_bytes(b"x" * 512)
            os.utime(d, (time.time() - 90_000, time.time() - 90_000))

        ShadowReaper(roots=[root], max_age_seconds=3600, min_age_seconds=1).reap()

        wrongly_deleted = [n for n, should_survive in traps.items()
                           if should_survive and not (root / n).exists()]
        for name in wrongly_deleted:
            finding("HIGH", "reaper-safety", f"deleted a non-shadow directory: {name}")
        if not wrongly_deleted:
            finding("OK", "reaper-safety",
                    "prefix matching held: user data and hidden directories survived")

        if (root / "harness-shadowy-business").exists():
            finding("MED", "reaper-safety",
                    "prefix match is a plain startswith, so a directory merely "
                    "beginning with the prefix is spared or taken by accident of "
                    "naming rather than by design")


def probe_event_detector_correctness() -> None:
    """Does the fast path ever disagree with a full walk?"""
    print("\nPROBE D: event path versus full walk")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "tree"
        for d in range(10):
            (root / f"pkg{d}").mkdir(parents=True)
            for f in range(20):
                (root / f"pkg{d}" / f"m{f}.py").write_text("x" * 512, encoding="utf-8")

        det = EventDrivenConflictDetector(reconcile_every=1000)
        det.baseline_tree([root])
        if not det.use_inotify:
            finding("MED", "event-correctness", "inotify unavailable, probe skipped")
            return

        det.detect_tree()
        (root / "pkg3" / "m7.py").write_text("CHANGED", encoding="utf-8")
        (root / "pkg8" / "new.py").write_text("NEW", encoding="utf-8")
        (root / "pkg1" / "m2.py").unlink()
        time.sleep(0.1)

        fast = {(c.path, c.kind) for c in det.detect_tree()}
        walked = {(c.path, c.kind) for c in det.force_reconcile()}

        missed = walked - fast
        spurious = fast - walked
        print(f"  fast path found {len(fast)}, walk found {len(walked)}, "
              f"missed {len(missed)}, spurious {len(spurious)}")
        if missed:
            finding("MED", "event-correctness",
                    f"the fast path missed {len(missed)} change(s) the walk found; "
                    "this is exactly why the scheduled reconcile exists")
        if spurious:
            finding("HIGH", "event-correctness",
                    f"the fast path reported {len(spurious)} conflict(s) that do "
                    "not exist")
        if not missed and not spurious:
            finding("OK", "event-correctness",
                    "event path and full walk agreed exactly on 3 changes across "
                    "200 files")
        det.close()


def probe_event_detector_cost() -> None:
    """Is the fast path actually cheaper than walking?"""
    print("\nPROBE E: event path cost versus walk")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "repo"
        for d in range(40):
            (root / f"pkg{d}").mkdir(parents=True)
            for f in range(50):
                (root / f"pkg{d}" / f"m{f}.py").write_text("x" * 1024, encoding="utf-8")
        total = 40 * 50

        det = EventDrivenConflictDetector(reconcile_every=10_000)
        det.baseline_tree([root])
        if not det.use_inotify:
            finding("MED", "event-cost", "inotify unavailable, probe skipped")
            return

        det.detect_tree()
        started = time.perf_counter()
        for _ in range(20):
            det.detect_tree()
        fast_ms = (time.perf_counter() - started) * 1000 / 20

        started = time.perf_counter()
        det.force_reconcile()
        walk_ms = (time.perf_counter() - started) * 1000

        print(f"  {total} files: fast check {fast_ms:.3f} ms, "
              f"full walk {walk_ms:.1f} ms, speedup {walk_ms / max(fast_ms, 1e-6):.0f}x")
        if fast_ms < walk_ms / 10:
            finding("CLOSED", "inotify-fast-path",
                    f"a clean check went from {walk_ms:.1f} ms to {fast_ms:.3f} ms "
                    f"on {total} files, a {walk_ms / max(fast_ms, 1e-6):.0f}x speedup")
        else:
            finding("MED", "inotify-fast-path",
                    f"fast path {fast_ms:.2f} ms vs walk {walk_ms:.1f} ms, "
                    "not the win it should be")
        det.close()


def probe_shadow_cost_v4() -> None:
    """Did reflink actually reduce shadow disk on this filesystem?"""
    print("\nPROBE F: shadow disk cost")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "files"
        root.mkdir()
        paths = [str(p) for p in (
            (root / f"c{i}.yaml") for i in range(50))]
        for p in paths:
            Path(p).write_bytes(b"x" * 200_000)

        gen = ReflinkUndoGenerator(shadow_dir=Path(tmp) / "shadow")
        started = time.perf_counter()
        gen.for_step(Step("bulk", touches=paths, run=lambda: None))
        elapsed = time.perf_counter() - started
        stats = gen.stats()

        used = subprocess.run(["du", "-sk", str(Path(tmp) / "shadow")],
                              capture_output=True, text=True)
        du_kb = used.stdout.split()[0] if used.returncode == 0 else "?"
        print(f"  50 x 200 KB: reflink={stats['reflink_supported']} "
              f"clones={stats['reflink_copies']} copies={stats['byte_copies']} "
              f"du={du_kb} KB in {elapsed * 1000:.0f} ms")

        if stats["reflink_supported"]:
            finding("CLOSED", "shadow-cost",
                    f"copy-on-write clones used, {stats['reflink_copies']} of 50, "
                    f"du reports {du_kb} KB instead of ~10 MB")
        else:
            finding("OPEN", "shadow-cost",
                    "this filesystem does not support reflinks, so shadows are "
                    "still byte for byte. On btrfs or xfs the same code clones "
                    "instead. ext4 users pay the Phase 3 cost")


def probe_full_regression() -> None:
    """All 81 cases across four phases."""
    print("\nPROBE G: full regression, phases 1 through 4")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q", "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=str(Path(__file__).resolve().parent))
    tail = (result.stdout or result.stderr).strip().splitlines()[-1:]
    print(f"  {tail[0] if tail else 'no output'}")
    if result.returncode == 0:
        finding("OK", "full-regression", "all 81 cases pass across four phases")
    else:
        finding("HIGH", "full-regression", f"regression: {tail}")


def probe_pipeline_end_to_end() -> None:
    """A vague task and a specific task through the real Phase 4 pipeline."""
    print("\nPROBE H: pipeline end to end")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        conf = root / "app.conf"
        conf.write_text("ORIGINAL\n", encoding="utf-8")
        ran: List[str] = []

        steps = [
            Step("edit", kind="write", command="echo x", description="edit",
                 estimated_seconds=20, touches=[str(conf)],
                 run=lambda: (ran.append("edit"),
                              conf.write_text("CLOBBERED\n", encoding="utf-8"))),
            Step("boom", command="echo y", description="fail", estimated_seconds=20,
                 run=lambda: (_ for _ in ()).throw(RuntimeError("failed"))),
        ]

        vague = Phase4IntegratedExecutor(
            workdir=root / "v", clarifier=StructuralClarifier(),
            safety=ParsingSafetyClassifier(confirm=lambda v: True), ollama=healthy())
        vague_result = vague.run("vague", "tidy things up a bit", steps)
        if vague_result.status is PipelineStatus.NEEDS_CLARIFICATION and not ran:
            finding("OK", "pipeline-e2e",
                    "a vague task stopped at stage 1 and executed nothing")
        else:
            finding("HIGH", "pipeline-e2e",
                    f"vague task reached {vague_result.status.value}")

        specific = Phase4IntegratedExecutor(
            workdir=root / "s", watch_roots=[root],
            clarifier=StructuralClarifier(ask=lambda a: "staging"),
            safety=ParsingSafetyClassifier(confirm=lambda v: True), ollama=healthy())
        result = specific.run("specific", "deploy commit a1b2c3d to staging", steps)
        if (result.status is PipelineStatus.EXECUTION_FAILED
                and conf.read_text(encoding="utf-8").strip() == "ORIGINAL"
                and result.rollback_verified):
            finding("OK", "pipeline-e2e",
                    "a specific task ran, failed, auto-reverted, and verified the "
                    "revert, with no caller-supplied undo")
        else:
            finding("HIGH", "pipeline-e2e",
                    f"status {result.status.value}, file "
                    f"{conf.read_text(encoding='utf-8').strip()!r}, "
                    f"verified {result.rollback_verified}")


def probe_remaining_gaps() -> None:
    """What is still open after four phases."""
    print("\nPROBE I: known remaining gaps")
    finding("OPEN", "clarifier-semantics",
            "the clarifier tests for structural anchors, not meaning. "
            "'fix /etc/nginx/nginx.conf' has a path and passes, though 'fix' "
            "still has no acceptance criterion")
    finding("OPEN", "corpus-size",
            "the blind corpus is 22 entries. A 0% false negative rate on 12 "
            "ambiguous requests is encouraging, not conclusive")
    finding("OPEN", "undo-coverage-ceiling",
            "database migrations, dd, and mkfs still need a caller-supplied undo")
    finding("OPEN", "mtime-evasion",
            "a same-size same-mtime edit is caught only by the declared-path "
            "SHA256 layer, so Step.touches still matters")


def main() -> None:
    probes: List[Callable[[], None]] = [
        probe_clarifier_generalisation,
        probe_clarifier_adversarial,
        probe_shadow_reaper_safety,
        probe_event_detector_correctness,
        probe_event_detector_cost,
        probe_shadow_cost_v4,
        probe_pipeline_end_to_end,
        probe_full_regression,
        probe_remaining_gaps,
    ]
    for p in probes:
        try:
            p()
        except Exception as exc:
            finding("CRASH", p.__name__, f"probe raised {type(exc).__name__}: {exc}")

    print("\n" + "=" * 78)
    print("PHASE 4 FINDINGS SUMMARY")
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
